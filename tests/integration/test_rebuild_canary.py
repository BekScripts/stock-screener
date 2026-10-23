"""One chunk of the second generation, rebuilt end to end against fakes.

Before a byte leaves the SEC for the bucket, the whole sequence is proven
here: preflight on an empty generation, the offline plans mirrored and read
back, one ticker-map fetch preserved raw, every identity resolved without a
guess, the chunk frozen, submissions and filings harvested with the stricter
V2 chain — read back from the bucket and hashed before the manifest learns
of them — a replay that fetches nothing, a full reconciliation, a COMPLETE
checkpoint, and a mirror that could recover all of it.

Then every way it must stop: a bucket that holds different bytes after a
PUT (no manifest row), a mirror that disagrees (no overwrite), a SEC 403
(no further request), a first pass that fails and a second that recovers,
and an interruption at each stage that a rerun resumes from without a false
COMPLETE.

No network: the SEC is a dict of URLs; the bucket is the fake client.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from api_clients.errors import ProviderAuthError, ProviderRequestError
from data_access import (
    CompanyRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import HistoricalSecurity
from domain import CompanyProfile
from stock_screener.cli import app
from stock_screener.config import Settings, get_settings
from stock_screener.sec_evidence import (
    ARTIFACT_PREFIXES,
    B2Configuration,
    B2EvidenceStorage,
    Budget,
    Checkpoint,
    ChunkStage,
    ControlMirror,
    EvidenceWriteError,
    IdentityRow,
    Manifest,
    MirrorConflictError,
    MirrorOutcome,
    Outcome,
    RequestKind,
    ResolutionSource,
    StorageUnavailableError,
    StoredArtifact,
    Target,
    harvest_artifacts,
    load_checkpoint,
    open_control_root,
    reconcile,
    resolve_identities,
    run_chunk,
    save_checkpoint,
    v2_progress,
)
from stock_screener.sec_evidence.rebuild import TICKER_MAP_URL

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from conftest import FakeS3Client, FakeSec
    from stock_screener.sec_evidence import EvidenceControlRoot

pytestmark = pytest.mark.integration

B2_CONFIG = B2Configuration(
    endpoint_url="https://s3.us-west-004.backblazeb2.com",
    bucket="rebuild-test-bucket",
    key_id=SecretStr("rebuild-key-id"),
    application_key=SecretStr("rebuild-application-key"),
)

GENERATION = "SEC_EVIDENCE_GENERATION_V2"

TICKER_MAP = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
    "2": {"cik_str": 999999, "ticker": "BBBY", "title": "Symbol Reuser Corp"},
}

AAPL_SUBMISSIONS = "https://data.sec.gov/submissions/CIK0000320193.json"
MSFT_SUBMISSIONS = "https://data.sec.gov/submissions/CIK0000789019.json"
SGEN_SUBMISSIONS = "https://data.sec.gov/submissions/CIK0001060736.json"
MSFT_10K = "https://www.sec.gov/Archives/edgar/data/789019/000095017024087843/msft-20240630.htm"
MSFT_10K_KEY = "filings/0000789019/000095017024087843/msft-20240630.htm"
SGEN_8K_4 = "https://www.sec.gov/Archives/edgar/data/1060736/000106073623000004/sgen-4.htm"
SGEN_8K_4_KEY = "filings/0001060736/000106073623000004/sgen-4.htm"
CHECKPOINT = "chunk-000000-0250.json"
ALLOWED_FORMS = {"10-K", "10-Q", "8-K", "20-F", "20-F/A", "40-F", "40-F/A"}


def identity(position: int, bucket: str, symbol: str, cik: int | None = None) -> IdentityRow:
    return IdentityRow(
        position=position,
        chunk=position // 250,
        bucket=bucket,
        symbol=symbol,
        name=symbol,
        company_id=position if bucket == "LIVE" else None,
        historical_security_id=None if bucket == "LIVE" else position,
        cik=cik,
        resolution="STORED" if cik is not None else "EDGAR_REQUIRED",
        submissions_url=f"https://data.sec.gov/submissions/CIK{cik:010d}.json" if cik else None,
    )


UNIVERSE = [
    identity(0, "LIVE", "AAPL"),
    identity(1, "DELISTED", "BBBY"),  # the symbol is reused today; must stay UNRESOLVED
    identity(2, "LIVE", "MSFT"),
    identity(3, "LIVE", "NOMAP"),
    identity(4, "DELISTED", "SGEN", cik=1060736),
    identity(5, "DELISTED", "ZZZZ"),
    identity(6, "LIVE", "AAPL"),  # a second listing of the same filer: one catalogue
]

OFFLINE_PLAN_FILES = (
    "base_identities.jsonl",
    "base_identities.sha256",
    "base_targets_offline.jsonl",
    "base_targets_offline.sha256",
    "prerequisites.json",
    "generation.json",
)


@pytest.fixture
def sec(fake_sec: type[FakeSec]) -> FakeSec:
    fake = fake_sec(TICKER_MAP)
    fake.add_filer(
        320193,
        [
            ("0000320193-24-000123", "10-K", "2024-11-01", "aapl-20240928.htm"),
            ("0000320193-24-000081", "10-Q", "2024-08-02", "aapl-20240629.htm"),
            ("0000320193-24-000050", "4", "2024-05-01", "form4.xml"),
        ],
    )
    fake.add_filer(789019, [("0000950170-24-087843", "10-K", "2024-07-30", "msft-20240630.htm")])
    fake.add_filer(
        1060736,
        [
            (f"0001060736-23-0000{n:02d}", "8-K", f"2023-0{n}-15", f"sgen-{n}.htm")
            for n in range(1, 9)
        ],
    )
    return fake


def seed_control(root: Path) -> EvidenceControlRoot:
    """A V2 control root holding the offline plans `evidence generation-plan` writes."""
    root.mkdir()
    control = open_control_root(Settings(sec_control_dir=str(root)))
    plans = control.plans / "generation_v2"
    plans.mkdir(parents=True)
    identities = "".join(row.line() + "\n" for row in UNIVERSE)
    (plans / "base_identities.jsonl").write_text(identities)
    (plans / "base_identities.sha256").write_text(
        f"{hashlib.sha256(identities.encode()).hexdigest()}  base_identities.jsonl\n"
    )
    offline = "".join(row.line() + "\n" for row in UNIVERSE if row.submissions_url)
    (plans / "base_targets_offline.jsonl").write_text(offline)
    (plans / "base_targets_offline.sha256").write_text(
        f"{hashlib.sha256(offline.encode()).hexdigest()}  base_targets_offline.jsonl\n"
    )
    (plans / "prerequisites.json").write_text("[]\n")
    (plans / "generation.json").write_text(f'{{"generation_id": "{GENERATION}"}}\n')
    return control


@pytest.fixture
def control(tmp_path: Path) -> EvidenceControlRoot:
    return seed_control(tmp_path / "control")


def bucket(fake_s3: type[FakeS3Client]) -> tuple[B2EvidenceStorage, FakeS3Client]:
    client = fake_s3()
    return B2EvidenceStorage(B2_CONFIG, client=client), client


def artifact_keys(client: FakeS3Client) -> set[str]:
    return {k for k in client.objects if k.split("/", 1)[0] in ARTIFACT_PREFIXES}


def artifact_calls(client: FakeS3Client, operation: str) -> list[str]:
    return [
        k for op, k in client.calls if op == operation and k.split("/", 1)[0] in ARTIFACT_PREFIXES
    ]


def trusted_paths(control: EvidenceControlRoot) -> set[str]:
    return {
        e.path
        for e in Manifest(control.manifest_path).latest_by_url().values()
        if e.is_stored and e.path is not None
    }


def same_length_tamper(body: bytes) -> tuple[bytes, dict[str, str]]:
    """Bytes the backend's HEAD verification cannot tell from the real ones."""
    last = b"X" if body[-1:] != b"X" else b"Y"
    return body[:-1] + last, {"sha256": hashlib.sha256(body).hexdigest()}


class TestResolutionNeverGuesses:
    def test_stored_wins_live_uses_the_map_and_delisted_never_takes_a_current_holder(
        self,
    ) -> None:
        ticker_map = {"AAPL": 320193, "MSFT": 789019, "BBBY": 999999}

        rows = {r.symbol: r for r in resolve_identities(UNIVERSE, ticker_map)}

        assert rows["SGEN"].source == ResolutionSource.STORED
        assert rows["SGEN"].cik == 1060736
        assert rows["AAPL"].source == ResolutionSource.SEC_TICKER_MAP
        assert rows["AAPL"].cik == 320193
        assert rows["BBBY"].source == ResolutionSource.UNRESOLVED
        assert rows["BBBY"].cik is None
        assert "today's holder" in rows["BBBY"].reason
        assert rows["NOMAP"].source == ResolutionSource.UNRESOLVED
        assert rows["ZZZZ"].source == ResolutionSource.UNRESOLVED

    def test_resolution_is_deterministic_and_in_universe_order(self) -> None:
        ticker_map = {"AAPL": 320193, "MSFT": 789019}

        first = [r.line() for r in resolve_identities(UNIVERSE, ticker_map)]
        second = [r.line() for r in resolve_identities(UNIVERSE, ticker_map)]

        assert first == second
        assert [json.loads(line)["position"] for line in first] == [0, 1, 2, 3, 4, 5, 6]


class TestTheStricterTrustChain:
    def test_a_bucket_holding_different_bytes_after_put_gets_no_manifest_row(
        self, fake_s3: type[FakeS3Client], fake_sec: type[FakeSec], control: EvidenceControlRoot
    ) -> None:
        storage, client = bucket(fake_s3)
        key = "submissions/CIK0000320193.json"
        payload = b'{"cik": "0000320193"}'
        client.tamper_after_put[key] = same_length_tamper(payload)
        sec = fake_sec()
        sec.responses[AAPL_SUBMISSIONS] = payload
        manifest = Manifest(control.manifest_path)
        target = Target(url=AAPL_SUBMISSIONS, kind=RequestKind.SUBMISSIONS, cik=320193)

        with pytest.raises(EvidenceWriteError, match="no manifest entry written"):
            harvest_artifacts(
                [target], storage, manifest, sec, budget=Budget(10, 10**6), verify_written=True
            )

        assert list(manifest.entries()) == []
        assert manifest.replay().latest == {}
        assert key in client.objects, "left in place for reconciliation, never deleted"
        assert not any(op.startswith("delete") for op, _ in client.calls)

    def test_the_backend_head_check_alone_would_have_trusted_the_same_tamper(
        self, fake_s3: type[FakeS3Client], fake_sec: type[FakeSec], control: EvidenceControlRoot
    ) -> None:
        """Why the flag exists: HEAD agrees on length and metadata, the body does not."""
        storage, client = bucket(fake_s3)
        key = "submissions/CIK0000320193.json"
        payload = b'{"cik": "0000320193"}'
        client.tamper_after_put[key] = same_length_tamper(payload)
        sec = fake_sec()
        sec.responses[AAPL_SUBMISSIONS] = payload
        manifest = Manifest(control.manifest_path)
        target = Target(url=AAPL_SUBMISSIONS, kind=RequestKind.SUBMISSIONS, cik=320193)

        harvest_artifacts([target], storage, manifest, sec, budget=Budget(10, 10**6))

        assert len(list(manifest.entries())) == 1


class TestTheMirror:
    def test_a_new_file_is_uploaded_then_read_back(
        self, fake_s3: type[FakeS3Client], tmp_path: Path
    ) -> None:
        storage, client = bucket(fake_s3)
        mirror = ControlMirror(storage, GENERATION)
        local = tmp_path / "generation.json"
        local.write_text("{}\n")

        record = mirror.mirror(local, "generation.json")

        assert record.outcome == MirrorOutcome.MIRRORED
        assert record.key == f"control/{GENERATION}/generation.json"
        assert ("put_object", record.key) in client.calls
        assert client.calls[-1] == ("get_object", record.key)
        assert mirror.report.puts == 1

    def test_an_identical_file_is_a_hit_and_costs_no_put(
        self, fake_s3: type[FakeS3Client], tmp_path: Path
    ) -> None:
        storage, client = bucket(fake_s3)
        mirror = ControlMirror(storage, GENERATION)
        local = tmp_path / "generation.json"
        local.write_text("{}\n")
        mirror.mirror(local, "generation.json")
        puts_before = sum(1 for op, _ in client.calls if op == "put_object")

        record = mirror.mirror(local, "generation.json")

        assert record.outcome == MirrorOutcome.MIRROR_HIT
        assert sum(1 for op, _ in client.calls if op == "put_object") == puts_before

    def test_different_bytes_are_a_conflict_and_nothing_is_overwritten(
        self, fake_s3: type[FakeS3Client], tmp_path: Path
    ) -> None:
        storage, client = bucket(fake_s3)
        mirror = ControlMirror(storage, GENERATION)
        local = tmp_path / "generation.json"
        local.write_text("{}\n")
        mirror.mirror(local, "generation.json")
        local.write_text('{"changed": true}\n')

        with pytest.raises(MirrorConflictError, match="nothing overwritten"):
            mirror.mirror(local, "generation.json")

        assert client.objects[f"control/{GENERATION}/generation.json"][0] == b"{}\n"

    def test_a_read_back_that_differs_from_the_upload_fails_the_mirror(
        self, fake_s3: type[FakeS3Client], tmp_path: Path
    ) -> None:
        storage, client = bucket(fake_s3)
        mirror = ControlMirror(storage, GENERATION)
        local = tmp_path / "plan.jsonl"
        local.write_text("row\n")
        client.tamper_after_put[f"control/{GENERATION}/plans/plan.jsonl"] = same_length_tamper(
            b"row\n"
        )

        with pytest.raises(MirrorConflictError, match="read back as"):
            mirror.mirror(local, "plans/plan.jsonl")

    @pytest.mark.parametrize("relative", ["", "/", "../escape", "a//b"])
    def test_an_unsafe_key_is_refused_before_any_request(
        self, fake_s3: type[FakeS3Client], tmp_path: Path, relative: str
    ) -> None:
        storage, client = bucket(fake_s3)
        mirror = ControlMirror(storage, GENERATION)
        local = tmp_path / "f"
        local.write_text("x")

        with pytest.raises(ValueError, match=r"key|segment"):
            mirror.mirror(local, relative)

        assert client.calls == []

    @pytest.mark.parametrize("relative", ["filings/x.htm", "submissions/y.json", "fulltext/z"])
    def test_an_artifact_prefix_is_refused_inside_the_namespace(
        self, fake_s3: type[FakeS3Client], tmp_path: Path, relative: str
    ) -> None:
        storage, client = bucket(fake_s3)
        mirror = ControlMirror(storage, GENERATION)
        local = tmp_path / "f"
        local.write_text("x")

        with pytest.raises(MirrorConflictError, match="artifact prefix"):
            mirror.mirror(local, relative)

        assert client.calls == []

    def test_snapshots_are_digest_addressed_and_never_overwritten(
        self, fake_s3: type[FakeS3Client], tmp_path: Path
    ) -> None:
        storage, client = bucket(fake_s3)
        mirror = ControlMirror(storage, GENERATION)
        local = tmp_path / "evidence.jsonl"
        local.write_text("{}\n")
        sha16 = hashlib.sha256(b"{}\n").hexdigest()[:16]

        first, meta = mirror.mirror_snapshot(local, "manifests", records=1, trusted=1)
        local.write_text("{}\n{}\n")
        second, _ = mirror.mirror_snapshot(local, "manifests", records=2, trusted=2)

        assert first.key != second.key
        assert first.key.endswith(f"-{sha16}.jsonl")
        assert meta.key.endswith(".snapshot.json")
        assert json.loads(client.objects[meta.key][0])["trusted"] == 1
        assert len([k for k in client.objects if "/manifests/" in k and k.endswith(".jsonl")]) == 2
        assert list(tmp_path.iterdir()) == [local], "no descriptor left on disk"

    def test_mirrored_control_files_are_invisible_to_reconciliation(
        self, fake_s3: type[FakeS3Client], tmp_path: Path, control: EvidenceControlRoot
    ) -> None:
        storage, _ = bucket(fake_s3)
        mirror = ControlMirror(storage, GENERATION)
        local = tmp_path / "generation.json"
        local.write_text("{}\n")
        mirror.mirror(local, "generation.json")

        state = reconcile(storage, Manifest(control.manifest_path), verify_digests=True)

        assert state.files_on_disk == 0
        assert state.orphan_files == 0


class TestOneChunkEndToEnd:
    def test_the_whole_sequence_completes_and_recovery_is_proven(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, client = bucket(fake_s3)

        report = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert report.passed, report.failure
        assert report.stage == ChunkStage.COMPLETE
        assert [s.name for s in report.steps if not s.ok] == []
        assert report.resolution["STORED"] == 1
        assert report.resolution["SEC_TICKER_MAP"] == 3
        assert report.resolution["UNRESOLVED"] == 3
        assert sec.calls.count(TICKER_MAP_URL) == 1
        assert report.submissions["targets"] == 3, "two AAPL listings share one catalogue"
        assert report.submissions["downloaded"] == 3
        assert report.filings["targets"] == 9, "AAPL 2 (Form 4 dropped), MSFT 1, SGEN capped at 6"
        assert report.filings["downloaded"] == 9
        assert report.filings["unique_cik_accession"] == 9
        assert report.reconciliation["trusted"] == 12
        assert report.reconciliation["orphan_files"] == 0
        assert report.reconciliation["hash_mismatches"] == 0
        assert report.reconciliation["zero_byte"] == 0
        assert artifact_keys(client) == trusted_paths(control)
        assert report.recovery_sufficient
        assert v2_progress(control) == (12, 1, [])

    def test_the_checkpoint_records_the_frozen_digests_and_the_accounting(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)

        run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        checkpoint = load_checkpoint(control.checkpoints / CHECKPOINT)
        assert checkpoint is not None
        assert checkpoint.stage == ChunkStage.COMPLETE
        assert checkpoint.selected == 7
        assert checkpoint.accounting["trusted"] == 12
        assert checkpoint.completed_at is not None
        assert f"generation={GENERATION}" in checkpoint.notes
        prefixes = {n.split("=", 1)[0] for n in checkpoint.notes}
        assert {"identity_plan_sha256", "resolution_sha256", "filing_targets_sha256"} <= prefixes
        chunk_dir = control.plans / "generation_v2" / "chunks" / "000"
        identities = (chunk_dir / "identities.jsonl").read_bytes()
        assert checkpoint.selection_digest == hashlib.sha256(identities).hexdigest()
        companion = (chunk_dir / "identities.sha256").read_text().split()[0]
        assert companion == checkpoint.selection_digest

    def test_the_chunk_summary_keeps_its_identity_counts_through_every_snapshot(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)

        run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        summary = json.loads(
            (control.plans / "generation_v2" / "chunks" / "000" / "summary.json").read_text()
        )
        assert summary["identities"] == 7
        assert summary["resolved"] == 4
        assert summary["stage"] == ChunkStage.COMPLETE
        assert summary["trusted"] == 12

    def test_every_artifact_was_read_back_before_it_was_recorded(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, client = bucket(fake_s3)

        run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        puts = artifact_calls(client, "put_object")
        gets = artifact_calls(client, "get_object")
        assert len(puts) == 12 == len(set(puts)), "each artifact PUT exactly once"
        assert set(puts) <= set(gets)
        for key in puts:
            assert client.calls.index(("put_object", key)) < client.calls.index(("get_object", key))
        assert not any(op.startswith("delete") for op, _ in client.calls)

    def test_the_frozen_files_are_byte_identical_across_two_generations(
        self,
        fake_s3: type[FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        tmp_path: Path,
    ) -> None:
        storage, _ = bucket(fake_s3)
        run_chunk(storage, control, sec, UNIVERSE, chunk=0)
        plans = control.plans / "generation_v2"
        frozen = (
            "filer_resolution_v1.jsonl",
            "filer_resolution_v1.sha256",
            "chunks/000/identities.jsonl",
            "chunks/000/identities.sha256",
            "chunks/000/filing_targets.jsonl",
            "chunks/000/filing_targets.sha256",
        )
        other = seed_control(tmp_path / "control2")
        storage2, _ = bucket(fake_s3)

        run_chunk(storage2, other, sec, UNIVERSE, chunk=0)

        for name in frozen:
            assert (other.plans / "generation_v2" / name).read_bytes() == (
                plans / name
            ).read_bytes()

    def test_the_replay_fetches_nothing_writes_nothing_and_hits_everything(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)

        report = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        converge = next(s for s in report.steps if s.name.startswith("converge"))
        assert converge.ok
        assert converge.detail.startswith("12 CACHE_HIT, 0 fetches, 0 bytes, 0 PUTs")

    def test_filing_targets_are_newest_first_within_the_allowed_forms(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)

        run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        targets = control.plans / "generation_v2" / "chunks" / "000" / "filing_targets.jsonl"
        rows = [json.loads(line) for line in targets.read_text().splitlines()]
        assert {r["form"] for r in rows} <= ALLOWED_FORMS
        sgen = [r for r in rows if r["cik"] == 1060736]
        dates = [r["filing_date"] for r in sgen]
        assert dates == sorted(dates, reverse=True)
        assert [r["accession"][-2:] for r in sgen] == ["08", "07", "06", "05", "04", "03"]


class TestStopConditions:
    def test_production_objects_already_in_the_bucket_stop_a_fresh_run(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, client = bucket(fake_s3)
        client.objects["filings/0000000009/x/stray.htm"] = (b"?", {})

        report = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert not report.passed
        assert "requires an empty generation" in (report.failure or "")
        assert sec.calls == []

    def test_a_first_generation_checkpoint_in_the_control_root_stops_the_run(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)
        save_checkpoint(
            control.checkpoints / "chunk-004250-0250.json",
            Checkpoint(
                offset=4250,
                limit=250,
                selection_digest="v1",
                selected=250,
                stage=ChunkStage.COMPLETE,
                accounting={"trusted": 342},
            ),
        )

        report = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert not report.passed
        assert "cannot support" in (report.failure or "")
        assert sec.calls == []

    def test_a_sec_403_stops_before_the_next_request(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)
        sec.fail_always[MSFT_SUBMISSIONS] = ProviderAuthError

        report = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert not report.passed
        assert "SEC refused the caller (ProviderAuthError)" in (report.failure or "")
        assert report.stage == ChunkStage.PENDING
        assert sec.calls[-1] == MSFT_SUBMISSIONS, "the refused request was the last one made"
        assert SGEN_SUBMISSIONS not in sec.calls

    def test_a_transient_failure_is_recovered_by_the_second_pass(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)
        sec.fail_once[MSFT_SUBMISSIONS] = ProviderRequestError

        report = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert report.passed, report.failure
        assert sec.calls.count(MSFT_SUBMISSIONS) == 2

    def test_a_persistent_failure_stops_after_the_second_pass_and_rolls_nothing_back(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)
        sec.fail_always[MSFT_SUBMISSIONS] = ProviderRequestError

        report = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert not report.passed
        assert "still failed after a second pass" in (report.failure or "")
        assert report.stage == ChunkStage.PENDING
        assert len(trusted_paths(control)) == 2
        assert v2_progress(control) == (2, 0, [])

    def test_a_tampered_bucket_object_stops_with_no_trusted_row_for_it(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, client = bucket(fake_s3)
        client.tamper_after_put[MSFT_10K_KEY] = same_length_tamper(sec.responses[MSFT_10K])

        report = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert not report.passed
        assert "no manifest entry written" in (report.failure or "")
        assert report.stage == ChunkStage.PASS1_DONE
        assert MSFT_10K_KEY not in trusted_paths(control)
        assert MSFT_10K_KEY in client.objects, "left in place for reconciliation, never deleted"

    def test_a_mirror_conflict_stops_before_any_evidence_download(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, client = bucket(fake_s3)
        client.objects[f"control/{GENERATION}/generation.json"] = (b'{"generation_id": "X"}\n', {})

        report = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert not report.passed
        assert "MirrorConflictError" in (report.failure or "")
        assert sec.calls == []
        assert artifact_keys(client) == set()

    def test_a_request_budget_stops_the_run_with_what_it_had(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)

        report = run_chunk(storage, control, sec, UNIVERSE, chunk=0, budget=Budget(2, 0))

        assert not report.passed
        assert "budget was reached" in (report.failure or "")
        assert report.sec_requests == 2
        assert len(trusted_paths(control)) == 2


class TestInterruptionAndResume:
    def test_an_interruption_after_submissions_resumes_without_refetching_them(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)
        original = storage.write_bytes
        armed = True

        def dies_mirroring_the_filing_plan(key: str, data: bytes) -> StoredArtifact:
            nonlocal armed
            if armed and "chunks/000/filing_targets" in key:
                armed = False
                raise StorageUnavailableError("simulated outage")
            return original(key, data)

        storage.write_bytes = dies_mirroring_the_filing_plan  # type: ignore[method-assign]
        first = run_chunk(storage, control, sec, UNIVERSE, chunk=0)
        fetched = sum(1 for url in sec.calls if "/submissions/" in url)
        assert not first.passed
        assert first.stage == ChunkStage.PASS1_DONE
        assert v2_progress(control) == (3, 0, [])

        second = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert second.passed, second.failure
        assert sum(1 for url in sec.calls if "/submissions/" in url) == fetched
        assert sec.calls.count(TICKER_MAP_URL) == 1, "the preserved ticker map was reused"
        assert second.submissions["cache_hit"] == 3
        assert v2_progress(control) == (12, 1, [])

    def test_an_interruption_mid_filings_resumes_and_converges(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)
        sec.fail_always[SGEN_8K_4] = ProviderRequestError
        first = run_chunk(storage, control, sec, UNIVERSE, chunk=0)
        assert not first.passed
        assert first.stage == ChunkStage.PASS1_DONE
        del sec.fail_always[SGEN_8K_4]

        second = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert second.passed, second.failure
        assert second.filings["cache_hit"] == 8
        assert second.filings["downloaded"] == 1
        assert v2_progress(control) == (12, 1, [])

    def test_a_write_that_never_reached_the_manifest_is_neither_trusted_nor_complete(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        """The bucket holds the object; the manifest does not know it; nothing lies."""
        storage, client = bucket(fake_s3)
        client.tamper_after_put[SGEN_8K_4_KEY] = same_length_tamper(sec.responses[SGEN_8K_4])
        first = run_chunk(storage, control, sec, UNIVERSE, chunk=0)
        assert not first.passed
        assert SGEN_8K_4_KEY in client.objects
        assert SGEN_8K_4_KEY not in trusted_paths(control)
        del client.tamper_after_put[SGEN_8K_4_KEY]

        puts_before = len(artifact_calls(client, "put_object"))

        second = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert not second.passed, "an object the manifest does not vouch for is an orphan"
        assert "REMOTE_ORPHAN" in (second.failure or "")
        assert SGEN_8K_4_KEY in (second.failure or "")
        assert len(artifact_calls(client, "put_object")) == puts_before, "never overwritten"
        assert SGEN_8K_4 not in sec.calls[sec.calls.index(SGEN_8K_4) + 1 :], "not refetched"
        assert v2_progress(control)[1] == 0

    def test_a_resume_keeps_the_first_runs_spend_on_the_checkpoint(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)
        sec.fail_always[SGEN_8K_4] = ProviderRequestError
        run_chunk(storage, control, sec, UNIVERSE, chunk=0)
        before = load_checkpoint(control.checkpoints / CHECKPOINT)
        assert before is not None
        assert before.requests["submissions"] == 3
        assert before.bytes_stored["submissions"] > 0
        del sec.fail_always[SGEN_8K_4]

        run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        after = load_checkpoint(control.checkpoints / CHECKPOINT)
        assert after is not None
        assert after.requests["submissions"] == 3, "a replay adds nothing"
        assert after.bytes_stored["submissions"] == before.bytes_stored["submissions"]
        assert after.requests["filings"] == 10 + 1, (
            "two failed attempts, then the one that succeeded"
        )

    def test_a_resumed_checkpoint_never_demotes_in_its_mirrored_snapshots(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, client = bucket(fake_s3)
        sec.fail_always[SGEN_8K_4] = ProviderRequestError
        run_chunk(storage, control, sec, UNIVERSE, chunk=0)
        del sec.fail_always[SGEN_8K_4]
        mirrored_before = set(client.objects)

        run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        prefix = f"control/{GENERATION}/chunks/000/checkpoints/"
        new_descriptors = sorted(
            k
            for k in client.objects
            if k.startswith(prefix) and k.endswith(".snapshot.json") and k not in mirrored_before
        )
        stages = []
        for descriptor in new_descriptors:
            facts = json.loads(client.objects[descriptor][0])
            body = client.objects[f"control/{GENERATION}/{facts['key']}"][0]
            stages.append(json.loads(body)["stage"])
        assert stages[0] == ChunkStage.PASS1_DONE, "where the interrupted run had got to"
        order = ["PENDING", "PASS1_DONE", "PASS2_DONE", "CONVERGED", "COMPLETE"]
        assert [order.index(s) for s in stages] == sorted(order.index(s) for s in stages)
        assert stages[-1] == ChunkStage.COMPLETE

    def test_a_completed_chunk_rerun_is_all_hits_and_stays_complete(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, client = bucket(fake_s3)
        run_chunk(storage, control, sec, UNIVERSE, chunk=0)
        puts_before = len(artifact_calls(client, "put_object"))

        again = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        assert again.passed, again.failure
        assert again.submissions["cache_hit"] == 3
        assert again.filings["cache_hit"] == 9
        assert len(artifact_calls(client, "put_object")) == puts_before
        assert again.mirror["MIRROR_HIT"] >= len(OFFLINE_PLAN_FILES)
        assert v2_progress(control) == (12, 1, [])


class TestV2StartsAtZero:
    def test_nothing_is_inherited_and_every_trusted_row_is_this_generations(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        assert v2_progress(control) == (0, 0, [])
        storage, _ = bucket(fake_s3)

        report = run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        rows = list(Manifest(control.manifest_path).entries())
        assert len(rows) == 12
        assert {e.outcome for e in rows} == {Outcome.DOWNLOADED}
        assert all(e.retrieved_at is not None for e in rows)
        assert all(str(e.retrieved_at).startswith("2026-09-16") for e in rows)
        assert all(e.path is not None and not e.path.startswith("control/") for e in rows)
        assert report.reconciliation["trusted"] == 12
        assert v2_progress(control) == (12, 1, [])

    def test_a_mirror_ledger_is_written_locally_for_status(
        self, fake_s3: type[FakeS3Client], control: EvidenceControlRoot, sec: FakeSec
    ) -> None:
        storage, _ = bucket(fake_s3)

        run_chunk(storage, control, sec, UNIVERSE, chunk=0)

        ledger = control.plans / "generation_v2" / "mirror_ledger.jsonl"
        records = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert all(r["key"].startswith(f"control/{GENERATION}/") for r in records)
        assert {r["outcome"] for r in records} == {"MIRRORED", "MIRROR_HIT"}
        assert "CONFLICT" not in {r["outcome"] for r in records}
        assert all(r["verified_at"] for r in records)


class TestTheCommand:
    """`evidence rebuild-canary` end to end over a seeded database, no network."""

    @pytest.fixture
    def environment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
        control = tmp_path / "control"
        control.mkdir()
        monkeypatch.setenv("ENVIRONMENT", "test")
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control))
        monkeypatch.setenv("B2_ENDPOINT_URL", B2_CONFIG.endpoint_url)
        monkeypatch.setenv("B2_BUCKET", B2_CONFIG.bucket)
        monkeypatch.setenv("B2_KEY_ID", "rebuild-key-id")
        monkeypatch.setenv("B2_APPLICATION_KEY", "rebuild-application-key")
        get_settings.cache_clear()
        yield control
        get_settings.cache_clear()

    @pytest.fixture
    def database(self, tmp_path: Path) -> Path:
        """Five live companies and three delisted securities, two of them with CIKs."""
        path = tmp_path / "stage.db"
        engine = create_engine_from_url(f"sqlite:///{path}")
        create_all(engine)
        with session_scope(build_session_factory(engine)) as session:
            for ticker in ("AAPL", "MSFT", "GOOGL", "BRKB", "T"):
                CompanyRepository(session).upsert_profile(
                    CompanyProfile(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ")
                )
            session.add(HistoricalSecurity(primary_ticker="BBBY", name="Bed Bath", cik=886158))
            session.add(HistoricalSecurity(primary_ticker="SGEN", name="Seagen", cik=1060736))
            session.add(HistoricalSecurity(primary_ticker="ZZZZ", name="No Filer", cik=None))
            session.flush()
        engine.dispose()
        return path

    @pytest.fixture
    def wired(
        self, environment: Path, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> FakeS3Client:
        client = fake_s3()
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=client),
        )
        return client

    @pytest.fixture
    def sec_cli(self, sec: FakeSec, monkeypatch: pytest.MonkeyPatch) -> FakeSec:
        sec.responses[TICKER_MAP_URL] = json.dumps(
            {
                **TICKER_MAP,
                "3": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet"},
            }
        ).encode()
        sec.add_filer(1652044, [("0001652044-24-000022", "10-Q", "2024-04-26", "goog.htm")])
        sec.add_filer(886158, [("0000886158-23-000010", "10-K", "2023-06-14", "bbby.htm")])
        monkeypatch.setattr("stock_screener.cli._sec_documents", lambda settings, budget: sec)
        return sec

    def plan(self, database: Path) -> None:
        result = CliRunner().invoke(
            app,
            [
                "evidence",
                "generation-plan",
                "--database",
                str(database),
                "--expect-universe-count",
                "8",
            ],
        )
        assert result.exit_code == 0, result.stdout

    def test_without_live_nothing_is_fetched_or_written(
        self, wired: FakeS3Client, sec_cli: FakeSec, database: Path
    ) -> None:
        result = CliRunner().invoke(
            app, ["evidence", "rebuild-canary", "--database", str(database)]
        )

        assert result.exit_code == 1
        assert "refusing without --live" in result.stdout
        assert wired.calls == []
        assert sec_cli.calls == []

    def test_a_local_backend_is_refused(
        self, environment: Path, monkeypatch: pytest.MonkeyPatch, sec_cli: FakeSec, tmp_path: Path
    ) -> None:
        root = tmp_path / "sec"
        root.mkdir()
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "local")
        monkeypatch.setenv("SEC_EVIDENCE_DIR", str(root))
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "rebuild-canary", "--live"])

        assert result.exit_code == 1
        assert "requires SEC_STORAGE_BACKEND=b2" in result.stdout
        assert sec_cli.calls == []

    def test_another_chunk_is_not_authorised(
        self, wired: FakeS3Client, sec_cli: FakeSec, database: Path
    ) -> None:
        result = CliRunner().invoke(
            app,
            ["evidence", "rebuild-canary", "--live", "--chunk", "1", "--database", str(database)],
        )

        assert result.exit_code == 1
        assert "only chunk 0 is authorised" in result.stdout
        assert wired.calls == []
        assert sec_cli.calls == []

    def test_the_command_completes_the_chunk_and_status_reports_it(
        self, environment: Path, wired: FakeS3Client, sec_cli: FakeSec, database: Path
    ) -> None:
        self.plan(database)

        result = CliRunner().invoke(
            app,
            [
                "evidence",
                "rebuild-canary",
                "--live",
                "--database",
                str(database),
                "--expect-universe-count",
                "8",
            ],
        )

        assert result.exit_code == 0, result.stdout
        assert "CHUNK COMPLETE — stop here; chunk 001 is not authorised" in result.stdout
        assert "recovery from mirror sufficient: True" in result.stdout
        assert "rebuild-application-key" not in result.stdout
        assert "rebuild-key-id" not in result.stdout
        assert sec_cli.calls.count(TICKER_MAP_URL) == 1
        # AAPL, MSFT, GOOGL from the map; BBBY, SGEN stored; BRKB, T, ZZZZ unresolved
        assert sum(1 for url in sec_cli.calls if "/submissions/" in url) == 5
        trusted = trusted_paths(open_control_root(Settings(sec_control_dir=str(environment))))
        assert len(trusted) == 5 + 2 + 1 + 1 + 1 + 6
        assert trusted == artifact_keys(wired)
        assert not any(op.startswith("delete") for op, _ in wired.calls)

        status = CliRunner().invoke(app, ["evidence", "status"])

        assert status.exit_code == 0, status.stdout
        assert "generation       : SEC_EVIDENCE_GENERATION_V2 (V1 unavailable)" in status.stdout
        assert "V2 progress      : 16 trusted artifacts, 1 completed chunks" in status.stdout
        assert "control mirror   : configured (control/SEC_EVIDENCE_GENERATION_V2)" in status.stdout

    def test_a_second_invocation_is_all_hits_and_costs_no_sec_request_but_the_map(
        self, environment: Path, wired: FakeS3Client, sec_cli: FakeSec, database: Path
    ) -> None:
        self.plan(database)
        args = [
            "evidence",
            "rebuild-canary",
            "--live",
            "--database",
            str(database),
            "--expect-universe-count",
            "8",
        ]
        assert CliRunner().invoke(app, args).exit_code == 0
        fetched = len(sec_cli.calls)
        puts = len(artifact_calls(wired, "put_object"))

        result = CliRunner().invoke(app, args)

        assert result.exit_code == 0, result.stdout
        assert len(sec_cli.calls) == fetched, (
            "the preserved ticker map and the trusted corpus were reused"
        )
        assert len(artifact_calls(wired, "put_object")) == puts
