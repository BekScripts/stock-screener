"""Record whether a security may be researched, month by month.

Phase 9K.7. The end of the 9K sequence: everything before it established what
the free evidence says, and this gives the verdict somewhere to live.

One new table, nothing existing altered. No column dropped, none relaxed, no
table rebuilt — so unlike `0027` and `0029` there is no foreign-key hazard and no
row-preservation risk to manage here.

## Why not `universe_membership`

Extending it was the first option considered and it does not work, for four
reasons that are all load-bearing:

- **It is interval-shaped.** Its rows are `valid_from`/`valid_to` spans, and this
  record is one row per month. Expressing "observed January, unknown February,
  observed March" as intervals is possible but reads as three adjacent spans,
  which is exactly the continuity this layer must not imply.
- **Its unique index would collide.** `(historical_security_id, valid_from)` and
  the company equivalent are unique, and **498 existing rows already start on a
  month boundary**. A monthly row for the same security on such a date would be
  rejected.
- **It has no class.** There is no `security_class`, no evidence mode, no
  accession and no acceptance date, and the whole point of this phase is that
  membership requires class.
- **It already has readers.** `members_as_of` serves `backtest.chain`,
  `signals.derivation` and the CLI with Phase 9C's meaning. Writing a second
  producer's rows into it, under a third semantics, would silently change what
  every one of those returns.

So this table is additive and separate, and `universe_membership` keeps its
meaning intact.

## Why the Option C check applies here and not in `listing_observations`

`listing_observations` is keyed by symbol and permits both identity columns to be
NULL, because a market-share file gives a ticker and nothing else — an
unattributed observation is the ordinary case there. A row *here* presupposes
that attribution already happened, so the full inequality applies: exactly one of
`company_id` and `historical_security_id`, never both and never neither.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the monthly research-universe table."""
    op.create_table(
        "research_universe_months",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("historical_security_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("observation_month", sa.Date(), nullable=False),
        sa.Column("venue", sa.String(length=20), nullable=True),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("security_class", sa.String(length=30), nullable=False),
        sa.Column("class_evidence_mode", sa.String(length=20), nullable=False),
        sa.Column("class_age_days", sa.Integer(), nullable=True),
        sa.Column("class_source_accession", sa.String(length=30), nullable=True),
        sa.Column("class_evidence_as_of", sa.Date(), nullable=True),
        sa.Column("listing_source_file_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=True),
        sa.Column("eligibility_reasons", sa.String(length=200), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["historical_security_id"], ["historical_securities.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["listing_source_file_id"], ["listing_source_files.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(company_id IS NULL) <> (historical_security_id IS NULL)",
            name="ck_research_universe_one_identity",
        ),
    )
    op.create_index(
        "uq_research_universe_company",
        "research_universe_months",
        ["company_id", "observation_month"],
        unique=True,
        sqlite_where=sa.text("company_id IS NOT NULL"),
        postgresql_where=sa.text("company_id IS NOT NULL"),
    )
    op.create_index(
        "uq_research_universe_historical",
        "research_universe_months",
        ["historical_security_id", "observation_month"],
        unique=True,
        sqlite_where=sa.text("historical_security_id IS NOT NULL"),
        postgresql_where=sa.text("historical_security_id IS NOT NULL"),
    )
    op.create_index(
        "ix_research_universe_month_state",
        "research_universe_months",
        ["observation_month", "state"],
    )
    op.create_index(
        "ix_research_universe_symbol", "research_universe_months", ["symbol", "observation_month"]
    )


def downgrade() -> None:
    """Drop the table.

    Safe in a way `0027`'s downgrade is not: nothing references this table, so
    dropping it cannot cascade into data that predates the revision.
    """
    op.drop_table("research_universe_months")
