"""Store extracted filing text so a claim about a business can cite it.

The filing index added in 0009 records that a company filed — form, date,
accession, URL — and deliberately nothing about what the document says. That was
enough to cite a filing and not enough to describe one, so every research report
answered `company_summary`, `recent_developments` and `catalysts` with `UNKNOWN`.

This table holds the missing half: verbatim text, one row per section of one
filing, extracted deterministically by pattern rather than by a model. The two
stay separate tables because they support different claims. `D.<accession>`
proves a filing exists; `X.<accession>.<section>` is text a reader can check, and
only the second may support a claim about what a company does or announced.

Uniqueness is company, accession and section, so re-extraction replaces rows
instead of accumulating near-duplicate paragraphs. `extracted_at` is separate
from `filed`: re-reading a document under an improved extractor moves the first
and never the second.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the filing_excerpts table, its uniqueness constraint and its index."""
    op.create_table(
        "filing_excerpts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("accession", sa.String(length=30), nullable=False),
        sa.Column("form", sa.String(length=20), nullable=False),
        sa.Column("section", sa.String(length=40), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("filed", sa.Date(), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "accession", "section", name="uq_filing_excerpt_section"),
    )
    op.create_index("ix_filing_excerpts_company_filed", "filing_excerpts", ["company_id", "filed"])


def downgrade() -> None:
    """Drop the filing_excerpts table."""
    op.drop_index("ix_filing_excerpts_company_filed", table_name="filing_excerpts")
    op.drop_table("filing_excerpts")
