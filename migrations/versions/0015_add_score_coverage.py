"""Record whether a scoring run covered the market or a single company.

Single-stock preparation reuses `score_market`, which is the point — one scoring
engine, one formula, no second implementation. But it wrote its snapshot on
today's date, and the ranking views take the most recent date there is. So one
`deep-research run MU` made today's ranking a list of one company, and the
dashboard showed two rows over a universe of five thousand.

The fix is not to stop persisting per-ticker scores; the report has to explain a
score that really exists. It is to say which runs were market-wide, so the
rankings can ask for the newest day the *market* was scored and ignore the rest.

Backfill classifies by size. A market run scores the whole stored universe and a
per-ticker run scores one company, so a `(score_date, score_version)` group of
fewer than ten rows was not a market run. There is no other signal available for
rows written before this column existed, and the two populations are four orders
of magnitude apart.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None

_MARKET_RUN_MIN_ROWS = 10


def upgrade() -> None:
    """Add the coverage column, classify existing rows, then make it required."""
    op.add_column(
        "score_snapshots",
        sa.Column("coverage", sa.String(length=10), nullable=True),
    )

    op.execute(sa.text("UPDATE score_snapshots SET coverage = 'MARKET'"))
    op.execute(
        sa.text(
            "UPDATE score_snapshots SET coverage = 'SINGLE' "
            "WHERE (score_date, score_version) IN ("
            "  SELECT score_date, score_version FROM score_snapshots"
            "  GROUP BY score_date, score_version"
            "  HAVING COUNT(*) < :threshold"
            ")"
        ).bindparams(threshold=_MARKET_RUN_MIN_ROWS)
    )

    with op.batch_alter_table("score_snapshots") as batch:
        batch.alter_column("coverage", existing_type=sa.String(length=10), nullable=False)

    op.create_index(
        "ix_score_snapshots_coverage_date",
        "score_snapshots",
        ["score_version", "coverage", "score_date"],
    )


def downgrade() -> None:
    """Drop the coverage column and its index."""
    op.drop_index("ix_score_snapshots_coverage_date", table_name="score_snapshots")
    op.drop_column("score_snapshots", "coverage")
