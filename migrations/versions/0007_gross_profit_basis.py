"""Record which accounting concept produced a gross profit.

XBRL has no single cost-of-revenue concept, and filers migrate between them: HF
Sinclair stopped tagging `CostOfGoodsAndServicesSold` in 2024, Expedia dropped
`CostOfRevenue` in 2019, and Expand Energy last tagged `GrossProfit` in 2011 —
while all three keep reporting revenue. Widening the chain recovers their gross
margins, but the concepts are not interchangeable: one that excludes
depreciation, depletion and amortisation yields a *higher* gross profit than one
that includes it.

Storing the concept alongside the figure is what keeps those two from being read
as the same measure, in the same spirit as `market_cap_source` and
`volume_basis`.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the gross-profit provenance column."""
    op.add_column(
        "financial_snapshots",
        sa.Column("gross_profit_basis", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    """Drop the gross-profit provenance column."""
    op.drop_column("financial_snapshots", "gross_profit_basis")
