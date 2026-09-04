"""Record which securities were in the research universe, and when.

`universe_events` answers whether a `companies` row was flagged active on the day
the capture pass looked at it. Two properties make it unable to carry historical
membership, and neither is a defect in it — it was built to accumulate forward,
and it does:

- **It is keyed by `company_id`.** A delisted security has no company row, so the
  securities a survivorship-aware universe most needs are the ones it structurally
  cannot hold. Measured on staging: 5,769 rows, every one of them `BASELINE`,
  every one dated the day capture first ran.
- **It is an observation log.** Its contract is that its rows were witnessed. A
  derived verdict stored in it would be indistinguishable from a sighting, which
  is exactly the confusion the whole point-in-time layer exists to prevent.

So this revision adds a second, additive table rather than widening the first.
`universe_membership` holds **effective-dated verdicts** — this security was, or
was not, in the universe over this span — with the confidence behind each. Its
identity is dual, exactly as `forward_returns` has been since `0027`:
`company_id` **or** `historical_security_id`, exactly one, enforced by a check
constraint and unique per kind through two partial indexes. That is what lets an
ordinary universe query return delisted names without a caller asking for them.

## Why `confidence` is a column and not a comment

The project's universe is defined by `evaluate_eligibility`, and its inputs are
not uniformly dated. Price, market capitalisation and liquidity are recoverable
point-in-time. **Exchange, security type and sector are not** — every stored
exchange in this database carries today's date, `security_identifiers.venue` is
NULL on all 3,147 rows, and the fails-to-deliver record carries no venue at all.

A reconstruction therefore cannot produce an *observed* membership for any
security, and the honest thing is to say so per row rather than once in a
document. `confidence` distinguishes a verdict whose every input is dated from
one that carried an undated attribute backwards under a documented rule, and
`inferred_inputs` names which attributes were carried. A study that will only
trust dated evidence filters on it; a reconstruction cannot promote an inference
by accident, because promoting it means writing a different value into a column.

Additive: one new table, no existing table touched, no `batch_alter_table`, so
nothing is rebuilt and nothing cascades.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the effective-dated membership table."""
    op.create_table(
        "universe_membership",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("historical_security_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("inferred_inputs", sa.String(length=120), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(company_id IS NULL) <> (historical_security_id IS NULL)",
            name="ck_universe_membership_one_identity",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_universe_membership_range",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["historical_security_id"], ["historical_securities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Partial, and therefore created outside the table definition: one index
    # spanning both nullable identity columns would enforce nothing, SQL holding
    # every NULL distinct from every other.
    op.create_index(
        "uq_universe_membership_company",
        "universe_membership",
        ["company_id", "valid_from"],
        unique=True,
        sqlite_where=sa.text("company_id IS NOT NULL"),
        postgresql_where=sa.text("company_id IS NOT NULL"),
    )
    op.create_index(
        "uq_universe_membership_historical",
        "universe_membership",
        ["historical_security_id", "valid_from"],
        unique=True,
        sqlite_where=sa.text("historical_security_id IS NOT NULL"),
        postgresql_where=sa.text("historical_security_id IS NOT NULL"),
    )
    op.create_index(
        "ix_universe_membership_window", "universe_membership", ["valid_from", "valid_to"]
    )
    op.create_index("ix_universe_membership_state", "universe_membership", ["state", "valid_from"])


def downgrade() -> None:
    """Drop the membership table, indexes first."""
    op.drop_index("ix_universe_membership_state", table_name="universe_membership")
    op.drop_index("ix_universe_membership_window", table_name="universe_membership")
    op.drop_index("uq_universe_membership_historical", table_name="universe_membership")
    op.drop_index("uq_universe_membership_company", table_name="universe_membership")
    op.drop_table("universe_membership")
