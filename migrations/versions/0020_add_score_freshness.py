"""Record whether a score's fundamentals were current when it was computed.

CompounderScore V1.2 excludes stale scores from current rankings, and this is
the column it reads. Freshness is recorded rather than derived because the bound
depends on the company's reporting cadence — an annual filer is not stale eight
months after its year end — so a reader comparing a date against a fixed window
would disagree with the screen that produced the row, and two readers would
disagree with each other.

The score itself is unchanged and stays on the row: a stale score is a real
score of the company as it last reported, and it remains on the stock page, in
research and in history. What V1.2 withholds is a place in a ranking of what
looks interesting *now* — Centerra Gold ranked twenty-eighth on revenue from
2023 measured against a market capitalisation from 2026.

NULL for every existing row, and read as CURRENT. That is what those rows meant:
they were computed under V1.1, where freshness had no bearing on ranking, and
nothing about them should change retrospectively.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the freshness column to score snapshots."""
    # A plain ADD COLUMN, which SQLite performs in place, and nullable so no
    # backfill is needed. No `batch_alter_table` — rebuilding a table under
    # enforced foreign keys is how migration 0017 deleted 1.6 million rows.
    op.add_column("score_snapshots", sa.Column("freshness", sa.String(length=10), nullable=True))


def downgrade() -> None:
    """Drop the freshness column."""
    op.drop_column("score_snapshots", "freshness")
