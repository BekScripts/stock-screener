"""Record a consolidated average daily share volume for each company.

The liquidity threshold in the eligibility screen is calibrated against volume
across every U.S. venue. A free market-data feed carries only one exchange —
roughly two to four percent of that — so applying the threshold to a figure
derived from its bars would be about twenty-five times too strict, and would
quietly remove most of the smaller companies this project exists to surface.

Storing the consolidated average separately lets the screen apply its threshold
to a comparable number, and lets it say so when only a partial one is available.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the consolidated average volume column."""
    op.add_column("companies", sa.Column("average_volume", sa.Float(), nullable=True))


def downgrade() -> None:
    """Drop the consolidated average volume column."""
    op.drop_column("companies", "average_volume")
