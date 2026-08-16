"""Store the SEC filing index so a research brief can cite a filing.

A research report is supposed to distinguish what a company reported from what a
model inferred, and a citation is how it does that. Until now there was nothing
to cite: EDGAR's submissions document was fetched on every fundamentals pass for
its SIC description and the filings index inside it was discarded.

This table keeps that index. Metadata only — form, dates, accession number and
location — because the value here is being able to say *that* a company filed a
10-Q on a date, at a URL a reader can open. Extracting the text behind that URL
is a separate concern with its own failure modes, and a brief that cites a filing
it did not read is still citing something verifiable.

Storing it is what lets a brief be assembled with no provider call at all: the
nightly pass fetches, and research reads. A brief that reached the network while
being assembled would make a research run depend on a vendor being up.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the filings table, its uniqueness constraint and its index."""
    op.create_table(
        "filings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("accession", sa.String(length=30), nullable=False),
        sa.Column("form", sa.String(length=20), nullable=False),
        sa.Column("filed", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("primary_document", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "accession", name="uq_filing_accession"),
    )
    op.create_index("ix_filings_company_filed", "filings", ["company_id", "filed"])


def downgrade() -> None:
    """Drop the filings table."""
    op.drop_index("ix_filings_company_filed", table_name="filings")
    op.drop_table("filings")
