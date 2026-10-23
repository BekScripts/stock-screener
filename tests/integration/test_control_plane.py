"""The control plane survives the artifact plane, and says so when it cannot.

Phase 9L.6 lost an external drive holding 19,350 filings and, beside them, the
manifest, checkpoints and plan that were the only record of what those filings
had been. Phase B2.5 gives that metadata a root of its own. These tests are
the proof, and they are arranged as the two halves of the failure-domain
question:

- **Artifact root gone, control root healthy** — the manifest still reads, the
  checkpoint still loads, the plan is still there, and every artifact
  operation fails loudly rather than pretending. That is the BEK scenario, and
  after this phase it is recoverable.
- **Control root gone, artifact root healthy** — nothing invents an empty
  manifest, assumes zero checkpoints, or restarts a sweep from the first
  batch. It stops.

The legacy colocated layout — everything beneath `SEC_EVIDENCE_DIR` — is
retained for the corpora that already use it, and is tested here as exactly
what it is: operable, and one failure domain.

Nothing reaches the network. The SEC client is a fake that serves one
document and counts its calls.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from api_clients.sec_documents import FetchedDocument
from stock_screener.cli import app
from stock_screener.config import Settings, get_settings
from stock_screener.sec_evidence import (
    CONTROL_SUBDIRECTORIES,
    Budget,
    Checkpoint,
    ChunkStage,
    ControlPlaneUnavailableError,
    ControlState,
    EvidenceControlRoot,
    LocalEvidenceStorage,
    Manifest,
    RequestKind,
    StorageUnavailableError,
    SweepError,
    Target,
    build_index,
    checkpoint_path,
    derived_root,
    filing_key,
    fingerprint_corpus,
    harvest,
    harvest_artifacts,
    inspect_control_dir,
    load_checkpoint,
    load_fingerprint,
    open_control_root,
    open_evidence_root,
    preflight_corpus,
    reconcile,
    same_volume,
    save_checkpoint,
    save_fingerprint,
)
from stock_screener.sec_evidence.sweep import LocalCapacity
from stock_screener.sec_evidence.targets import load_manifest

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import MemoryEvidenceStorage
    from stock_screener.sec_evidence import EvidenceRoot

pytestmark = pytest.mark.integration

COVER = (
    b'<html><body><ix:nonNumeric contextRef="c1" name="dei:Security12bTitle">Common Stock'
    b'</ix:nonNumeric><ix:nonNumeric contextRef="c1" name="dei:TradingSymbol">AAPL'
    b'</ix:nonNumeric><ix:nonNumeric contextRef="c1" name="dei:SecurityExchangeName">'
    b"The Nasdaq Stock Market LLC</ix:nonNumeric></body></html>"
)

FILING = Target(
    url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
    kind=RequestKind.FILING,
    cik=320193,
    accession="0000320193-24-000123",
    document="aapl-20240928.htm",
    form="10-K",
    filing_date="2024-11-01",
)
FILING_KEY = filing_key(320193, "0000320193-24-000123", "aapl-20240928.htm")


class Source:
    """A SEC client that serves one fixed document and counts its calls."""

    def __init__(self, payload: bytes = COVER) -> None:
        self.payload = payload
        self.calls = 0

    def fetch(self, url: str) -> FetchedDocument:
        self.calls += 1
        return FetchedDocument(
            url=url,
            status=200,
            body=self.payload,
            sha256=hashlib.sha256(self.payload).hexdigest(),
            content_type="text/html",
            etag=None,
            last_modified=None,
            retrieved_at=datetime(2026, 9, 15, tzinfo=UTC),
        )

    def close(self) -> None:
        return None


def budget() -> Budget:
    return Budget(max_requests=10, max_bytes=10**6)


def remove_tree(path: Path) -> None:
    """Delete a directory tree — the volume going away, as far as the code can tell."""
    for child in sorted(path.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    path.rmdir()


@pytest.fixture
def artifact_root(tmp_path: Path) -> Path:
    root = tmp_path / "volume" / "sec"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def control_dir(tmp_path: Path) -> Path:
    root = tmp_path / "boot" / "control"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def separated(artifact_root: Path, control_dir: Path) -> tuple[EvidenceRoot, EvidenceControlRoot]:
    """The B2.5 layout: artifacts on one root, control plane on another."""
    settings = Settings(sec_evidence_dir=str(artifact_root), sec_control_dir=str(control_dir))
    root = open_evidence_root(settings)
    return root, open_control_root(settings, colocate_with=root)


class TestControlRootResolution:
    def test_an_unset_setting_is_unconfigured_with_no_path(self) -> None:
        assert inspect_control_dir(Settings(sec_control_dir=None)) == (
            ControlState.UNCONFIGURED,
            None,
        )
        assert inspect_control_dir(Settings(sec_control_dir="   "))[0] is ControlState.UNCONFIGURED

    def test_a_missing_directory_is_unavailable_and_is_not_created(self, tmp_path: Path) -> None:
        absent = tmp_path / "Volumes" / "BOOT" / "control"

        state, path = inspect_control_dir(Settings(sec_control_dir=str(absent)))
        with pytest.raises(ControlPlaneUnavailableError, match="never created"):
            open_control_root(Settings(sec_control_dir=str(absent)))

        assert state is ControlState.UNAVAILABLE
        assert path == absent
        assert not (tmp_path / "Volumes").exists()

    def test_a_file_at_the_path_is_not_a_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "control"
        target.write_text("not a directory")

        state, _ = inspect_control_dir(Settings(sec_control_dir=str(target)))
        with pytest.raises(ControlPlaneUnavailableError, match="not a directory"):
            open_control_root(Settings(sec_control_dir=str(target)))

        assert state is ControlState.NOT_A_DIRECTORY

    def test_an_existing_directory_opens_and_gains_its_layout(self, control_dir: Path) -> None:
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))

        assert control.path == control_dir
        assert not control.colocated
        assert all((control_dir / name).is_dir() for name in CONTROL_SUBDIRECTORIES)
        assert control.manifest_path == control_dir / "manifests" / "evidence.jsonl"
        assert control.checkpoints == control_dir / "checkpoints"
        assert control.plans == control_dir / "plans"
        assert control.migration == control_dir / "migration"
        assert control.derived == control_dir / "derived"

    def test_only_the_required_subdirectories_are_created_on_open(self, control_dir: Path) -> None:
        """Plans, migration and derived appear when something writes them, not before."""
        open_control_root(Settings(sec_control_dir=str(control_dir)))

        assert sorted(p.name for p in control_dir.iterdir()) == sorted(CONTROL_SUBDIRECTORIES)

    def test_a_relative_setting_resolves_to_an_absolute_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".evidence").mkdir()

        control = open_control_root(Settings(sec_control_dir=".evidence"))

        assert control.path.is_absolute()
        assert control.path == (tmp_path / ".evidence").resolve()

    def test_unset_with_a_local_root_falls_back_to_the_legacy_layout(
        self, artifact_root: Path
    ) -> None:
        settings = Settings(sec_evidence_dir=str(artifact_root), sec_control_dir=None)
        root = open_evidence_root(settings)

        control = open_control_root(settings, colocate_with=root)

        assert control is root, "the fallback is the artifact root itself, not a copy of it"
        assert control.colocated
        assert control.manifest_path == artifact_root / "manifests" / "evidence.jsonl"
        assert control.checkpoints == artifact_root / "checkpoints"

    def test_unset_with_nothing_to_colocate_with_is_an_error(self) -> None:
        """A remote artifact backend has no local root to fall back to. Say so."""
        with pytest.raises(ControlPlaneUnavailableError, match="SEC_CONTROL_DIR is unset"):
            open_control_root(Settings(sec_control_dir=None))

    def test_a_configured_root_wins_over_the_fallback(
        self, artifact_root: Path, control_dir: Path
    ) -> None:
        settings = Settings(sec_evidence_dir=str(artifact_root), sec_control_dir=str(control_dir))
        root = open_evidence_root(settings)

        control = open_control_root(settings, colocate_with=root)

        assert control is not root
        assert control.path == control_dir
        assert not control.colocated


class TestSameVolumeNamesTheSharedFailureDomain:
    def test_two_directories_on_one_device_share_a_domain(self, tmp_path: Path) -> None:
        first = tmp_path / "a"
        second = tmp_path / "b"
        first.mkdir()
        second.mkdir()

        assert same_volume(first, second)

    def test_the_legacy_layout_is_one_domain_by_construction(self, artifact_root: Path) -> None:
        root = open_evidence_root(Settings(sec_evidence_dir=str(artifact_root)))

        assert same_volume(root.path, root.manifests)


class TestManifestDistinguishesUnwrittenFromGone:
    def test_an_unwritten_manifest_in_a_present_directory_is_empty(self, control_dir: Path) -> None:
        """First-time initialisation: nothing to read yet, and the first append creates it."""
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))
        manifest = Manifest(control.manifest_path)

        assert list(manifest.entries()) == []

    def test_a_manifest_whose_directory_is_gone_raises_on_read(self, control_dir: Path) -> None:
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))
        manifest = Manifest(control.manifest_path)
        remove_tree(control_dir)

        with pytest.raises(ControlPlaneUnavailableError, match="unavailable, not empty"):
            list(manifest.entries())

    def test_a_manifest_whose_directory_is_gone_raises_on_append_and_creates_nothing(
        self, control_dir: Path
    ) -> None:
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))
        manifest = Manifest(control.manifest_path)
        remove_tree(control_dir)
        source = Source()

        with pytest.raises(ControlPlaneUnavailableError):
            harvest_artifacts([FILING], LocalEvidenceStorage(control_dir), manifest, source)

        assert not control_dir.exists(), "no directory is conjured to hold a new empty log"
        assert source.calls == 0, "nothing was fetched into a corpus that cannot be vouched for"


class TestCheckpointDistinguishesUnattemptedFromGone:
    def test_an_absent_checkpoint_in_a_present_directory_is_unattempted(
        self, control_dir: Path
    ) -> None:
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))

        assert load_checkpoint(checkpoint_path(control, 4250, 250)) is None

    def test_a_checkpoint_whose_directory_is_gone_raises(self, control_dir: Path) -> None:
        """Seventeen completed batches must not read as a sweep that never started."""
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))
        marker = checkpoint_path(control, 4250, 250)
        save_checkpoint(
            marker, Checkpoint(offset=4250, limit=250, selection_digest="d", selected=250)
        )
        remove_tree(control_dir)

        with pytest.raises(ControlPlaneUnavailableError, match="never run"):
            load_checkpoint(marker)

    def test_checkpoints_live_beneath_the_control_root_not_the_artifacts(
        self, separated: tuple[EvidenceRoot, EvidenceControlRoot]
    ) -> None:
        root, control = separated

        marker = checkpoint_path(control, 0, 250)

        assert marker.is_relative_to(control.path)
        assert not marker.is_relative_to(root.path)


class TestCapacityIsALocalBackendQuestion:
    def test_the_local_backend_reports_free_space(self, artifact_root: Path) -> None:
        assert LocalEvidenceStorage(artifact_root).free_bytes() > 0
        assert isinstance(LocalEvidenceStorage(artifact_root), LocalCapacity)

    def test_a_memory_backend_has_no_capacity_and_is_not_asked(
        self, memory_storage: type[MemoryEvidenceStorage], control_dir: Path
    ) -> None:
        """No universal disk-capacity requirement: a reserve of a petabyte is not a failure."""
        storage = memory_storage()
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))
        manifest = Manifest(control.manifest_path)
        harvest_artifacts([FILING], storage, manifest, Source(), budget=budget())

        assert not isinstance(storage, LocalCapacity)
        preflight_corpus(storage, manifest, min_free_bytes=1024**5, verify_digests=True)

    def test_a_local_backend_below_its_reserve_stops_the_chunk(
        self, separated: tuple[EvidenceRoot, EvidenceControlRoot]
    ) -> None:
        root, control = separated
        manifest = Manifest(control.manifest_path)

        with pytest.raises(SweepError, match="below the"):
            preflight_corpus(root, manifest, min_free_bytes=root.free_bytes() + 1)

    def test_a_vanished_local_root_stops_the_chunk_in_the_sweeps_own_words(
        self, separated: tuple[EvidenceRoot, EvidenceControlRoot]
    ) -> None:
        root, control = separated
        manifest = Manifest(control.manifest_path)
        remove_tree(root.path)

        with pytest.raises(SweepError, match="not a directory"):
            preflight_corpus(root, manifest, min_free_bytes=0)

    def test_the_corpus_gate_still_runs_for_every_backend(
        self, memory_storage: type[MemoryEvidenceStorage], control_dir: Path
    ) -> None:
        storage = memory_storage()
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))
        manifest = Manifest(control.manifest_path)
        harvest_artifacts([FILING], storage, manifest, Source(), budget=budget())
        storage.write_bytes(FILING_KEY, b"tampered")

        with pytest.raises(SweepError, match="does not reconcile"):
            preflight_corpus(storage, manifest, verify_digests=True)


class TestSeparateRootsLifecycle:
    """Harvest into A, record into B, and then take A away."""

    def test_the_manifest_lands_in_the_control_root_and_nothing_else_in_the_artifacts(
        self, separated: tuple[EvidenceRoot, EvidenceControlRoot]
    ) -> None:
        root, control = separated
        manifest = Manifest(control.manifest_path)

        report = harvest_artifacts([FILING], root, manifest, Source(), budget=budget())

        assert report.by_outcome == {"DOWNLOADED": 1}
        assert control.manifest_path.is_file()
        assert (root.path / FILING_KEY).is_file()
        assert not (root.path / "manifests" / "evidence.jsonl").exists()
        assert list(root.list_prefix("")) == [FILING_KEY]

    def test_the_manifest_record_is_the_same_record_the_colocated_layout_writes(
        self, separated: tuple[EvidenceRoot, EvidenceControlRoot], tmp_path: Path
    ) -> None:
        """Location moved; schema, key, SHA, bytes, URL and timestamp did not."""
        root, control = separated
        harvest_artifacts(
            [FILING], root, Manifest(control.manifest_path), Source(), budget=budget()
        )
        legacy_dir = tmp_path / "legacy"
        legacy_dir.mkdir()
        legacy = open_evidence_root(Settings(sec_evidence_dir=str(legacy_dir)))
        harvest([FILING], legacy, Source(), budget=budget())

        separated_line = json.loads(control.manifest_path.read_text().splitlines()[0])
        legacy_line = json.loads(legacy.manifest_path.read_text().splitlines()[0])

        del separated_line["recorded_at"], legacy_line["recorded_at"]
        assert separated_line == legacy_line
        assert separated_line["path"] == FILING_KEY
        assert str(control.path) not in json.dumps(separated_line)
        assert str(root.path) not in json.dumps(separated_line)

    def test_replay_parse_reconcile_and_checkpoint_all_work_across_the_two_roots(
        self, separated: tuple[EvidenceRoot, EvidenceControlRoot]
    ) -> None:
        root, control = separated
        manifest = Manifest(control.manifest_path)
        source = Source()
        harvest_artifacts([FILING], root, manifest, source, budget=budget())

        replay = harvest_artifacts([FILING], root, manifest, source, budget=budget())
        extraction = build_index(root, manifest, derived_root(control) / "index.db")
        state = reconcile(root, manifest, verify_digests=True)
        marker = checkpoint_path(control, 0, 250)
        save_checkpoint(
            marker,
            Checkpoint(
                offset=0, limit=250, selection_digest="d", selected=250, stage=ChunkStage.COMPLETE
            ),
        )
        fingerprint = save_fingerprint(control, fingerprint_corpus(control, manifest))

        assert replay.by_outcome == {"CACHE_HIT": 1}
        assert source.calls == 1
        assert extraction.filings_parsed == 1
        assert state.clean
        assert load_checkpoint(marker) is not None
        assert fingerprint.is_relative_to(control.path)
        assert (control.derived / "index.db").is_file()
        assert list(root.list_prefix("")) == [FILING_KEY], "the artifact root holds artifacts only"

    def test_losing_the_artifact_root_leaves_every_recovery_record_readable(
        self, separated: tuple[EvidenceRoot, EvidenceControlRoot]
    ) -> None:
        """The BEK scenario, after the fix."""
        root, control = separated
        manifest = Manifest(control.manifest_path)
        harvest_artifacts([FILING], root, manifest, Source(), budget=budget())
        marker = checkpoint_path(control, 0, 250)
        save_checkpoint(
            marker,
            Checkpoint(
                offset=0, limit=250, selection_digest="d", selected=250, stage=ChunkStage.COMPLETE
            ),
        )
        control.plans.mkdir()
        plan = control.plans / "targets.jsonl"
        plan.write_text(
            json.dumps(
                {
                    "cik": 320193,
                    "accession": "0000320193-24-000123",
                    "form": "10-K",
                    "filing_date": "2024-11-01",
                }
            )
            + "\n"
        )
        save_fingerprint(control, fingerprint_corpus(control, manifest))

        remove_tree(root.path)

        (entry,) = Manifest(control.manifest_path).entries()
        assert entry.path == FILING_KEY
        assert entry.sha256 == hashlib.sha256(COVER).hexdigest()
        resumed = load_checkpoint(marker)
        assert resumed is not None
        assert resumed.stage == ChunkStage.COMPLETE
        targets, audit = load_manifest(plan)
        assert audit.rows == 1
        assert targets[0].accession == "0000320193-24-000123"
        assert load_fingerprint(control) is not None

    def test_losing_the_artifact_root_makes_every_artifact_operation_loud(
        self, separated: tuple[EvidenceRoot, EvidenceControlRoot]
    ) -> None:
        root, control = separated
        manifest = Manifest(control.manifest_path)
        source = Source()
        harvest_artifacts([FILING], root, manifest, source, budget=budget())
        remove_tree(root.path)

        with pytest.raises(StorageUnavailableError):
            root.exists(FILING_KEY)
        with pytest.raises(StorageUnavailableError):
            list(root.list_prefix(""))
        with pytest.raises(StorageUnavailableError):
            reconcile(root, manifest, verify_digests=True)
        with pytest.raises(StorageUnavailableError):
            harvest_artifacts([FILING], root, manifest, source, budget=budget())

        assert source.calls == 1, "an unreachable corpus is not refetched into thin air"
        (entry,) = manifest.entries()
        assert entry.outcome.value == "DOWNLOADED", "the provenance record is untouched"

    def test_losing_the_control_root_stops_everything_and_creates_nothing(
        self, separated: tuple[EvidenceRoot, EvidenceControlRoot]
    ) -> None:
        root, control = separated
        manifest = Manifest(control.manifest_path)
        source = Source()
        harvest_artifacts([FILING], root, manifest, source, budget=budget())
        marker = checkpoint_path(control, 0, 250)
        remove_tree(control.path)

        with pytest.raises(ControlPlaneUnavailableError):
            list(manifest.entries())
        with pytest.raises(ControlPlaneUnavailableError):
            load_checkpoint(marker)
        with pytest.raises(ControlPlaneUnavailableError):
            harvest_artifacts([FILING], root, manifest, source, budget=budget())
        with pytest.raises(ControlPlaneUnavailableError, match="never created"):
            open_control_root(Settings(sec_control_dir=str(control.path)), colocate_with=root)

        assert source.calls == 1, "no request was spent on a corpus that cannot be recorded"
        assert not control.path.exists(), "the control root is not recreated empty"
        assert root.exists(FILING_KEY), "the artifacts are untouched"

    def test_no_control_plane_operation_lists_the_artifact_namespace(
        self, memory_storage: type[MemoryEvidenceStorage], control_dir: Path
    ) -> None:
        """Opening the manifest, a checkpoint or a plan never enumerates storage."""
        storage = memory_storage()
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))
        manifest = Manifest(control.manifest_path)
        harvest_artifacts([FILING], storage, manifest, Source(), budget=budget())
        marker = checkpoint_path(control, 0, 250)
        save_checkpoint(marker, Checkpoint(offset=0, limit=250, selection_digest="d", selected=250))
        storage.listed.clear()

        list(manifest.entries())
        manifest.replay()
        load_checkpoint(marker)
        fingerprint_corpus(control, manifest)

        assert storage.listed == []


class TestLegacyColocatedLayoutStaysOperable:
    def test_the_colocated_root_runs_the_whole_lifecycle_unchanged(
        self, artifact_root: Path
    ) -> None:
        settings = Settings(sec_evidence_dir=str(artifact_root))
        root = open_evidence_root(settings)
        control = open_control_root(settings, colocate_with=root)
        manifest = Manifest(control.manifest_path)
        source = Source()

        harvest_artifacts([FILING], root, manifest, source, budget=budget())
        replay = harvest_artifacts([FILING], root, manifest, source, budget=budget())
        state = reconcile(root, manifest, verify_digests=True)
        marker = checkpoint_path(control, 0, 250)
        save_checkpoint(marker, Checkpoint(offset=0, limit=250, selection_digest="d", selected=250))

        assert replay.by_outcome == {"CACHE_HIT": 1}
        assert state.clean
        assert manifest.path == artifact_root / "manifests" / "evidence.jsonl"
        assert marker == artifact_root / "checkpoints" / "chunk-000000-0250.json"
        assert derived_root(control) == artifact_root / "derived"

    def test_the_legacy_wrapper_and_the_separated_form_write_the_same_place(
        self, artifact_root: Path
    ) -> None:
        settings = Settings(sec_evidence_dir=str(artifact_root))
        root = open_evidence_root(settings)

        harvest([FILING], root, Source(), budget=budget())

        control = open_control_root(settings, colocate_with=root)
        (entry,) = Manifest(control.manifest_path).entries()
        assert entry.path == FILING_KEY

    def test_the_colocated_layout_is_one_failure_domain(self, artifact_root: Path) -> None:
        """The arrangement that lost the metadata, stated as a fact rather than hidden."""
        settings = Settings(sec_evidence_dir=str(artifact_root))
        root = open_evidence_root(settings)
        control = open_control_root(settings, colocate_with=root)
        harvest_artifacts(
            [FILING], root, Manifest(control.manifest_path), Source(), budget=budget()
        )

        remove_tree(root.path)

        with pytest.raises(ControlPlaneUnavailableError):
            list(Manifest(control.manifest_path).entries())


class TestStatusReportsBothPlanes:
    @pytest.fixture
    def environment(self, artifact_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setenv("SEC_EVIDENCE_DIR", str(artifact_root))
        monkeypatch.setenv("ENVIRONMENT", "test")
        get_settings.cache_clear()
        return artifact_root

    def test_the_legacy_layout_is_reported_as_a_shared_failure_domain(
        self, environment: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SEC_CONTROL_DIR", raising=False)
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "status"])

        assert result.exit_code == 0
        assert "artifact backend : local" in result.stdout
        assert f"artifact root    : {environment}" in result.stdout
        assert "LEGACY" in result.stdout
        assert "shared failure domain" in result.stdout
        assert f"manifest         : {environment / 'manifests' / 'evidence.jsonl'}" in result.stdout
        get_settings.cache_clear()

    def test_a_separate_control_root_is_reported_with_its_own_paths(
        self, environment: Path, control_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control_dir))
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "status"])

        assert result.exit_code == 0
        assert f"control root     : {control_dir}" in result.stdout
        assert "control state    : AVAILABLE" in result.stdout
        assert f"manifest         : {control_dir / 'manifests' / 'evidence.jsonl'}" in result.stdout
        assert f"checkpoints      : {control_dir / 'checkpoints'}" in result.stdout
        assert "LEGACY" not in result.stdout
        get_settings.cache_clear()

    def test_a_missing_control_root_exits_non_zero_and_creates_nothing(
        self, environment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        absent = tmp_path / "Volumes" / "BOOT" / "control"
        monkeypatch.setenv("SEC_CONTROL_DIR", str(absent))
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "status"])

        assert result.exit_code == 1
        assert "control state    : UNAVAILABLE" in result.stdout
        assert not (tmp_path / "Volumes").exists()
        get_settings.cache_clear()
