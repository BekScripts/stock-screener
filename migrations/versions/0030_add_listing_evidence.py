"""Record where a security was listed, month by month, from a free public source.

Phase 9K audited every free source of historical listing venue this project could
reach and found exactly one usable record: Nasdaq's public monthly *Market Share
by Symbol* files. This revision gives that evidence somewhere to live.

Two tables, both new, both append-only. Nothing existing is altered, no column
is dropped or relaxed, and no table is rebuilt — so unlike `0027` and `0029`
there is no foreign-key hazard here and no row-preservation risk to manage.

## Why not reuse `security_identifiers`

That table already carries `venue`, `valid_from` and `valid_to`, and reusing it
was the first thing considered. It cannot work: it is keyed by
`historical_security_id NOT NULL`, so it can hold venue for a delisted security
and for nothing else — while 93.6% of the observations this source produces
belong to *live* companies. Widening it would mean giving the identity layer a
second, nullable identity and a second meaning, which is a larger change than
adding a table that says one thing.

## Why `listing_observations` does not use the Option C check

`forward_returns`, `universe_membership` and the PIT snapshot tables all carry
`CHECK ((company_id IS NULL) <> (historical_security_id IS NULL))` — exactly one
identity, never neither. This table deliberately does **not**, and the reason is
the source rather than the pattern. A market-share row gives a ticker and nothing
else; whether that ticker resolves to a live company, to a delisted security, to
two issuers in sequence, or to nothing at all is a separate question that
`identity_state` records. An unattributed observation is the ordinary case, not a
violation, and a constraint forbidding it would force the importer to invent an
identity for every row — the precise failure the phase exists to avoid.

What is enforced is the half that still holds: **never both**. That is
`ck_listing_observation_one_identity`, an OR rather than an inequality.

## What the unique key means

`(symbol, venue, observation_month)` is what the file actually establishes, so it
is what the table is unique on. That is also what makes ingestion idempotent:
re-reading a month cannot produce a second row for a symbol, and a re-run over
the whole corpus is a no-op.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the listing source and observation tables."""
    op.create_table(
        "listing_source_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=120), nullable=False),
        sa.Column("url", sa.String(length=400), nullable=False),
        sa.Column("source_family", sa.String(length=20), nullable=False),
        sa.Column("expected_tape", sa.String(length=10), nullable=False),
        sa.Column("observed_tape", sa.String(length=10), nullable=False),
        sa.Column("venue", sa.String(length=20), nullable=True),
        sa.Column("observation_month", sa.Date(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("skipped_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "retrieved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_name", name="uq_listing_source_file"),
    )
    op.create_index(
        "ix_listing_source_files_month",
        "listing_source_files",
        ["observation_month", "venue"],
    )

    op.create_table(
        "listing_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("venue", sa.String(length=20), nullable=False),
        sa.Column("observation_month", sa.Date(), nullable=False),
        sa.Column("evidence_state", sa.String(length=30), nullable=False),
        sa.Column("consolidated_volume", sa.Float(), nullable=True),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("historical_security_id", sa.Integer(), nullable=True),
        sa.Column(
            "identity_state", sa.String(length=30), nullable=False, server_default="UNRESOLVED"
        ),
        sa.Column("source_file_id", sa.Integer(), nullable=False),
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
            ["source_file_id"], ["listing_source_files.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "venue", "observation_month", name="uq_listing_observation"),
        # Never both. Deliberately not the Option C inequality — see the module
        # docstring: an observation with neither identity is the ordinary case.
        sa.CheckConstraint(
            "company_id IS NULL OR historical_security_id IS NULL",
            name="ck_listing_observation_one_identity",
        ),
    )
    for name, columns in (
        ("ix_listing_observations_month", ["observation_month", "venue"]),
        ("ix_listing_observations_symbol", ["symbol", "observation_month"]),
        ("ix_listing_observations_company", ["company_id", "observation_month"]),
        ("ix_listing_observations_historical", ["historical_security_id", "observation_month"]),
    ):
        op.create_index(name, "listing_observations", columns)


def downgrade() -> None:
    """Drop both tables.

    Safe in a way `0027`'s downgrade is not: these tables are new, nothing
    references them, and dropping them cannot cascade into data that predates
    this revision.
    """
    op.drop_table("listing_observations")
    op.drop_index("ix_listing_source_files_month", table_name="listing_source_files")
    op.drop_table("listing_source_files")
