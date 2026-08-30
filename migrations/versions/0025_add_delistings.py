"""Record the securities that left the market, which no other table can hold.

Measured on staging: across 4,599 companies and the 120 sessions after
2024-01-02, **not one security stopped trading**. A real universe of that size
loses roughly a hundred names to acquisitions and failures over six months. The
zero is not a property of the market; it is a property of a `companies` table
that holds what is investable today, and therefore cannot contain anything that
stopped being investable.

Every downstream consequence follows from that one fact. A forward return can
never come back `INTERRUPTED`. A backtest over the ranking silently studies only
the survivors. And no amount of care in the point-in-time layers repairs it,
because the rows were never there to bound.

This table is keyed by ticker rather than by `company_id`, and that is
deliberate. The alternative — giving each delisted security a company row —
would fix the join by putting thousands of untradeable tickers into the table
the scanner, the rankings, the CLI and the dashboard all read as the investable
set. The bias is worth fixing; it is not worth fixing there.

What the source supports and what it does not: the corporate-action endpoint
enumerates mergers and worthless removals without a symbol filter, which is what
makes discovery possible at all — 946 distinct delisting subjects in 2023 alone,
including ATVI, SGEN, HZNP and VMW, none of which appear in the provider's own
inactive-asset list. It does **not** cover receivership: SVB and First Republic
are absent under every type. That gap is documented rather than guessed at.

Additive. One new table, no existing table touched, no `batch_alter_table`.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the delisting table."""
    op.create_table(
        "delistings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("cash_per_share", sa.Float(), nullable=True),
        sa.Column("acquirer_ticker", sa.String(length=20), nullable=True),
        sa.Column("acquirer_rate", sa.Float(), nullable=True),
        sa.Column("acquiree_rate", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.String(length=80), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_id", name="uq_delisting_source"),
    )
    op.create_index("ix_delistings_ticker", "delistings", ["ticker"], unique=False)
    op.create_index("ix_delistings_date", "delistings", ["effective_date"], unique=False)


def downgrade() -> None:
    """Drop it, indexes first."""
    op.drop_index("ix_delistings_date", table_name="delistings")
    op.drop_index("ix_delistings_ticker", table_name="delistings")
    op.drop_table("delistings")
