"""Give a security an identity that outlives every name it trades under.

Migration `0026` keyed a historical security by CIK, on the reasoning that a CIK
survives a ticker change and separates a reused ticker from its predecessor. That
reasoning holds; the problem is availability. The free record establishes a CIK
for about an eighth of delisted symbols, so a NOT NULL CIK made the table unable
to hold the names it exists to hold — a schema requiring evidence nobody
publishes.

The remedy is not to promote a different external identifier in its place. CUSIP
is the obvious candidate and is the wrong answer: it identifies an *issue*, not a
lineage, and a reverse split retires one CUSIP and mints another over unchanged
economics, so keying on it splits one company's history in two exactly where a
study most needs it whole.

So the key is `historical_securities.id`, an opaque internal number, and every
external handle becomes a **dated interval** against it:

    historical_securities.id
        ├── security_identifiers  (cusip, ticker, venue, valid_from, valid_to)
        ├── cik                   (still unique where known, now nullable)
        └── historical_* children (observations, prices, corporate actions)

Three rules fall out, and they are what the tests hold:

- a ticker change **preserves** the id — one interval closes, another opens;
- a ticker reused by a different issuer takes a **different** id — no join
  between them exists to be made;
- a reverse split **preserves** the id — a new CUSIP is a new interval, never a
  new security.

This revision also lands the evidence the intervals are built from. The SEC's
fails-to-deliver files are the only free record pairing a CUSIP with a symbol
*on a date* back to 2004, and `ftd_identity_observations` holds one row per
distinct triple with the window it was seen over, `ftd_source_files` the digest
of every archive read. Evidence is kept apart from conclusions because evidence
also exists for symbols that resolved to nothing, and those are the rows an audit
of what is missing is made of.

Finally, `forward_returns` learns the second identity. It could only ever be
measured against a live company, which is the survivorship bias in the outcome
half of the record: the securities whose outcomes matter most are the ones that
left. `company_id` becomes nullable, `historical_security_id` joins it, and a
check constraint requires exactly one. The uniqueness rule is replaced by two
partial unique indexes — one per identity kind — because a single constraint over
a nullable pair enforces nothing, SQL holding every NULL distinct from every
other.

**No row is moved.** Every existing measurement keeps its `company_id` and its
values; `historical_security_id` is NULL on all of them. Migrating a delisted
name's history is a data operation that needs its identity proven first, and it
is deliberately not done here.

## On rebuilding `historical_securities` safely

Making `cik` nullable on SQLite needs a table rebuild — create, copy, drop,
rename — and `historical_securities` has three children declaring
`ON DELETE CASCADE`. Dropping a parent under enforced foreign keys is precisely
what migration `0017` did to `companies`, deleting about 1.6 million rows.

Foreign keys are therefore disabled for the rebuild, which is what SQLite's own
documentation prescribes for this procedure. The subtlety that makes it dangerous
is that `PRAGMA foreign_keys` is **silently ignored inside a transaction**, and
Alembic has usually opened one by the time a migration body runs — so the naive
version of this appears to work, does nothing, and cascades. `_disable_foreign_keys`
commits at the driver level first and then *verifies* the pragma took effect,
raising rather than proceeding if it did not. A migration that cannot make itself
safe must fail loudly, not carry on hopefully.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | None = None
depends_on: str | None = None


def _disable_foreign_keys(bind: Connection) -> bool:
    """Turn off SQLite foreign-key enforcement for a table rebuild, or refuse.

    `PRAGMA foreign_keys` is a no-op inside an open transaction, and Alembic has
    normally written a version stamp — a DML write — before a migration body
    runs. The driver-level commit ends that transaction so the pragma can take;
    the read-back proves that it did.

    Args:
        bind: The connection the migration is running on.

    Returns:
        True when enforcement was disabled and must be restored, False on a
        dialect where none of this applies.

    Raises:
        RuntimeError: If enforcement could not be disabled on SQLite. Rebuilding
            `historical_securities` with it on would cascade-delete every
            historical observation, price and corporate action.
    """
    if bind.dialect.name != "sqlite":
        return False
    raw = bind.connection.dbapi_connection
    if raw is None:  # pragma: no cover — defensive; pysqlite always exposes one
        message = "no DBAPI connection available to disable foreign keys on"
        raise RuntimeError(message)
    raw.commit()
    raw.execute("PRAGMA foreign_keys=OFF")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
        message = (
            "could not disable SQLite foreign-key enforcement; rebuilding "
            "historical_securities would cascade-delete its observations, prices "
            "and corporate actions. Refusing to continue."
        )
        raise RuntimeError(message)
    return True


def _restore_foreign_keys(bind: Connection, *, disabled: bool) -> None:
    """Turn enforcement back on after a rebuild, if this migration turned it off."""
    if not disabled:
        return
    raw = bind.connection.dbapi_connection
    if raw is None:  # pragma: no cover — defensive
        return
    raw.commit()
    raw.execute("PRAGMA foreign_keys=ON")


def _relax_historical_security_cik() -> None:
    """Make `historical_securities.cik` nullable, keeping it unique where present.

    The unique constraint stays exactly as it was. SQL holds every NULL distinct
    from every other, so `UNIQUE (cik)` continues to forbid two rows claiming one
    filer while permitting any number of rows that establish no filer at all —
    which is the behaviour this change needs and the reason no partial index is
    required here.
    """
    bind = op.get_bind()
    disabled = _disable_foreign_keys(bind)
    try:
        with op.batch_alter_table("historical_securities", recreate="always") as batch:
            batch.alter_column("cik", existing_type=sa.Integer(), nullable=True)
    finally:
        _restore_foreign_keys(bind, disabled=disabled)


def _restore_historical_security_cik() -> None:
    """Put the NOT NULL back, for the downgrade.

    Rows with no CIK cannot survive it — there is nothing to put in the column
    and inventing a filer number is the one thing this layer exists not to do —
    so they are deleted, and their identifier intervals cascade with them.
    """
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM historical_securities WHERE cik IS NULL"))
    disabled = _disable_foreign_keys(bind)
    try:
        with op.batch_alter_table("historical_securities", recreate="always") as batch:
            batch.alter_column("cik", existing_type=sa.Integer(), nullable=False)
    finally:
        _restore_foreign_keys(bind, disabled=disabled)


def _add_identity_tables() -> None:
    """Create the dated identifier table and the evidence behind it."""
    op.create_table(
        "security_identifiers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("historical_security_id", sa.Integer(), nullable=False),
        sa.Column("cusip", sa.String(length=9), nullable=True),
        sa.Column("ticker", sa.String(length=20), nullable=True),
        sa.Column("venue", sa.String(length=50), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.Column("lineage", sa.String(length=30), nullable=False),
        sa.Column("issuer_name", sa.String(length=255), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=True),
        sa.Column("evidence_hash", sa.String(length=64), nullable=True),
        sa.Column("source_file", sa.String(length=120), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cusip IS NOT NULL OR ticker IS NOT NULL",
            name="ck_security_identifier_has_an_identifier",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_security_identifier_range",
        ),
        sa.ForeignKeyConstraint(
            ["historical_security_id"], ["historical_securities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "historical_security_id",
            "cusip",
            "ticker",
            "venue",
            "valid_from",
            name="uq_security_identifier",
        ),
    )
    op.create_index(
        "ix_security_identifiers_cusip", "security_identifiers", ["cusip", "valid_from"]
    )
    op.create_index(
        "ix_security_identifiers_ticker", "security_identifiers", ["ticker", "valid_from"]
    )
    op.create_index(
        "ix_security_identifiers_security",
        "security_identifiers",
        ["historical_security_id", "valid_from"],
    )

    op.create_table(
        "ftd_source_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=120), nullable=False),
        sa.Column("url", sa.String(length=400), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("skipped_lines", sa.Integer(), nullable=False),
        sa.Column("first_settlement_date", sa.Date(), nullable=True),
        sa.Column("last_settlement_date", sa.Date(), nullable=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_name", name="uq_ftd_source_file"),
    )
    op.create_index(
        "ix_ftd_source_files_dates",
        "ftd_source_files",
        ["first_settlement_date", "last_settlement_date"],
    )

    op.create_table(
        "ftd_identity_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cusip", sa.String(length=9), nullable=False),
        sa.Column("issuer_base", sa.String(length=6), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("issuer_name", sa.String(length=255), nullable=False),
        sa.Column("first_settlement_date", sa.Date(), nullable=False),
        sa.Column("last_settlement_date", sa.Date(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("cusip_valid", sa.Boolean(), nullable=False),
        sa.Column("first_source_file", sa.String(length=120), nullable=False),
        sa.Column("last_source_file", sa.String(length=120), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cusip", "symbol", "issuer_name", name="uq_ftd_identity"),
    )
    op.create_index("ix_ftd_identity_symbol", "ftd_identity_observations", ["symbol"])
    op.create_index("ix_ftd_identity_cusip", "ftd_identity_observations", ["cusip"])
    op.create_index("ix_ftd_identity_issuer_base", "ftd_identity_observations", ["issuer_base"])


def _open_forward_returns_to_historical_securities() -> None:
    """Let a forward return belong to a delisted security as well as a company.

    `forward_returns` is rebuilt rather than extended, because `company_id` has
    to lose its NOT NULL and SQLite cannot relax one in place. The rebuild is
    safe in a way the parent's is not: nothing has a foreign key *to*
    `forward_returns`, so dropping it cascades to nothing. Values are copied
    column for column and no row's meaning changes.
    """
    with op.batch_alter_table("forward_returns", recreate="always") as batch:
        batch.add_column(sa.Column("historical_security_id", sa.Integer(), nullable=True))
        batch.alter_column("company_id", existing_type=sa.Integer(), nullable=True)
        batch.drop_constraint("uq_forward_return", type_="unique")
        batch.create_foreign_key(
            "fk_forward_return_historical_security",
            "historical_securities",
            ["historical_security_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_check_constraint(
            "ck_forward_return_one_identity",
            "(company_id IS NULL) <> (historical_security_id IS NULL)",
        )

    # Partial, and therefore created outside the batch: the rule is "unique per
    # identity kind", and one index spanning both nullable columns would enforce
    # nothing at all, since SQL holds every NULL distinct from every other.
    op.create_index(
        "uq_forward_return_company",
        "forward_returns",
        ["company_id", "observation_date", "horizon_sessions"],
        unique=True,
        sqlite_where=sa.text("company_id IS NOT NULL"),
        postgresql_where=sa.text("company_id IS NOT NULL"),
    )
    op.create_index(
        "uq_forward_return_historical",
        "forward_returns",
        ["historical_security_id", "observation_date", "horizon_sessions"],
        unique=True,
        sqlite_where=sa.text("historical_security_id IS NOT NULL"),
        postgresql_where=sa.text("historical_security_id IS NOT NULL"),
    )


def upgrade() -> None:
    """Add the identity layer and open forward returns to both identity kinds."""
    _relax_historical_security_cik()
    _add_identity_tables()
    _open_forward_returns_to_historical_securities()


def downgrade() -> None:
    """Remove the identity layer and return forward returns to companies alone.

    Measurements against a historical security are deleted: there is no company
    to attribute them to, and attributing them to one would be the splice this
    whole layer exists to prevent. Securities with no CIK go the same way, for
    the same reason.
    """
    op.drop_index("uq_forward_return_historical", table_name="forward_returns")
    op.drop_index("uq_forward_return_company", table_name="forward_returns")
    op.execute(sa.text("DELETE FROM forward_returns WHERE company_id IS NULL"))
    with op.batch_alter_table("forward_returns", recreate="always") as batch:
        batch.drop_constraint("ck_forward_return_one_identity", type_="check")
        batch.drop_constraint("fk_forward_return_historical_security", type_="foreignkey")
        batch.drop_column("historical_security_id")
        batch.alter_column("company_id", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint(
            "uq_forward_return", ["company_id", "observation_date", "horizon_sessions"]
        )

    op.drop_index("ix_ftd_identity_issuer_base", table_name="ftd_identity_observations")
    op.drop_index("ix_ftd_identity_cusip", table_name="ftd_identity_observations")
    op.drop_index("ix_ftd_identity_symbol", table_name="ftd_identity_observations")
    op.drop_table("ftd_identity_observations")
    op.drop_index("ix_ftd_source_files_dates", table_name="ftd_source_files")
    op.drop_table("ftd_source_files")
    op.drop_index("ix_security_identifiers_security", table_name="security_identifiers")
    op.drop_index("ix_security_identifiers_ticker", table_name="security_identifiers")
    op.drop_index("ix_security_identifiers_cusip", table_name="security_identifiers")
    op.drop_table("security_identifiers")

    _restore_historical_security_cik()
