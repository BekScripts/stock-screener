"""Store validated deep research reports, and keep every one of them.

Phase 3's `research_reports` explains a stored score snapshot, and upserts: the
same evidence read under the same prompt is the same report, so a second run
should replace the first rather than accumulate copies.

A deep research report is a different kind of thing. It is an on-demand
investigation of one company against its current fundamentals, its filings and
what has since been published about it — dated, and interesting precisely
because it was written then. Two runs a month apart are two facts about what
this system concluded, and overwriting the older one would throw away the only
record of a thesis changing.

So this table has **no unique constraint**. `ix_deep_research_reports_cache`
serves the cache lookup instead, and the repository reads the newest row
matching a key rather than the row matching it.

Two fingerprints, not one. `deterministic_fingerprint` covers everything this
system calculated or the company filed; `evidence_fingerprint` covers that plus
the external sources. A caller can then tell the two reasons a report goes stale
apart — the fundamentals moved, or somebody published something — which are
different reasons to spend a model call.

`ticker` is denormalised beside `company_id` in the same spirit as `jobs.target`:
a report records what was said about a symbol on a date, and stays readable
afterwards.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the deep research report table and the indexes it is read by."""
    op.create_table(
        "deep_research_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("score_version", sa.String(length=40), nullable=False),
        sa.Column("deterministic_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("contract_version", sa.String(length=40), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("validated_report_json", sa.JSON(), nullable=False),
        sa.Column("validation_issues_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_deep_research_reports_cache",
        "deep_research_reports",
        ["company_id", "deterministic_fingerprint", "evidence_fingerprint", "prompt_version"],
    )
    op.create_index(
        "ix_deep_research_reports_company_generated",
        "deep_research_reports",
        ["company_id", "generated_at"],
    )


def downgrade() -> None:
    """Drop the deep research report table and its indexes."""
    op.drop_index("ix_deep_research_reports_company_generated", table_name="deep_research_reports")
    op.drop_index("ix_deep_research_reports_cache", table_name="deep_research_reports")
    op.drop_table("deep_research_reports")
