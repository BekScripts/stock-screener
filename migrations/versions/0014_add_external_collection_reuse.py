"""Record the external evidence a deep report was collected from, and when.

The report itself carries only the sources its claims cite, which is right for
reading it back and wrong for reusing it. A rerun needs the **whole** accepted
set, or the brief it rebuilds would be smaller than the one that produced the
report and would miss the cache it was trying to hit.

Why reuse at all: a search engine answers the same query with a slightly
different valid article set every few minutes. The resulting fingerprint change
is real and must not be normalised away — but paying a model to re-read
substantially the same news is waste, and a dashboard button that costs money on
every press is a bad button.

`external_state` is what makes a reuse decision safe. A collection whose searches
partly failed produced a thin set on purpose, and treating that as a healthy
cache would freeze the gap in place for the whole window.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the collected-evidence columns to the deep research report table."""
    op.add_column(
        "deep_research_reports",
        sa.Column("external_state", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "deep_research_reports",
        sa.Column("external_collected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "deep_research_reports",
        sa.Column("collected_external_json", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_deep_research_reports_collection",
        "deep_research_reports",
        ["company_id", "deterministic_fingerprint", "external_collected_at"],
    )


def downgrade() -> None:
    """Drop the collected-evidence columns."""
    op.drop_index("ix_deep_research_reports_collection", table_name="deep_research_reports")
    op.drop_column("deep_research_reports", "collected_external_json")
    op.drop_column("deep_research_reports", "external_collected_at")
    op.drop_column("deep_research_reports", "external_state")
