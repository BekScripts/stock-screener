"""Initial schema: companies, financial_snapshots, price_history.

The two unique constraints created here are what make the daily ingest
idempotent. `uq_financial_snapshot_period` stops a restated quarter becoming a
second row that would double a trailing-twelve-month total, and
`uq_price_history_session` stops the overlap re-fetch duplicating bars.

Revision ID: 0001
Revises:
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the three Phase 1 tables."""
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("exchange", sa.String(length=50), nullable=True),
        sa.Column("sector", sa.String(length=120), nullable=True),
        sa.Column("industry", sa.String(length=160), nullable=True),
        sa.Column("market_cap", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_companies_ticker", "companies", ["ticker"], unique=True)

    op.create_table(
        "financial_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("revenue", sa.Float(), nullable=True),
        sa.Column("gross_profit", sa.Float(), nullable=True),
        sa.Column("operating_income", sa.Float(), nullable=True),
        sa.Column("operating_cash_flow", sa.Float(), nullable=True),
        sa.Column("capital_expenditure", sa.Float(), nullable=True),
        sa.Column("free_cash_flow", sa.Float(), nullable=True),
        sa.Column("cash", sa.Float(), nullable=True),
        sa.Column("total_debt", sa.Float(), nullable=True),
        sa.Column("shares_outstanding", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "period_end", name="uq_financial_snapshot_period"),
    )
    op.create_index(
        "ix_financial_snapshots_company_period",
        "financial_snapshots",
        ["company_id", "period_end"],
        unique=False,
    )

    op.create_table(
        "price_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "date", name="uq_price_history_session"),
    )
    op.create_index(
        "ix_price_history_company_date",
        "price_history",
        ["company_id", "date"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the three Phase 1 tables."""
    op.drop_index("ix_price_history_company_date", table_name="price_history")
    op.drop_table("price_history")
    op.drop_index("ix_financial_snapshots_company_period", table_name="financial_snapshots")
    op.drop_table("financial_snapshots")
    op.drop_index("ix_companies_ticker", table_name="companies")
    op.drop_table("companies")
