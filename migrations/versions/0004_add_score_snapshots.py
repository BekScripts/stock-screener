"""Store one CompounderScore per company per day per formula version.

Phase 2 turns metrics into a ranking, and a ranking is only interesting over
time: "improving fast" is the difference between today's score and the nearest
snapshot thirty days ago. That needs history, and history needs its own table.

Scores are kept apart from `financial_snapshots` on purpose. That table holds
what a company reported; this one holds an opinion derived from it under a named
set of rules. Mixed together, a restatement and a re-score would be
indistinguishable.

The unique constraint spans the version as well as the company and the date, so
a v2 score never overwrites the v1 history it cannot be compared with.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the score snapshot table and its indexes."""
    op.create_table(
        "score_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("score_date", sa.Date(), nullable=False),
        sa.Column("score_version", sa.String(length=40), nullable=False),
        sa.Column(
            "calculated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("scoring_status", sa.String(length=30), nullable=False),
        # Every score column is nullable: a company that could not be scored
        # still gets a row, carrying the status that says why.
        sa.Column("growth_score", sa.Float(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("valuation_score", sa.Float(), nullable=True),
        sa.Column("momentum_score", sa.Float(), nullable=True),
        sa.Column("raw_score", sa.Float(), nullable=True),
        sa.Column("risk_penalty", sa.Float(), nullable=True),
        sa.Column("final_score", sa.Float(), nullable=True),
        sa.Column("risk_level", sa.String(length=20), nullable=True),
        sa.Column("score_category", sa.String(length=40), nullable=True),
        sa.Column("valuation_basis", sa.String(length=20), nullable=True),
        sa.Column("data_coverage", sa.Float(), nullable=True),
        sa.Column("risk_coverage", sa.Float(), nullable=True),
        sa.Column("market_cap", sa.Float(), nullable=True),
        sa.Column("revenue_growth_yoy", sa.Float(), nullable=True),
        sa.Column("revenue_growth_acceleration", sa.Float(), nullable=True),
        sa.Column("enterprise_value", sa.Float(), nullable=True),
        sa.Column("breakdown", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "score_date", "score_version", name="uq_score_snapshot_daily"
        ),
    )
    op.create_index(
        "ix_score_snapshots_company_date", "score_snapshots", ["company_id", "score_date"]
    )
    op.create_index(
        "ix_score_snapshots_ranking",
        "score_snapshots",
        ["score_version", "score_date", "final_score"],
    )


def downgrade() -> None:
    """Drop the score snapshot table."""
    op.drop_index("ix_score_snapshots_ranking", table_name="score_snapshots")
    op.drop_index("ix_score_snapshots_company_date", table_name="score_snapshots")
    op.drop_table("score_snapshots")
