"""Let a delisted security carry a market feature and a market signal.

Phase 9B gave a security that left the market its outcomes; Phase 9C gave it
membership. The Phase 10 audit found the chain still broke one step short of a
strategy decision: `pit_feature_snapshots` and `pit_signal_snapshots` are keyed
by `company_id` alone, and a delisted security has no `companies` row.

Measured on staging, that is not a theoretical gap. **10,002 decisions on
historical securities reach a forward outcome, and not one of them can carry a
signal.** Any strategy that filters on a market signal therefore drops the entire
delisted population — silently, at the last step, after four phases of work to
remove exactly that bias. A filter nobody wrote is the worst kind, because
nothing fails.

This revision gives both tables the dual identity the rest of the Phase 9 layers
already use:

    company_id              nullable  → companies.id
    historical_security_id  nullable  → historical_securities.id
    CHECK exactly one populated

No third identity model is invented. The shape, the check constraint's spelling,
and the two partial unique indexes are copied from `forward_returns` (`0027`) and
`universe_membership` (`0028`), because a fourth variation on the same idea would
be one more thing to keep in agreement.

## Why the unique constraint becomes two partial indexes

`UNIQUE (company_id, snapshot_date, feature_version)` over a *nullable*
`company_id` enforces nothing for historical rows: SQL holds every NULL distinct
from every other, so `(NULL, 2023-06-30, MARKET_FEATURES_V1)` could sit beside
itself without limit. The rule is "unique per identity kind", and that is what
two partial indexes say.

## Why the rebuild here is safe

Relaxing `company_id` to nullable needs a SQLite table rebuild. Unlike `0027`'s
rebuild of `historical_securities` — a *parent* with three cascading children,
which needed foreign keys disabled and verified — both tables here are leaves:
they hold foreign keys *to* `pit_capture_runs` and `companies`, and nothing holds
one to them. Dropping a leaf cascades to nothing, so no pragma dance is required
and none is performed.

Values are copied column for column. Every existing company-backed row keeps its
`company_id`, its snapshot date, its version and every computed value, and gains
a NULL `historical_security_id`. **No row is migrated between identities.**

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | None = None
depends_on: str | None = None

#: The two tables, with the version column that completes their uniqueness key.
_TABLES = (
    ("pit_feature_snapshots", "feature_version", "pit_feature", "uq_pit_feature_snapshot"),
    ("pit_signal_snapshots", "signal_version", "pit_signal", "uq_pit_signal_snapshot"),
)


def upgrade() -> None:
    """Open both point-in-time snapshot tables to a historical-security identity."""
    for table, version_column, prefix, old_constraint in _TABLES:
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.add_column(sa.Column("historical_security_id", sa.Integer(), nullable=True))
            batch.alter_column("company_id", existing_type=sa.Integer(), nullable=True)
            batch.drop_constraint(old_constraint, type_="unique")
            batch.create_foreign_key(
                f"fk_{prefix}_historical_security",
                "historical_securities",
                ["historical_security_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch.create_check_constraint(
                f"ck_{prefix}_one_identity",
                "(company_id IS NULL) <> (historical_security_id IS NULL)",
            )

        # Partial, and therefore outside the batch: one index over the nullable
        # pair would enforce nothing at all.
        op.create_index(
            f"uq_{prefix}_snapshot_company",
            table,
            ["company_id", "snapshot_date", version_column],
            unique=True,
            sqlite_where=sa.text("company_id IS NOT NULL"),
            postgresql_where=sa.text("company_id IS NOT NULL"),
        )
        op.create_index(
            f"uq_{prefix}_snapshot_historical",
            table,
            ["historical_security_id", "snapshot_date", version_column],
            unique=True,
            sqlite_where=sa.text("historical_security_id IS NOT NULL"),
            postgresql_where=sa.text("historical_security_id IS NOT NULL"),
        )
        op.create_index(
            f"ix_{table}_historical_date",
            table,
            ["historical_security_id", "snapshot_date"],
        )


def downgrade() -> None:
    """Return both tables to a company-only identity.

    Historical-security rows are deleted rather than reassigned: there is no
    company to attribute them to, and attributing them to one would be the
    identity splice the whole Phase 9 layer exists to prevent.
    """
    for table, version_column, prefix, old_constraint in _TABLES:
        op.drop_index(f"ix_{table}_historical_date", table_name=table)
        op.drop_index(f"uq_{prefix}_snapshot_historical", table_name=table)
        op.drop_index(f"uq_{prefix}_snapshot_company", table_name=table)
        op.execute(sa.text(f"DELETE FROM {table} WHERE company_id IS NULL"))  # noqa: S608
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.drop_constraint(f"ck_{prefix}_one_identity", type_="check")
            batch.drop_constraint(f"fk_{prefix}_historical_security", type_="foreignkey")
            batch.drop_column("historical_security_id")
            batch.alter_column("company_id", existing_type=sa.Integer(), nullable=False)
            batch.create_unique_constraint(
                old_constraint, ["company_id", "snapshot_date", version_column]
            )
