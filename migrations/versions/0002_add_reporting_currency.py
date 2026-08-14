"""Record the currency a company reports in.

Market capitalisation is quoted in USD by the market, but a foreign issuer
listed on a U.S. exchange may file its statements in another currency. Without
knowing which, Phase 2 would divide a dollar market cap by a euro revenue and
produce a valuation multiple wrong by the exchange rate — silently, and by a
factor that still looks plausible.

NULL means the provider did not say. The eligibility screen treats that as USD,
which is true for the overwhelming majority of U.S.-listed common stock.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the currency columns."""
    op.add_column("companies", sa.Column("currency", sa.String(length=3), nullable=True))
    op.add_column(
        "financial_snapshots",
        sa.Column("reported_currency", sa.String(length=3), nullable=True),
    )


def downgrade() -> None:
    """Drop the currency columns."""
    op.drop_column("financial_snapshots", "reported_currency")
    op.drop_column("companies", "currency")
