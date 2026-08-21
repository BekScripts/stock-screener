"""Store exchange rates, and split reporting currency from quote currency.

Two changes, both about the same confusion.

`companies.currency` held one answer to two questions. EDGAR wrote the currency
a company files its statements in; a market-data vendor wrote the currency its
shares trade in, which for an ADR is USD whatever the company reports in.
Whichever provider ran last won, and nothing downstream could tell which meaning
it was reading. The column is split so the two can never be confused again:
`reporting_currency` is what the balance sheet is in, `quote_currency` is what
the market capitalisation is in, and a foreign issuer legitimately has two
different values.

The existing column is renamed rather than dropped and re-added, because its
values came from EDGAR's XBRL unit key in Phase 7B and are genuinely the
reporting currency. `quote_currency` starts NULL, which reads as USD — correct
for every listing this screen admits, since NASDAQ, NYSE and NYSE American all
quote in dollars.

`fx_rates` then stores the rates that bring the two together. Rates are
persisted rather than fetched and forgotten so a score stays reproducible: a
valuation computed from a rate nobody wrote down cannot be checked afterwards,
and re-deriving it later would use a different rate and quietly answer the same
question differently. The unique constraint includes the provider, because two
sources publishing the same pair on the same day are two observations rather
than a conflict, and a score should say which one it used.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Split the currency column and add the rate store."""
    # Deliberately **not** `batch_alter_table`. On SQLite that rebuilds the
    # table — create, copy, drop, rename — and dropping `companies` while
    # foreign keys are enforced cascades through every child that references it
    # with ON DELETE CASCADE. That is price history, fundamentals, scores,
    # filings, excerpts, research reports and the user's watchlist: about 1.6
    # million rows, deleted by a migration whose entire purpose was to rename a
    # column. `benchmark_prices` and `jobs` would survive, because they are the
    # only two tables with no foreign key to `companies`.
    #
    # SQLite has supported RENAME COLUMN natively since 3.25 and ADD COLUMN
    # forever. Both edit the schema in place, touch no rows, and cascade
    # nothing.
    op.execute(sa.text("ALTER TABLE companies RENAME COLUMN currency TO reporting_currency"))
    op.execute(sa.text("ALTER TABLE companies ADD COLUMN quote_currency VARCHAR(3)"))

    op.create_table(
        "fx_rates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("rate", sa.Float(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column(
            "retrieved_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "base_currency",
            "quote_currency",
            "rate_date",
            "provider",
            name="uq_fx_rate_observation",
        ),
    )
    op.create_index(
        "ix_fx_rates_pair_date", "fx_rates", ["base_currency", "quote_currency", "rate_date"]
    )


def downgrade() -> None:
    """Drop the rate store and merge the currency columns back into one."""
    op.drop_index("ix_fx_rates_pair_date", table_name="fx_rates")
    op.drop_table("fx_rates")

    op.execute(sa.text("ALTER TABLE companies DROP COLUMN quote_currency"))
    op.execute(sa.text("ALTER TABLE companies RENAME COLUMN reporting_currency TO currency"))
