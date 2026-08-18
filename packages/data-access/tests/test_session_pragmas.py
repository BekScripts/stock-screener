"""The SQLite pragmas an engine applies, and which databases get them.

WAL is what lets the API keep serving while a market-wide ingest holds a write
transaction. In the default journal mode that ingest locks the whole database
and every dashboard read fails rather than waits, so this is load-bearing rather
than tuning — and it is applied to file-backed databases only, because WAL needs
real files and an in-memory database has no second writer to be isolated from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from data_access import create_engine_from_url

if TYPE_CHECKING:
    from pathlib import Path


def _pragma(url: str, name: str) -> str:
    """Return one pragma's value from a fresh connection to `url`."""
    engine = create_engine_from_url(url)
    try:
        with engine.connect() as connection:
            return str(connection.execute(text(f"PRAGMA {name}")).scalar()).lower()
    finally:
        engine.dispose()


@pytest.mark.unit
def test_a_file_backed_database_runs_in_wal_mode(tmp_path: Path) -> None:
    assert _pragma(f"sqlite:///{tmp_path / 'radar.db'}", "journal_mode") == "wal"


@pytest.mark.unit
def test_an_in_memory_database_is_left_in_its_default_journal_mode() -> None:
    # WAL cannot apply to a database with no file behind it. Asking for it there
    # would either fail or silently do nothing, and tests would be running
    # against a configuration production never uses.
    assert _pragma("sqlite:///:memory:", "journal_mode") != "wal"


@pytest.mark.unit
def test_a_file_backed_database_waits_rather_than_raising_on_a_locked_moment(
    tmp_path: Path,
) -> None:
    # A checkpoint takes the write lock briefly. Without a busy timeout a reader
    # that arrives during it gets "database is locked" instead of its rows.
    assert int(_pragma(f"sqlite:///{tmp_path / 'radar.db'}", "busy_timeout")) == 5000


@pytest.mark.unit
@pytest.mark.parametrize("url", ["sqlite:///:memory:", "sqlite://"])
def test_foreign_keys_are_enforced_everywhere(url: str) -> None:
    # Unlike WAL this is not conditional: a cascade that works on disk and not
    # in a test would make the tests the wrong shape.
    assert _pragma(url, "foreign_keys") == "1"
