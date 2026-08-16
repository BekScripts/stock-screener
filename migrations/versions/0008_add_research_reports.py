"""Store one validated AI research report per company, brief and prompt.

Phase 3 explains a ranking rather than producing one, so its output needs a home
that cannot be confused with the score it describes. Reports live in their own
table for the same reason scores were kept out of `financial_snapshots`: that
table holds what a company reported, `score_snapshots` holds an opinion derived
from it under a named set of rules, and this one holds prose written about that
opinion by a model. Mixed together, a restatement, a re-score and a re-read would
be indistinguishable.

The unique constraint is the cache key. `(company_id, score_version,
brief_fingerprint, prompt_version)` identifies a company, the scoring rules its
report explains, a hash of every piece of evidence supplied, and the prompt that
turned that evidence into sentences. If all four match, the stored report is the
answer and calling a model again would buy nothing. If any one differs, it is a
different reading of different evidence and deserves its own row.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the research report table, its cache key and its index."""
    op.create_table(
        "research_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("score_version", sa.String(length=40), nullable=False),
        sa.Column("score_date", sa.Date(), nullable=False),
        sa.Column("brief_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("contract_version", sa.String(length=40), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "score_version",
            "brief_fingerprint",
            "prompt_version",
            name="uq_research_report_cache_key",
        ),
    )
    op.create_index(
        "ix_research_reports_company_generated",
        "research_reports",
        ["company_id", "generated_at"],
    )


def downgrade() -> None:
    """Drop the research report table."""
    op.drop_index("ix_research_reports_company_generated", table_name="research_reports")
    op.drop_table("research_reports")
