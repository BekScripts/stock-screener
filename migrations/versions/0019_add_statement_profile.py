"""Record statement shape and consolidated liquidity on a company.

Two independent gaps, both about admitting a company to the ranking rather than
about what it scores.

`statement_profile` is the shape of the filing, classified from the concepts it
tags. A deposit-funded bank was scoring 74.78 and ranking twenty-first because
both available classifications called it a technology company: a market-data
vendor said `Software - Infrastructure`, the SEC's own SIC list said `Business
Services`. Its statements said customer deposits, a loan book and central-bank
balances. Labels can be wrong; the statements cannot. The classification has to
live here because it is derivable only while the concept names are in hand —
after ingestion all that remains is normalised figures, and a bank's look exactly
like a shop's.

`consolidated_avg_volume` and `volume_source` carry the liquidity figure the
$1M threshold is calibrated against. Stored price bars come from a single
exchange, which is a few percent of the real tape and varies between companies
by a factor of two thousand, so the threshold cannot be applied to them. The
consolidated tape can answer, and provenance travels beside the number so a
liquidity decision can be traced to the feed that made it.

All three are NULL for existing rows and mean exactly what NULL should mean:
nobody has classified this company yet, and no consolidated figure has been
fetched for it. `statement_profile` reads as GENERAL, which is what every
currently-scored company already is — so the domestic universe scores
identically across this migration.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the statement-profile and consolidated-volume columns."""
    # Three plain ADD COLUMNs, which SQLite performs in place, and all nullable
    # so none of them needs a backfill. No `batch_alter_table` anywhere near
    # this: rebuilding `companies` drops it, and dropping it under enforced
    # foreign keys is how migration 0017 deleted 1.6 million rows.
    op.add_column("companies", sa.Column("statement_profile", sa.String(length=30), nullable=True))
    op.add_column("companies", sa.Column("consolidated_avg_volume", sa.Float(), nullable=True))
    op.add_column("companies", sa.Column("volume_source", sa.String(length=20), nullable=True))


def downgrade() -> None:
    """Drop the statement-profile and consolidated-volume columns."""
    op.drop_column("companies", "volume_source")
    op.drop_column("companies", "consolidated_avg_volume")
    op.drop_column("companies", "statement_profile")
