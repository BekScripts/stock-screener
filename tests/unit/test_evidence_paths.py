"""Refusing to guess where the SEC evidence corpus is.

The corpus lives on external storage, so *absence* is an ordinary runtime
condition. These tests pin the behaviour that makes absence safe: the root is
never created, the four ways it can be unusable stay distinct, and an unmounted
volume raises instead of quietly becoming an empty cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from stock_screener.config import Settings
from stock_screener.evidence import (
    EvidenceState,
    EvidenceUnavailableError,
    inspect_sec_evidence_dir,
    resolve_sec_evidence_dir,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def settings_with(value: str | None) -> Settings:
    """Settings carrying one evidence root and nothing else that matters."""
    return Settings(sec_evidence_dir=value)


class TestTheFourStatesStayDistinct:
    """Each calls for a different action, so none may be collapsed into another."""

    def test_unset_is_unconfigured(self) -> None:
        state, path = inspect_sec_evidence_dir(settings_with(None))

        assert state is EvidenceState.UNCONFIGURED
        assert path is None

    def test_blank_is_unconfigured_rather_than_a_relative_path(self) -> None:
        state, _ = inspect_sec_evidence_dir(settings_with("   "))

        assert state is EvidenceState.UNCONFIGURED

    def test_a_missing_path_is_unavailable(self, tmp_path: Path) -> None:
        state, _ = inspect_sec_evidence_dir(settings_with(str(tmp_path / "not-mounted")))

        assert state is EvidenceState.UNAVAILABLE

    def test_a_file_is_not_a_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "evidence"
        target.write_text("oops")

        state, _ = inspect_sec_evidence_dir(settings_with(str(target)))

        assert state is EvidenceState.NOT_A_DIRECTORY

    def test_an_existing_but_empty_root_is_empty_not_unavailable(self, tmp_path: Path) -> None:
        """A corpus not yet harvested is not the same as a drive not plugged in."""
        root = tmp_path / "sec"
        root.mkdir()

        state, _ = inspect_sec_evidence_dir(settings_with(str(root)))

        assert state is EvidenceState.EMPTY

    def test_a_populated_root_is_available(self, tmp_path: Path) -> None:
        root = tmp_path / "sec"
        root.mkdir()
        (root / "filing.html").write_text("<html/>")

        state, resolved = inspect_sec_evidence_dir(settings_with(str(root)))

        assert state is EvidenceState.AVAILABLE
        assert resolved == root


class TestTheRootIsNeverCreated:
    """The failure this module exists to prevent."""

    def test_resolving_a_missing_root_does_not_create_it(self, tmp_path: Path) -> None:
        missing = tmp_path / "not-mounted" / "sec"

        with pytest.raises(EvidenceUnavailableError):
            resolve_sec_evidence_dir(settings_with(str(missing)))

        assert not missing.exists()
        assert not missing.parent.exists()

    def test_resolving_when_unconfigured_creates_nothing_anywhere(self) -> None:
        with pytest.raises(EvidenceUnavailableError) as caught:
            resolve_sec_evidence_dir(settings_with(None))

        assert caught.value.state is EvidenceState.UNCONFIGURED
        assert caught.value.path is None


class TestTheErrorSaysWhichFailureItIs:
    def test_an_unmounted_volume_is_reported_as_such(self, tmp_path: Path) -> None:
        with pytest.raises(EvidenceUnavailableError) as caught:
            resolve_sec_evidence_dir(settings_with(str(tmp_path / "gone")))

        assert caught.value.state is EvidenceState.UNAVAILABLE
        assert "not mounted" in str(caught.value)

    def test_being_unconfigured_names_the_setting_and_why_it_has_no_default(self) -> None:
        with pytest.raises(EvidenceUnavailableError) as caught:
            resolve_sec_evidence_dir(settings_with(None))

        message = str(caught.value)
        assert "SEC_EVIDENCE_DIR" in message
        assert "boot disk" in message

    def test_an_empty_root_is_refused_for_a_reader_but_allowed_for_a_harvest(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "sec"
        root.mkdir()

        assert resolve_sec_evidence_dir(settings_with(str(root)), allow_empty=True) == root
        with pytest.raises(EvidenceUnavailableError) as caught:
            resolve_sec_evidence_dir(settings_with(str(root)), allow_empty=False)
        assert caught.value.state is EvidenceState.EMPTY


class TestThereIsNoSilentFallback:
    def test_the_setting_has_no_default(self) -> None:
        """A default is what would make a harvest target the boot disk."""
        assert Settings().sec_evidence_dir is None

    def test_no_state_resolves_to_a_local_path_of_its_own(self, tmp_path: Path) -> None:
        """Every failing state raises; none returns a substitute directory."""
        for value in (None, "", str(tmp_path / "missing")):
            with pytest.raises(EvidenceUnavailableError):
                resolve_sec_evidence_dir(settings_with(value))
