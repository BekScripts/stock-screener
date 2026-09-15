"""Caching a fails-to-deliver archive without damaging the machine or the cache.

Two failures are silent and both are regressions here: a cache directory whose
volume is not mounted must not be conjured onto the boot disk, and a write that
stops halfway must not leave a file the next run accepts as a complete archive.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from api_clients.errors import ProviderDataError
from api_clients.sec_ftd import _store

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

PAYLOAD = b"fails-to-deliver archive bytes"


class TestAnUnmountedVolumeIsNeverCreated:
    def test_a_cache_whose_grandparent_is_missing_is_refused(self, tmp_path: Path) -> None:
        """`/Volumes/BEK/...` while the drive is unplugged must raise, not mkdir."""
        missing = tmp_path / "Volumes" / "BEK" / "stock-screener-data" / "ftd" / "a.zip"

        with pytest.raises(ProviderDataError, match="not mounted"):
            _store(missing, PAYLOAD)

    def test_nothing_is_written_when_the_volume_is_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "Volumes" / "BEK" / "ftd" / "a.zip"

        with pytest.raises(ProviderDataError):
            _store(missing, PAYLOAD)

        assert not (tmp_path / "Volumes").exists()

    def test_a_cache_directory_beside_an_existing_parent_is_created(self, tmp_path: Path) -> None:
        """One missing level is an ordinary first run, not an absent drive."""
        destination = tmp_path / "ftd" / "a.zip"

        _store(destination, PAYLOAD)

        assert destination.read_bytes() == PAYLOAD


class TestAnArchiveIsStoredWholeOrNotAtAll:
    def test_the_stored_bytes_round_trip(self, tmp_path: Path) -> None:
        destination = tmp_path / "a.zip"

        _store(destination, PAYLOAD)

        assert hashlib.sha256(destination.read_bytes()).hexdigest() == (
            hashlib.sha256(PAYLOAD).hexdigest()
        )

    def test_a_failed_write_leaves_no_partial_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An interrupted store must leave nothing the size check would accept."""
        destination = tmp_path / "a.zip"

        def explode(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("api_clients.sec_ftd.os.fsync", explode)

        with pytest.raises(OSError, match="disk full"):
            _store(destination, PAYLOAD)

        assert not destination.exists()
        assert list(tmp_path.iterdir()) == []

    def test_a_second_store_replaces_the_first(self, tmp_path: Path) -> None:
        destination = tmp_path / "a.zip"
        _store(destination, PAYLOAD)

        _store(destination, b"newer bytes")

        assert destination.read_bytes() == b"newer bytes"
        assert len(list(tmp_path.iterdir())) == 1
