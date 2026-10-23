"""Record securities that have left the market, addressable after delisting.

`companies` is the current investable universe. A delisted security must never
enter it, or the scanner, the rankings and the dashboard would all read a name
that no longer trades as investable. But a survivorship-aware backtest has to be
able to reconstruct that name — what it filed, what it traded at, what became of
it — from the free record that outlives its listing.

This revision adds the parallel record. `historical_securities` is keyed by CIK,
the SEC's stable filer identifier: a ticker change keeps one CIK, a reused ticker
is a different CIK, and an acquisition gives acquiree and acquirer separate CIKs,
so none of the three can contaminate the others. Its three child tables mirror
the point-in-time company record — observations selected on `filed_date`, daily
prices, and the corporate actions that make a past price readable — so the frozen
metric and scoring machinery reconstructs a historical security exactly as it
reconstructs a live one, without a `companies` row.

Additive. Four new tables, no existing table touched, no `batch_alter_table`, so
nothing cascades and no row is rebuilt.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the historical-security tables."""
    op.create_table(
        "historical_securities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("primary_ticker", sa.String(length=20), nullable=False),
        sa.Column("exchange", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("sector", sa.String(length=120), nullable=True),
        sa.Column("industry", sa.String(length=160), nullable=True),
        sa.Column("statement_profile", sa.String(length=30), nullable=True),
        sa.Column("first_filing_date", sa.Date(), nullable=True),
        sa.Column("last_filing_date", sa.Date(), nullable=True),
        sa.Column("last_trading_date", sa.Date(), nullable=True),
        sa.Column("terminal_value_per_share", sa.Float(), nullable=True),
        sa.Column("terminal_value_source", sa.String(length=50), nullable=True),
        sa.Column("reporting_currency", sa.String(length=3), nullable=True),
        sa.Column("quote_currency", sa.String(length=3), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cik", name="uq_historical_security_cik"),
    )
    op.create_index(
        "ix_historical_securities_ticker",
        "historical_securities",
        ["primary_ticker"],
        unique=False,
    )

    op.create_table(
        "historical_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("historical_security_id", sa.Integer(), nullable=False),
        sa.Column("observation_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("taxonomy", sa.String(length=40), nullable=False),
        sa.Column("concept", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("filed_date", sa.Date(), nullable=True),
        sa.Column("accession", sa.String(length=30), nullable=True),
        sa.Column("form", sa.String(length=20), nullable=True),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_period", sa.String(length=10), nullable=True),
        sa.Column("frame", sa.String(length=40), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["historical_security_id"], ["historical_securities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "historical_security_id",
            "observation_fingerprint",
            name="uq_historical_observation",
        ),
    )
    op.create_index(
        "ix_historical_observations_pit",
        "historical_observations",
        ["historical_security_id", "concept", "filed_date"],
        unique=False,
    )

    op.create_table(
        "historical_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("historical_security_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["historical_security_id"], ["historical_securities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("historical_security_id", "date", name="uq_historical_price"),
    )
    op.create_index(
        "ix_historical_prices_security_date",
        "historical_prices",
        ["historical_security_id", "date"],
        unique=False,
    )

    op.create_table(
        "historical_corporate_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("historical_security_id", sa.Integer(), nullable=False),
        sa.Column("action_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("action_type", sa.String(length=30), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("split_factor", sa.Float(), nullable=True),
        sa.Column("cash_amount", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("payable_date", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.String(length=80), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["historical_security_id"], ["historical_securities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "historical_security_id",
            "action_fingerprint",
            name="uq_historical_corporate_action",
        ),
    )
    op.create_index(
        "ix_historical_corporate_actions_security_date",
        "historical_corporate_actions",
        ["historical_security_id", "ex_date"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the historical-security tables, children first."""
    op.drop_index(
        "ix_historical_corporate_actions_security_date",
        table_name="historical_corporate_actions",
    )
    op.drop_table("historical_corporate_actions")
    op.drop_index("ix_historical_prices_security_date", table_name="historical_prices")
    op.drop_table("historical_prices")
    op.drop_index("ix_historical_observations_pit", table_name="historical_observations")
    op.drop_table("historical_observations")
    op.drop_index("ix_historical_securities_ticker", table_name="historical_securities")
    op.drop_table("historical_securities")
