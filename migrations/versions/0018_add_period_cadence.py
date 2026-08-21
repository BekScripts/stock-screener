"""Record what a reporting period actually covers.

Every stored period was implicitly a quarter, because the only filers with
fundamentals were the ones that report quarterly. Foreign private issuers report
annually or half-yearly, and their statements are just as real — a fiscal year of
revenue is a fact about a business, not a quarter that needs dividing by four.

So a period now carries its start date and a cadence classified from its own
dates. `cadence` is what stops a year of revenue being read as three months of
it: the metric engine forms a trailing year from one annual period, four
quarters or two half-years, and refuses to mix them.

Existing rows are backfilled to QUARTERLY. That is what they are — every filer
with stored fundamentals before this migration reported quarterly, since the
adapter could produce nothing else — and it keeps the domestic universe scoring
identically across the change. `period_start` stays NULL for them; the engine
infers cadence from spacing where a start date is missing, so nothing is lost.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the period start and cadence columns, backfilling existing rows."""
    # Both are plain ADD COLUMN, which SQLite performs in place. A NOT NULL
    # column is added with its default in the same statement, so no follow-up
    # `batch_alter_table` is needed — and none is wanted, because rebuilding a
    # table under enforced foreign keys is how a rename deletes a database.
    op.add_column("financial_snapshots", sa.Column("period_start", sa.Date(), nullable=True))
    op.add_column(
        "financial_snapshots",
        sa.Column("cadence", sa.String(length=12), nullable=False, server_default="UNKNOWN"),
    )
    op.execute(sa.text("UPDATE financial_snapshots SET cadence = 'QUARTERLY'"))


def downgrade() -> None:
    """Drop the period start and cadence columns."""
    op.drop_column("financial_snapshots", "cadence")
    op.drop_column("financial_snapshots", "period_start")
