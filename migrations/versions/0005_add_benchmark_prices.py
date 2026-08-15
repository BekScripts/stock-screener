"""Store the broad-market benchmark's daily price history.

Market confirmation is measured relative to the market: a stock up 30% while the
index is up 28% has told us almost nothing. That subtraction needs the index's
own price history, fetched through the same market-data provider as everything
else.

A separate table rather than a row in `companies`. The benchmark is an ETF, and
adding it to the company universe would mean every scan, every screen and every
ranking had to remember to exclude it — a filter that only has to be forgotten
once to put an index fund in a list of research candidates.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the benchmark price table and its index."""
    op.create_table(
        "benchmark_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "date", name="uq_benchmark_price_session"),
    )
    op.create_index("ix_benchmark_prices_symbol_date", "benchmark_prices", ["symbol", "date"])


def downgrade() -> None:
    """Drop the benchmark price table."""
    op.drop_index("ix_benchmark_prices_symbol_date", table_name="benchmark_prices")
    op.drop_table("benchmark_prices")
