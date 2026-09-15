"""Where SEC artifacts go, and what happens when the drive is not there.

The regressions that matter are the silent ones: an unmounted volume becoming a
directory tree on the boot disk, and a half-written file that the next run
mistakes for a complete artifact.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from stock_screener.config import Settings
from stock_screener.evidence import EvidenceUnavailableError
from stock_screener.sec_evidence import (
    SUBDIRECTORIES,
    EvidenceWriteError,
    digest_of,
    open_evidence_root,
    store_atomically,
)
from stock_screener.sec_evidence.storage import EvidenceRoot

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

PAYLOAD = b"<xml>a filing document</xml>"


def settings_for(root: str | None) -> Settings:
    """Settings naming one evidence root, built directly rather than from .env."""
    return Settings(sec_evidence_dir=root)


class TestTheRootIsNeverCreated:
    def test_an_unset_root_is_refused(self) -> None:
        with pytest.raises(EvidenceUnavailableError, match="not set"):
            open_evidence_root(settings_for(None))

    def test_a_missing_root_is_refused_and_not_created(self, tmp_path: Path) -> None:
        """The unmounted-drive case: nothing may appear on the local disk."""
        missing = tmp_path / "Volumes" / "BEK" / "stock-screener-data" / "sec"

        with pytest.raises(EvidenceUnavailableError):
            open_evidence_root(settings_for(str(missing)))

        assert not (tmp_path / "Volumes").exists()

    def test_a_file_is_not_a_root(self, tmp_path: Path) -> None:
        target = tmp_path / "sec"
        target.write_text("not a directory")

        with pytest.raises(EvidenceUnavailableError):
            open_evidence_root(settings_for(str(target)))

    def test_an_existing_root_gains_its_subdirectories(self, tmp_path: Path) -> None:
        """Creating beneath a verified root is safe: the root proves the mount."""
        root = tmp_path / "sec"
        root.mkdir()

        opened = open_evidence_root(settings_for(str(root)))

        assert all((opened.path / name).is_dir() for name in SUBDIRECTORIES)

    def test_reopening_an_existing_root_is_a_no_op(self, tmp_path: Path) -> None:
        root = tmp_path / "sec"
        root.mkdir()
        open_evidence_root(settings_for(str(root)))

        again = open_evidence_root(settings_for(str(root)))

        assert all((again.path / name).is_dir() for name in SUBDIRECTORIES)


class TestTheLayoutIsDeterministic:
    def test_a_filing_path_uses_the_sec_archive_form(self, tmp_path: Path) -> None:
        """Padded CIK, dashless accession — the path reads back to a URL."""
        root = EvidenceRoot(path=tmp_path)

        path = root.filing_path(886158, "0000886158-23-000044", "bbby-20230502.htm")

        assert (
            path == tmp_path / "filings" / "0000886158" / "000088615823000044" / "bbby-20230502.htm"
        )

    def test_a_submissions_path_is_padded(self, tmp_path: Path) -> None:
        assert EvidenceRoot(path=tmp_path).submissions_path(320193).name == "CIK0000320193.json"

    def test_a_document_name_cannot_escape_its_directory(self, tmp_path: Path) -> None:
        root = EvidenceRoot(path=tmp_path)

        with pytest.raises(ValueError, match="usable SEC document name"):
            root.filing_path(1, "0000000000-00-000000", "../../etc/passwd")

    def test_a_fulltext_path_digests_the_query(self, tmp_path: Path) -> None:
        """A query carries slashes and quotes; a digest carries none."""
        path = EvidenceRoot(path=tmp_path).fulltext_path('"exchange transfer" AND 8-K')

        assert path.parent == tmp_path / "fulltext"
        assert path.suffix == ".json"


class TestAnArtifactIsWholeOrAbsent:
    def test_stored_bytes_round_trip(self, tmp_path: Path) -> None:
        destination = tmp_path / "filings" / "x" / "doc.htm"

        written = store_atomically(destination, PAYLOAD, root=tmp_path)

        assert written == len(PAYLOAD)
        assert digest_of(destination) == hashlib.sha256(PAYLOAD).hexdigest()

    def test_an_interrupted_write_leaves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = tmp_path / "doc.htm"

        def explode(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("stock_screener.sec_evidence.storage.os.fsync", explode)

        with pytest.raises(OSError, match="disk full"):
            store_atomically(destination, PAYLOAD, root=tmp_path)

        assert not destination.exists()
        assert list(tmp_path.iterdir()) == []

    def test_a_root_that_vanished_mid_run_is_refused(self, tmp_path: Path) -> None:
        """The drive unmounted after the root was verified. Nothing is rebuilt."""
        root = tmp_path / "sec"
        root.mkdir()
        destination = root / "filings" / "doc.htm"
        root.rmdir()

        with pytest.raises(EvidenceWriteError, match="unmounted"):
            store_atomically(destination, PAYLOAD, root=root)

        assert not root.exists()

    def test_a_destination_outside_the_root_is_refused(self, tmp_path: Path) -> None:
        """`/Volumes` always exists, so "nearest existing parent" is no guard."""
        root = tmp_path / "sec"
        root.mkdir()

        with pytest.raises(EvidenceWriteError, match="outside the evidence root"):
            store_atomically(tmp_path / "elsewhere" / "doc.htm", PAYLOAD, root=root)

        assert not (tmp_path / "elsewhere").exists()

    def test_a_rewrite_replaces_rather_than_duplicates(self, tmp_path: Path) -> None:
        destination = tmp_path / "doc.htm"
        store_atomically(destination, PAYLOAD, root=tmp_path)

        store_atomically(destination, b"newer", root=tmp_path)

        assert destination.read_bytes() == b"newer"
        assert len(list(tmp_path.iterdir())) == 1

    def test_the_digest_of_a_missing_file_is_none(self, tmp_path: Path) -> None:
        assert digest_of(tmp_path / "absent") is None
