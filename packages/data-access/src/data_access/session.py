"""Engine and session construction.

Nothing here is a module-level global. The engine is built from a URL the caller
supplies and handed back, so a test can run against in-memory SQLite while the
same code runs against PostgreSQL in production, and neither has to know about
the other.

PostgreSQL is the intended production database; SQLite is supported so a clone
can run the whole pipeline without provisioning anything. The only place that
difference is visible is the upsert dialect in `repositories`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine


def _is_in_memory_sqlite(url: str) -> bool:
    """Return whether a URL points at an in-memory SQLite database."""
    normalised = url.strip().lower()
    return normalised.startswith("sqlite") and (
        normalised.endswith(("sqlite://", ":memory:")) or "mode=memory" in normalised
    )


def create_engine_from_url(url: str, *, echo: bool = False) -> Engine:
    """Build an engine for a database URL.

    Two SQLite-specific adjustments are made, both because SQLite's defaults are
    wrong for how this application uses it:

    **Foreign keys are enabled.** SQLite ignores them unless asked, which would
    silently disable the `ON DELETE CASCADE` on both child tables and make a
    local run behave differently from production.

    **In-memory databases get a single shared connection.** With the default
    pool, every connection opens its own blank database, so a table created on
    one connection is missing on the next — and the thread check rejects reuse
    across the threadpool a web framework serves sync endpoints from. `StaticPool`
    plus `check_same_thread=False` makes one in-memory database behave like one
    database.

    Args:
        url: A SQLAlchemy database URL.
        echo: Whether to log every statement. Noisy; for debugging only.

    Returns:
        A configured engine. The caller owns disposing of it.
    """
    if _is_in_memory_sqlite(url):
        engine = create_engine(
            url,
            echo=echo,
            future=True,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    else:
        engine = create_engine(url, echo=echo, future=True)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(connection: Any, _record: Any) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to an engine.

    `expire_on_commit` is off so objects stay usable after the transaction
    closes, which keeps the ingestion loop from re-querying a row it just wrote.

    Args:
        engine: The engine sessions should bind to.

    Returns:
        A factory producing new sessions.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a transactional session, committing on success.

    Rolls back and re-raises on any exception, so a half-written ingestion run
    leaves nothing behind. The exception is not logged here — that happens where
    it is handled, once.

    Args:
        factory: The session factory to draw from.

    Yields:
        An open session inside a transaction.
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine) -> None:
    """Create every table directly, bypassing migrations.

    For tests and throwaway local databases only. Real databases are migrated
    with Alembic, because `create_all` has no concept of altering a table that
    already exists and will happily leave a schema half a version behind.

    Args:
        engine: The engine to create tables on.
    """
    from data_access.models import Base

    Base.metadata.create_all(engine)
