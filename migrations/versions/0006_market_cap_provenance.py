"""Record point-in-time share counts, and where a market cap came from.

Market capitalisation used to arrive from one metered provider, which made that
provider a gate on the whole market: a company it did not answer for had no
market cap, failed the eligibility screen as missing required data, and could
not be scored however complete its filings were. On a plan that answers a few
hundred requests a day, that is most of the universe.

Filings already carry the missing piece. The cover page of every 10-Q and 10-K
states common shares outstanding at a point in time, which multiplied by the
latest close gives a market capitalisation good enough to screen on. It is not
the same claim as a vendor's figure, so the source is stored beside it rather
than the two being silently mixed.

`ranking_state` records the other half of the same idea: whether a score was
computed from broad-scan data alone, or after a candidate was enriched and its
liquidity verified against consolidated volume.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the share count and the two provenance columns."""
    op.add_column(
        "financial_snapshots",
        sa.Column("common_shares_outstanding", sa.Float(), nullable=True),
    )
    op.add_column(
        "score_snapshots",
        sa.Column(
            "ranking_state",
            sa.String(length=20),
            nullable=False,
            server_default="PRELIMINARY",
        ),
    )
    op.add_column("score_snapshots", sa.Column("market_cap_source", sa.String(length=20)))
    op.add_column("score_snapshots", sa.Column("volume_basis", sa.String(length=20)))


def downgrade() -> None:
    """Drop the share count and the provenance columns."""
    op.drop_column("score_snapshots", "volume_basis")
    op.drop_column("score_snapshots", "market_cap_source")
    op.drop_column("score_snapshots", "ranking_state")
    op.drop_column("financial_snapshots", "common_shares_outstanding")
