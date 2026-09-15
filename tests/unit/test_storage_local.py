"""What is true of the local backend and of no other.

The contract tests say what every backend must do. These say how this one does
it, where that matters: the root it is anchored to, the atomic rename, the temp
file that must not survive a failure, and the two filesystem artefacts — macOS
AppleDouble sidecars and in-flight temporaries — that must never be mistaken
for evidence. None of these concepts exist for an object store, which is why
they are tested here and not in the contract.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from stock_screener.sec_evidence import (
    EvidenceWriteError,
    LocalEvidenceStorage,
    StorageUnavailableError,
    UnsafeStorageKeyError,
    validate_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

pytestmark = pytest.mark.integration

KEY = "filings/0000000001/000000000124000001/doc.htm"


class TestRootAnchoring:
    def test_a_key_resolves_beneath_the_configured_root(self, tmp_path: Path) -> None:
        storage = LocalEvidenceStorage(tmp_path)

        storage.write_bytes(KEY, b"x")

        assert (tmp_path / "filings/0000000001/000000000124000001/doc.htm").read_bytes() == b"x"

    def test_the_root_is_whatever_the_caller_names(self, tmp_path: Path) -> None:
        """No volume, drive or mount point is special to this class."""
        elsewhere = tmp_path / "some" / "other" / "place"
        elsewhere.mkdir(parents=True)
        storage = LocalEvidenceStorage(elsewhere)

        storage.write_bytes(KEY, b"x")

        assert (elsewhere / KEY).is_file()
        assert not (tmp_path / KEY).exists()

    def test_a_root_that_vanished_refuses_to_write(self, tmp_path: Path) -> None:
        """The unmounted-drive case: nothing may appear anywhere else."""
        root = tmp_path / "volume"
        root.mkdir()
        storage = LocalEvidenceStorage(root)
        root.rmdir()

        with pytest.raises(EvidenceWriteError, match=r"write .* did not complete"):
            storage.write_bytes(KEY, b"x")

        assert not root.exists(), "the root must not be recreated"

    def test_the_write_refusal_is_a_storage_unavailable_error(self, tmp_path: Path) -> None:
        """Callers that stop on the backend-neutral type catch the local one too."""
        root = tmp_path / "volume"
        root.mkdir()
        storage = LocalEvidenceStorage(root)
        root.rmdir()

        with pytest.raises(StorageUnavailableError, match="did not complete"):
            storage.write_bytes(KEY, b"x")

    @pytest.mark.parametrize("operation", ["exists", "stat", "read_bytes", "digest", "list_prefix"])
    def test_a_missing_root_is_unavailable_never_absent(
        self, tmp_path: Path, operation: str
    ) -> None:
        storage = LocalEvidenceStorage(tmp_path / "never-created")

        with pytest.raises(StorageUnavailableError, match=r"evidence root .* unavailable"):
            _read_operation(storage, operation)


def _read_operation(storage: LocalEvidenceStorage, operation: str) -> object:
    operations: dict[str, Callable[[], object]] = {
        "exists": lambda: storage.exists(KEY),
        "stat": lambda: storage.stat(KEY),
        "read_bytes": lambda: storage.read_bytes(KEY),
        "digest": lambda: storage.digest(KEY),
        "list_prefix": lambda: list(storage.list_prefix("filings")),
    }
    return operations[operation]()


class TestStorageFailuresAreNotAbsence:
    @pytest.mark.parametrize("operation", ["exists", "stat", "read_bytes", "digest", "list_prefix"])
    @pytest.mark.parametrize(
        "failure", [PermissionError("permission denied"), OSError("I/O failed")]
    )
    def test_metadata_failure_is_unavailable(
        self, tmp_path: Path, operation: str, failure: OSError
    ) -> None:
        storage = LocalEvidenceStorage(tmp_path)
        storage.write_bytes(KEY, b"x")

        with (
            patch.object(Path, "stat", side_effect=failure),
            pytest.raises(StorageUnavailableError, match=str(failure)),
        ):
            _read_operation(storage, operation)

    @pytest.mark.parametrize("operation", ["read_bytes", "digest"])
    def test_read_io_failure_is_unavailable(self, tmp_path: Path, operation: str) -> None:
        storage = LocalEvidenceStorage(tmp_path)
        storage.write_bytes(KEY, b"x")

        with (
            patch.object(Path, "open", side_effect=OSError("read I/O failed")),
            pytest.raises(StorageUnavailableError, match="read I/O failed"),
        ):
            _read_operation(storage, operation)

    def test_listing_failure_in_a_nested_directory_is_not_an_empty_listing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = LocalEvidenceStorage(tmp_path)
        storage.write_bytes(KEY, b"x")
        scandir = os.scandir

        def fail_nested(path: Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
            if path.name == "000000000124000001":
                raise OSError("nested listing unavailable")
            return scandir(path)

        monkeypatch.setattr(os, "scandir", fail_nested)

        with pytest.raises(StorageUnavailableError, match="nested listing unavailable"):
            list(storage.list_prefix("filings"))

    def test_write_io_failure_earns_no_artifact(self, tmp_path: Path) -> None:
        storage = LocalEvidenceStorage(tmp_path)

        with (
            patch.object(os, "fsync", side_effect=OSError("write I/O failed")),
            pytest.raises(EvidenceWriteError, match="write I/O failed"),
        ):
            storage.write_bytes(KEY, b"x")

        assert not storage.exists(KEY)
        assert list(storage.list_prefix("filings")) == []

    @pytest.mark.parametrize("operation", ["exists", "stat", "read_bytes", "digest", "list_prefix"])
    def test_a_symlink_cannot_escape_the_namespace(self, tmp_path: Path, operation: str) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "filings").symlink_to(outside, target_is_directory=True)
        storage = LocalEvidenceStorage(root)

        with pytest.raises(UnsafeStorageKeyError, match="symbolic link"):
            _read_operation(storage, operation)


class TestAtomicWrite:
    def test_a_completed_write_leaves_no_temporary_file(self, tmp_path: Path) -> None:
        storage = LocalEvidenceStorage(tmp_path)

        storage.write_bytes(KEY, b"complete")

        directory = tmp_path / "filings/0000000001/000000000124000001"
        assert [p.name for p in directory.iterdir()] == ["doc.htm"]

    def test_overwriting_replaces_the_whole_artifact(self, tmp_path: Path) -> None:
        """A reader sees the old bytes or the new ones, never a mixture."""
        storage = LocalEvidenceStorage(tmp_path)
        storage.write_bytes(KEY, b"a" * 1000)

        storage.write_bytes(KEY, b"b" * 10)

        assert (tmp_path / KEY).read_bytes() == b"b" * 10

    def test_the_write_creates_only_directories_beneath_the_root(self, tmp_path: Path) -> None:
        storage = LocalEvidenceStorage(tmp_path)

        storage.write_bytes("filings/0000000009/x/deep.htm", b"x")

        assert (tmp_path / "filings" / "0000000009" / "x").is_dir()


class TestListingFiltersLocalArtefacts:
    def test_an_appledouble_sidecar_is_not_listed(self, tmp_path: Path) -> None:
        """macOS writes `._name` beside every file on exFAT. It is never evidence."""
        storage = LocalEvidenceStorage(tmp_path)
        storage.write_bytes(KEY, b"x")
        (tmp_path / "filings/0000000001/000000000124000001/._doc.htm").write_bytes(b"\x00\x05")

        assert list(storage.list_prefix("filings")) == [KEY]

    def test_an_in_flight_temporary_is_not_listed(self, tmp_path: Path) -> None:
        """The `.name.random` file `store_atomically` uses is a write in progress."""
        storage = LocalEvidenceStorage(tmp_path)
        storage.write_bytes(KEY, b"x")
        (tmp_path / "filings/0000000001/000000000124000001/.doc.htm.abc123").write_bytes(b"part")

        assert list(storage.list_prefix("filings")) == [KEY]

    def test_listed_keys_use_forward_slashes_regardless_of_platform(self, tmp_path: Path) -> None:
        storage = LocalEvidenceStorage(tmp_path)
        storage.write_bytes(KEY, b"x")

        (listed,) = storage.list_prefix("")

        assert "/" in listed
        assert "\\" not in listed
        assert listed == KEY

    def test_a_directory_is_never_listed_as_a_key(self, tmp_path: Path) -> None:
        storage = LocalEvidenceStorage(tmp_path)
        storage.write_bytes(KEY, b"x")

        listed = list(storage.list_prefix(""))

        assert listed == [KEY]
        assert "filings" not in listed


class TestKeyValidationIsExplicit:
    @pytest.mark.parametrize(
        ("key", "reason"),
        [
            ("", "empty"),
            ("/abs", "absolute"),
            ("a\\b", "backslash"),
            ("C:/x", "drive-letter"),
            ("a//b", "empty segment"),
            ("a/../b", "dot segment"),
            ("./a", "dot segment"),
            ("a/", "empty segment"),
        ],
    )
    def test_each_unsafe_shape_names_its_reason(self, key: str, reason: str) -> None:
        with pytest.raises(UnsafeStorageKeyError, match=reason):
            validate_key(key)

    def test_a_safe_key_is_returned_unchanged(self) -> None:
        assert validate_key(KEY) == KEY

    def test_validation_never_rewrites_a_key(self) -> None:
        """Rejected, not repaired: a cleaned key names a different artifact."""
        with pytest.raises(UnsafeStorageKeyError, match="dot segment"):
            validate_key("filings/../filings/x.htm")
