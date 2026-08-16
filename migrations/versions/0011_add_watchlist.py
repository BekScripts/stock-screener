"""Store the companies a person chose to keep watching.

The ranking answers "what looks interesting today". It cannot answer "what did I
decide to follow", because that is a judgement rather than a calculation, and it
survives a company dropping out of the top fifty — which is exactly when
remembering it matters.

Deliberately three columns. A ticker, when it was added, and an optional note.
No target price, no position size, no folders or tags: those turn a shortlist
into a portfolio tool, and the pipeline that feeds this has no notion of a
position.

Unique on `company_id`, so adding a company twice is idempotent at the database
rather than in whichever caller happened to press the button.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the watchlist table and its uniqueness constraint."""
    op.create_table(
        "watchlist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", name="uq_watchlist_company"),
    )


def downgrade() -> None:
    """Drop the watchlist table."""
    op.drop_table("watchlist")
