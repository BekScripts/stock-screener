"""The baseline rebuild over a four-chunk universe, against fakes.

Chunk 000 is the canary's; the driver takes 001 through the last, one
after another, and closes the baseline. What it must get right: chunks in
order and never two at once; a filer shared with an earlier chunk is one
catalogue and a `CACHE_HIT`; a completed chunk is a no-op on rerun; the
last chunk is partial; milestones walk the namespace without a body
fetch; the terminal gate downloads and hashes everything once; the
`BASELINE_COMPLETE` marker exists only after every gate; and every stop
condition stops before anything is overwritten.

No network: the SEC is the canned fake; the bucket is the fake client.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest
from botocore.exceptions import ReadTimeoutError
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
    BASELINE_MARKER,
    B2Configuration,
    B2EvidenceStorage,
    BaselineReport,
    Budget,
    CanaryStopError,
    ChunkStage,
    ControlMirror,
    EvidenceIntegrityError,
    Manifest,
    MirrorOutcome,
    RetryingReads,
    chunk_is_complete,
    load_checkpoint,
    open_control_root,
    prove_baseline_recovery,
    run_baseline,
    run_chunk,
    structural_reconciliation,
    v2_progress,
    verify_frozen_prerequisites,
)
from stock_screener.sec_evidence.rebuild import TICKER_MAP_URL, chunk_checkpoint_path

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from conftest import FakeS3Client, FakeSec
    from stock_screener.sec_evidence import EvidenceControlRoot, IdentityRow

pytestmark = pytest.mark.integration

B2_CONFIG = B2Configuration(
    endpoint_url="https://s3.us-west-004.backblazeb2.com",
    bucket="baseline-test-bucket",
    key_id=SecretStr("baseline-key-id"),
    application_key=SecretStr("baseline-application-key"),
)
GENERATION = "SEC_EVIDENCE_GENERATION_V2"
UNIVERSE_SHA = "b8" * 32

#: Live filers by symbol; the delisted ones carry stored CIKs.
TICKER_MAP = {
    "0": {"cik_str": 100, "ticker": "AAA", "title": "A"},
    "1": {"cik_str": 200, "ticker": "BBB", "title": "B"},
    "2": {"cik_str": 300, "ticker": "CCC", "title": "C"},
    "3": {"cik_str": 300, "ticker": "CCCB", "title": "C class B"},
    "4": {"cik_str": 400, "ticker": "DDD", "title": "D"},
    "5": {"cik_str": 500, "ticker": "EEE", "title": "E"},
    "6": {"cik_str": 100, "ticker": "AAAL", "title": "A, listed again"},
}


def universe(make: Callable[..., IdentityRow]) -> list[IdentityRow]:
    """Four chunks: 3, 3, 3 and a partial 2. Filer 100 sits in chunks 0 and 2; 300 in 1 and 3."""
    return [
        make(0, "LIVE", "AAA", chunk=0),
        make(1, "DELISTED", "GONE", chunk=0),
        make(2, "LIVE", "BBB", chunk=0),
        make(3, "LIVE", "CCC", chunk=1),
        make(4, "DELISTED", "OLD", cik=600, chunk=1),
        make(5, "LIVE", "NOMAP", chunk=1),
        make(6, "LIVE", "AAAL", chunk=2),
        make(7, "LIVE", "DDD", chunk=2),
        make(8, "DELISTED", "ZZZ", chunk=2),
        make(9, "LIVE", "CCCB", chunk=3),
        make(10, "LIVE", "EEE", chunk=3),
    ]


def filings(cik: int, count: int, form: str = "8-K") -> list[tuple[str, str, str, str]]:
    return [
        (f"{cik:010d}-24-{n:06d}", form, f"2024-{n:02d}-10", f"doc{cik}-{n}.htm")
        for n in range(1, count + 1)
    ]


@pytest.fixture
def sec(fake_sec: type[FakeSec]) -> FakeSec:
    fake = fake_sec(TICKER_MAP)
    fake.add_filer(100, filings(100, 2, "10-K"))
    fake.add_filer(200, filings(200, 1))
    fake.add_filer(300, filings(300, 8))  # capped at 6
    fake.add_filer(400, filings(400, 3, "10-Q"))
    fake.add_filer(500, filings(500, 1, "20-F"))
    fake.add_filer(600, filings(600, 2))
    return fake


@pytest.fixture
def identities(make_identity: Callable[..., IdentityRow]) -> list[IdentityRow]:
    return universe(make_identity)


def seed_control(root: Path, rows: list[IdentityRow]) -> EvidenceControlRoot:
    root.mkdir()
    control = open_control_root(Settings(sec_control_dir=str(root)))
    plans = control.plans / "generation_v2"
    plans.mkdir(parents=True)
    lines = "".join(row.line() + "\n" for row in rows)
    (plans / "base_identities.jsonl").write_text(lines)
    (plans / "base_identities.sha256").write_text(
        f"{hashlib.sha256(lines.encode()).hexdigest()}  base_identities.jsonl\n"
    )
    offline = "".join(row.line() + "\n" for row in rows if row.submissions_url)
    (plans / "base_targets_offline.jsonl").write_text(offline)
    (plans / "base_targets_offline.sha256").write_text(
        f"{hashlib.sha256(offline.encode()).hexdigest()}  base_targets_offline.jsonl\n"
    )
    (plans / "prerequisites.json").write_text("[]\n")
    (plans / "generation.json").write_text(
        json.dumps({"generation_id": GENERATION, "universe_sha256": UNIVERSE_SHA}) + "\n"
    )
    return control


@pytest.fixture
def control(tmp_path: Path, identities: list[IdentityRow]) -> EvidenceControlRoot:
    return seed_control(tmp_path / "control", identities)


def bucket(fake_s3: type[FakeS3Client]) -> tuple[B2EvidenceStorage, FakeS3Client]:
    client = fake_s3()
    return B2EvidenceStorage(B2_CONFIG, client=client), client


@pytest.fixture
def canary_done(
    fake_s3: type[FakeS3Client],
    control: EvidenceControlRoot,
    sec: FakeSec,
    identities: list[IdentityRow],
) -> tuple[B2EvidenceStorage, FakeS3Client]:
    """Chunk 000 COMPLETE, as the canary leaves it."""
    storage, client = bucket(fake_s3)
    report = run_chunk(storage, control, sec, identities, chunk=0)
    assert report.passed, report.failure
    return storage, client


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


def nested(facts: dict[str, object], key: str) -> dict[str, object]:
    """A nested facts dict, typed for the assertions."""
    inner = facts[key]
    assert isinstance(inner, dict)
    return inner


def drive(
    storage: B2EvidenceStorage,
    control: EvidenceControlRoot,
    sec: FakeSec,
    identities: list[IdentityRow],
    *,
    start: int = 1,
    end: int = 3,
    milestone_every: int = 2,
    hook: Callable[[int], None] | None = None,
    lines: list[str] | None = None,
    budget: Callable[[], Budget] = lambda: Budget(100, 0),
    sleep: Callable[[float], None] | None = None,
    retries: int = 2,
) -> BaselineReport:
    pauses: list[float] = []
    return run_baseline(
        storage,
        control,
        lambda budget: sec,
        identities,
        start=start,
        end=end,
        budget_factory=budget,
        progress=(lines if lines is not None else []).append,
        milestone_every=milestone_every,
        milestone_hook=hook,
        universe_sha256=UNIVERSE_SHA,
        retries_per_chunk=retries,
        retry_delay_s=60.0,
        sleep=sleep or pauses.append,
    )


# Expected trusted counts: chunk 0 → filers 100, 200: 2 catalogues + 2 + 1 filings = 5
# chunk 1 → filers 300, 600: 2 + 6 + 2 = 10 (total 15)
# chunk 2 → filers 100 (shared), 400: 1 new catalogue + 3 filings = 4 (total 19)
# chunk 3 → filers 300 (shared), 500: 1 new catalogue + 1 filing = 2 (total 21)


class TestProgression:
    def test_every_remaining_chunk_completes_in_order_and_the_baseline_closes(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        lines: list[str] = []

        report = drive(storage, control, sec, identities, lines=lines)

        assert report.passed, report.failure
        assert report.baseline_complete
        assert [c.chunk for c in report.chunks] == [1, 2, 3]
        assert all(c.stage == ChunkStage.COMPLETE for c in report.chunks)
        assert v2_progress(control) == (21, 4, [])
        assert len(trusted_paths(control)) == 21
        assert sorted(report.milestones) == [2, 3]
        assert report.terminal["verified"] == 21
        assert report.terminal["hash_mismatches"] == 0
        assert report.terminal["orphan_objects"] == 0
        assert nested(report.terminal, "recovery")["sufficient"] is True
        assert (control.plans / "generation_v2" / BASELINE_MARKER).is_file()
        assert f"control/{GENERATION}/{BASELINE_MARKER}" in client.objects
        assert any(line.startswith("chunk 001: COMPLETE") for line in lines)
        assert any(line.startswith("BASELINE_COMPLETE") for line in lines)

    def test_a_filer_shared_with_an_earlier_chunk_is_one_catalogue_and_a_cache_hit(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        puts_after_canary = len(artifact_calls(client, "put_object"))

        report = drive(storage, control, sec, identities)

        chunk2 = next(c for c in report.chunks if c.chunk == 2)
        assert chunk2.filers == 2
        assert chunk2.submissions_hit == 1, "filer 100's catalogue came from chunk 0"
        assert chunk2.submissions_new == 1
        assert chunk2.filings_hit == 2 and chunk2.filings_new == 3
        assert sec.calls.count("https://data.sec.gov/submissions/CIK0000000100.json") == 1
        puts = artifact_calls(client, "put_object")
        assert len(puts) == len(set(puts)), "no key was PUT twice"
        assert len(puts) - puts_after_canary == 16

    def test_the_last_partial_chunk_is_built_and_counted(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done

        report = drive(storage, control, sec, identities)

        last = report.chunks[-1]
        assert last.chunk == 3 and last.identities == 2 and last.resolved == 2
        checkpoint = load_checkpoint(chunk_checkpoint_path(control, 3))
        assert checkpoint is not None and checkpoint.selected == 2
        assert checkpoint.limit == 250 and checkpoint.offset == 750
        assert nested(report.terminal, "recovery")["chunks"] == 4
        assert nested(report.terminal, "recovery")["identities"] == 11

    def test_the_ticker_map_is_never_fetched_again(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done

        drive(storage, control, sec, identities)

        assert sec.calls.count(TICKER_MAP_URL) == 1

    def test_unresolved_identities_stay_unresolved_and_produce_no_target(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done

        drive(storage, control, sec, identities)

        rows = [
            json.loads(line)
            for line in (control.plans / "generation_v2" / "filer_resolution_v1.jsonl")
            .read_text()
            .splitlines()
        ]
        assert {r["symbol"] for r in rows if r["source"] == "UNRESOLVED"} == {
            "GONE",
            "NOMAP",
            "ZZZ",
        }
        assert not any("NOMAP" in url or "GONE" in url for url in sec.calls)


class TestRangeAndOrder:
    def test_chunk_000_cannot_be_reinitialised_and_ranges_must_be_ordered(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done
        for start, end in ((0, 3), (2, 1), (1, 4), (-1, 2)):
            with pytest.raises(ValueError, match="not within 001-003"):
                drive(storage, control, sec, identities, start=start, end=end)

    def test_a_chunk_needs_the_one_before_it_complete(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done

        report = run_chunk(storage, control, sec, identities, chunk=2)

        assert not report.passed
        assert "chunk 001 is absent, not COMPLETE" in (report.failure or "")
        assert v2_progress(control) == (5, 1, [])

    def test_the_driver_refuses_to_start_before_the_canary(
        self,
        fake_s3: type[FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = bucket(fake_s3)

        report = drive(storage, control, sec, identities)

        assert not report.passed
        assert "ticker map is missing" in (report.failure or "")
        assert sec.calls == []

    def test_completed_chunks_are_no_ops_and_the_run_continues_from_the_first_incomplete(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        first = drive(storage, control, sec, identities, end=2)
        assert first.passed and not first.baseline_complete
        calls_before = len(sec.calls)
        gets_before = len(artifact_calls(client, "get_object"))
        lines: list[str] = []

        second = drive(storage, control, sec, identities, lines=lines)

        assert second.passed and second.baseline_complete
        assert [(c.chunk, c.skipped) for c in second.chunks] == [(1, True), (2, True), (3, False)]
        assert lines[1] == "chunk 001: COMPLETE (no-op; frozen files verified)"
        assert sum(1 for u in sec.calls[calls_before:] if "/submissions/" in u) == 1
        gets = artifact_calls(client, "get_object")[gets_before:]
        assert gets.count("submissions/CIK0000000600.json") == 1, (
            "a chunk 001 artifact was downloaded once — by the terminal gate, not by a rerun"
        )

    def test_a_completed_chunk_with_a_tampered_frozen_file_is_not_a_no_op(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done
        drive(storage, control, sec, identities, end=1)
        targets = control.plans / "generation_v2" / "chunks" / "001" / "filing_targets.jsonl"
        targets.write_text(targets.read_text() + "\n")

        assert not chunk_is_complete(control, 1)


class TestResume:
    def test_an_interruption_mid_chunk_resumes_that_chunk_and_finishes(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        url = "https://www.sec.gov/Archives/edgar/data/400/000000040024000002/doc400-2.htm"
        sec.fail_always[url] = ProviderRequestError
        first = drive(storage, control, sec, identities)
        assert not first.passed
        assert first.next_incomplete == 2
        assert v2_progress(control) == (15 + 3, 2, [])  # chunk 2's other artifacts stayed trusted
        del sec.fail_always[url]
        puts_before = len(artifact_calls(client, "put_object"))

        second = drive(storage, control, sec, identities)

        assert second.passed and second.baseline_complete
        assert [(c.chunk, c.skipped) for c in second.chunks] == [(1, True), (2, False), (3, False)]
        resumed = next(c for c in second.chunks if c.chunk == 2)
        assert resumed.filings_new == 1 and resumed.filings_hit == 4
        assert len(artifact_calls(client, "put_object")) - puts_before == 1 + 2
        assert v2_progress(control) == (21, 4, [])

    def test_a_terminal_gate_that_fails_leaves_no_marker(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done

        def stray_after_the_last_chunk(chunk: int) -> None:
            if chunk == 3:
                client.objects["filings/0000000999/x/stray.htm"] = (b"?", {"sha256": "0" * 64})

        report = drive(storage, control, sec, identities, hook=stray_after_the_last_chunk)

        assert not report.passed
        assert "terminal verification failed" in (report.failure or "")
        assert "orphan_files" in (report.failure or "")
        assert not report.baseline_complete
        assert not (control.plans / "generation_v2" / BASELINE_MARKER).is_file()
        assert f"control/{GENERATION}/{BASELINE_MARKER}" not in client.objects
        assert v2_progress(control) == (21, 4, []), "chunk 003 itself completed"


class TestStopConditions:
    def test_a_sec_403_stops_before_the_next_request_and_names_the_chunk(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done
        sec.fail_always["https://data.sec.gov/submissions/CIK0000000300.json"] = ProviderAuthError

        report = drive(storage, control, sec, identities)

        assert not report.passed
        assert "SEC refused the caller (ProviderAuthError)" in (report.failure or "")
        assert report.next_incomplete == 1
        assert sec.calls[-1] == "https://data.sec.gov/submissions/CIK0000000300.json"

    def test_an_object_in_the_bucket_without_a_row_is_a_remote_orphan_and_is_not_overwritten(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        key = "filings/0000000300/000000030024000008/doc300-8.htm"  # a chunk 001 target
        client.objects[key] = (b"<html>someone else</html>", {"sha256": "0" * 64})

        report = drive(storage, control, sec, identities)

        assert not report.passed
        assert "REMOTE_ORPHAN" in (report.failure or "")
        assert key in (report.failure or "")
        assert client.objects[key][0] == b"<html>someone else</html>"
        assert key not in trusted_paths(control)

    def test_a_trusted_artifact_that_no_longer_hashes_stops_and_is_not_repaired(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        key = "submissions/CIK0000000100.json"  # chunk 0's, shared with chunk 2
        original, metadata = client.objects[key]
        client.objects[key] = (original[:-1] + b"X", metadata)
        fetches_before = len(sec.calls)

        report = drive(storage, control, sec, identities)

        assert not report.passed
        assert "REMOTE_UNTRUSTED" in (report.failure or "")
        assert key in (report.failure or "")
        assert report.next_incomplete == 2
        assert client.objects[key][0] == original[:-1] + b"X", "left exactly as found"
        assert "CIK0000000100" not in " ".join(sec.calls[fetches_before:])

    def test_a_stray_object_anywhere_stops_the_next_chunk_without_a_body_fetch(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        client.objects["submissions/CIK0000000777.json"] = (b"{}", {})
        gets_before = len(artifact_calls(client, "get_object"))

        report = drive(storage, control, sec, identities)

        assert not report.passed
        assert "orphan_files" in (report.failure or ""), "the chunk's own prefix walk found it"
        assert report.next_incomplete == 1
        new_gets = artifact_calls(client, "get_object")[gets_before:]
        assert "submissions/CIK0000000777.json" not in new_gets, "no body was fetched to find it"
        assert not chunk_is_complete(control, 1)

    def test_a_clean_milestone_walks_the_namespace_at_listing_cost_and_reports_it(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done

        report = drive(storage, control, sec, identities, milestone_every=1)

        assert sorted(report.milestones) == [1, 2, 3]
        after_one = report.milestones[1]
        assert after_one["objects"] == 15 == after_one["trusted"]
        assert after_one["orphan_objects"] == 0 and after_one["missing"] == 0
        assert after_one["completed_chunks"] == 2 and after_one["remaining_chunks"] == 2
        assert nested(after_one, "mirror_recovery")["newest_manifest_snapshot_is_current"] is True

    def test_the_milestone_hook_can_stop_the_run(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done

        def drift(chunk: int) -> None:
            raise CanaryStopError(f"database digest drift at chunk {chunk:03d}")

        report = drive(storage, control, sec, identities, hook=drift)

        assert not report.passed
        assert "database digest drift at chunk 002" in (report.failure or "")
        assert v2_progress(control)[1] == 3, "chunks 001 and 002 had completed"

    def test_frozen_resolution_drift_stops_before_any_chunk(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done
        resolution = control.plans / "generation_v2" / "filer_resolution_v1.jsonl"
        resolution.write_text(resolution.read_text().replace('"cik": 600', '"cik": 601'))
        calls_before = len(sec.calls)

        with pytest.raises(CanaryStopError, match="does not hash to its companion"):
            verify_frozen_prerequisites(control)
        report = drive(storage, control, sec, identities)

        assert not report.passed
        assert len(sec.calls) == calls_before

    def test_a_ticker_map_that_no_longer_matches_its_sidecar_stops_without_a_refetch(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done
        raw = control.plans / "generation_v2" / "prerequisites" / "company_tickers.json"
        raw.write_bytes(raw.read_bytes() + b" ")

        report = drive(storage, control, sec, identities)

        assert not report.passed
        assert "ticker map does not hash to its sidecar" in (report.failure or "")
        assert sec.calls.count(TICKER_MAP_URL) == 1

    def test_the_per_chunk_budget_stops_the_chunk_and_names_it(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done

        report = drive(storage, control, sec, identities, budget=lambda: Budget(3, 0))

        assert not report.passed
        assert "budget was reached" in (report.failure or "")
        assert report.next_incomplete == 1


class TestTransportFaults:
    def test_a_transient_bucket_timeout_is_resumed_and_the_chunk_completes(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        pauses: list[float] = []

        def outage_ends(seconds: float) -> None:
            pauses.append(seconds)
            client.failing.pop("put_object", None)

        armed = True

        def start_outage_mid_chunk_one(url: str) -> None:
            nonlocal armed
            if armed and url.endswith("doc300-5.htm"):
                armed = False
                client.failing["put_object"] = ReadTimeoutError(endpoint_url="https://b2")

        original_fetch = sec.fetch

        def fetch(url: str):  # type: ignore[no-untyped-def]  # test hook
            start_outage_mid_chunk_one(url)
            return original_fetch(url)

        sec.fetch = fetch  # type: ignore[method-assign]
        lines: list[str] = []

        report = drive(storage, control, sec, identities, lines=lines, sleep=outage_ends)

        assert report.passed, report.failure
        assert pauses == [60.0]
        one = next(c for c in report.chunks if c.chunk == 1)
        assert one.retries == 1
        assert one.stage == ChunkStage.COMPLETE
        assert any(
            "chunk 001: transport fault, resuming in 60 s (1 of 2)" in line for line in lines
        )
        assert "resumed 1x after transport faults" in one.line()
        puts = artifact_calls(client, "put_object")
        assert len(puts) == len(set(puts)) + 1, "only the timed-out key was PUT twice"
        assert v2_progress(control) == (21, 4, [])

    def test_a_persistent_bucket_outage_stops_after_the_bounded_retries(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        client.failing["put_object"] = ReadTimeoutError(endpoint_url="https://b2")
        pauses: list[float] = []

        report = drive(storage, control, sec, identities, sleep=pauses.append)

        assert not report.passed
        assert "ReadTimeoutError" in (report.failure or "")
        assert pauses == [60.0, 60.0]
        assert report.next_incomplete == 1
        assert report.chunks[0].retries == 2
        assert v2_progress(control) == (5, 1, [])

    def test_an_integrity_failure_is_never_retried(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        key = "filings/0000000300/000000030024000008/doc300-8.htm"
        body = sec.responses[
            "https://www.sec.gov/Archives/edgar/data/300/000000030024000008/doc300-8.htm"
        ]
        client.tamper_after_put[key] = (
            body[:-1] + b"X",
            {"sha256": hashlib.sha256(body).hexdigest()},
        )
        pauses: list[float] = []

        report = drive(storage, control, sec, identities, sleep=pauses.append)

        assert not report.passed
        assert "EvidenceIntegrityError" in (report.failure or "")
        assert "no manifest entry written" in (report.failure or "")
        assert pauses == [], "an integrity stop is not a transport fault"
        assert report.chunks[0].retries == 0
        assert artifact_calls(client, "put_object").count(key) == 1, "never re-PUT"
        assert key not in trusted_paths(control)


class TestTerminalGateReads:
    def test_a_stream_that_breaks_once_during_the_terminal_walk_is_retried(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        pauses: list[float] = []

        def break_one_stream_in_the_terminal_walk(chunk: int) -> None:
            if chunk == 3:
                client.failing["get_object"] = ReadTimeoutError(endpoint_url="https://b2")

        def stream_recovers(seconds: float) -> None:
            pauses.append(seconds)
            client.failing.pop("get_object", None)

        report = drive(
            storage,
            control,
            sec,
            identities,
            hook=break_one_stream_in_the_terminal_walk,
            sleep=stream_recovers,
        )

        assert report.passed, report.failure
        assert report.baseline_complete
        assert pauses == [5.0], "one read retry, not a restart of the walk"
        assert report.terminal["reads_retried"] == 1
        assert report.terminal["verified"] == 21

    def test_a_persistent_read_fault_in_the_terminal_walk_stops_without_a_marker(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        pauses: list[float] = []

        def break_every_stream(chunk: int) -> None:
            if chunk == 3:
                client.failing["get_object"] = ReadTimeoutError(endpoint_url="https://b2")

        report = drive(
            storage, control, sec, identities, hook=break_every_stream, sleep=pauses.append
        )

        assert not report.passed
        assert "ReadTimeoutError" in (report.failure or "")
        assert pauses == [5.0, 5.0]
        assert not report.baseline_complete
        assert not (control.plans / "generation_v2" / BASELINE_MARKER).is_file()
        assert v2_progress(control) == (21, 4, []), "every chunk stayed COMPLETE"

    def test_the_view_never_retries_a_write_or_an_integrity_error(
        self, fake_s3: type[FakeS3Client], tmp_path: Path
    ) -> None:
        storage, client = bucket(fake_s3)
        pauses: list[float] = []
        reads = RetryingReads(storage, sleep=pauses.append)
        client.failing["put_object"] = ReadTimeoutError(endpoint_url="https://b2")

        with pytest.raises(Exception, match="ReadTimeoutError"):
            reads.write_bytes("filings/0000000001/x/a.htm", b"a")
        assert pauses == [], "a write fault is the chunk resume's business"

        client.failing.clear()
        client.tamper_after_put["filings/0000000001/x/b.htm"] = (b"bb", {"sha256": "0" * 64})
        with pytest.raises(EvidenceIntegrityError):
            reads.write_bytes("filings/0000000001/x/b.htm", b"b")
        assert pauses == []


class TestControlMirrorAndRecovery:
    def test_every_completed_chunk_leaves_its_plans_checkpoint_and_summary_in_the_mirror(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done

        drive(storage, control, sec, identities)

        keys = set(client.objects)
        for chunk in range(4):
            prefix = f"control/{GENERATION}/chunks/{chunk:03d}/"
            for name in (
                "identities.jsonl",
                "identities.sha256",
                "filing_targets.jsonl",
                "filing_targets.sha256",
            ):
                assert prefix + name in keys
            assert any(k.startswith(prefix + "checkpoints/") for k in keys)
            assert any(k.startswith(prefix + "summaries/") for k in keys)
        descriptors = [
            k
            for k in keys
            if k.startswith(f"control/{GENERATION}/manifests/") and k.endswith(".snapshot.json")
        ]
        assert len(descriptors) >= 4 + 3 * 4, (
            "one per snapshot step of every chunk, plus the terminal one"
        )
        final = json.loads(client.objects[max(descriptors)][0])
        assert final["stage"] == "BASELINE_COMPLETE" and final["chunks"] == 4
        assert final["universe_sha256"] == UNIVERSE_SHA

    def test_the_marker_is_deterministic_and_a_rerun_is_a_mirror_hit(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done
        first = drive(storage, control, sec, identities)
        assert nested(first.terminal, "marker")["outcome"] == MirrorOutcome.MIRRORED
        marker = json.loads((control.plans / "generation_v2" / BASELINE_MARKER).read_text())
        assert marker["stage"] == "BASELINE_COMPLETE"
        assert marker["trusted"] == 21 and marker["chunks"] == 4
        assert (
            marker["manifest_sha256"]
            == hashlib.sha256(control.manifest_path.read_bytes()).hexdigest()
        )

        second = drive(storage, control, sec, identities)

        assert second.passed and second.baseline_complete
        assert all(c.skipped for c in second.chunks)
        assert nested(second.terminal, "marker")["outcome"] == MirrorOutcome.MIRROR_HIT
        assert second.sec_requests == 0

    def test_recovery_from_the_bucket_alone_reconstructs_the_generation(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, _ = canary_done
        drive(storage, control, sec, identities)
        mirror = ControlMirror(storage, GENERATION)

        facts = prove_baseline_recovery(mirror, storage, control, 4)

        assert facts["sufficient"] is True
        assert facts["trusted"] == 21 == facts["bucket_objects"]
        assert facts["identities"] == 11 and facts["chunks"] == 4
        assert facts["universe_sha256"] == UNIVERSE_SHA

    def test_a_mirror_conflict_stops_the_chunk(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
        sec: FakeSec,
        identities: list[IdentityRow],
    ) -> None:
        storage, client = canary_done
        client.objects[f"control/{GENERATION}/chunks/001/identities.jsonl"] = (b"other\n", {})

        report = drive(storage, control, sec, identities)

        assert not report.passed
        assert "MirrorConflictError" in (report.failure or "")
        assert client.objects[f"control/{GENERATION}/chunks/001/identities.jsonl"][0] == b"other\n"
        assert report.next_incomplete == 1

    def test_structural_reconciliation_reports_missing_and_orphans_at_listing_cost(
        self,
        canary_done: tuple[B2EvidenceStorage, FakeS3Client],
        control: EvidenceControlRoot,
    ) -> None:
        storage, client = canary_done
        del client.objects["submissions/CIK0000000200.json"]
        client.objects["fulltext/stray.json"] = (b"{}", {})
        calls_before = len(client.calls)

        counts = structural_reconciliation(storage, Manifest(control.manifest_path))

        assert counts == {
            "objects": 5,
            "trusted": 5,
            "orphan_objects": 1,
            "missing": 1,
            "path_violations": 0,
        }
        assert {op for op, _ in client.calls[calls_before:]} == {"list_objects_v2"}


class TestTheCommand:
    @pytest.fixture
    def environment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
        control = tmp_path / "control"
        control.mkdir()
        monkeypatch.setenv("ENVIRONMENT", "test")
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control))
        monkeypatch.setenv("B2_ENDPOINT_URL", B2_CONFIG.endpoint_url)
        monkeypatch.setenv("B2_BUCKET", B2_CONFIG.bucket)
        monkeypatch.setenv("B2_KEY_ID", "baseline-key-id")
        monkeypatch.setenv("B2_APPLICATION_KEY", "baseline-application-key")
        get_settings.cache_clear()
        yield control
        get_settings.cache_clear()

    @pytest.fixture
    def database(self, tmp_path: Path) -> Path:
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
    def sec_cli(self, fake_sec: type[FakeSec], monkeypatch: pytest.MonkeyPatch) -> FakeSec:
        fake = fake_sec(
            {
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
                "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
            }
        )
        fake.add_filer(320193, filings(320193, 2, "10-K"))
        fake.add_filer(789019, filings(789019, 1))
        fake.add_filer(886158, filings(886158, 1))
        fake.add_filer(1060736, filings(1060736, 7))
        monkeypatch.setattr("stock_screener.cli._sec_documents", lambda settings, budget: fake)
        return fake

    def test_without_live_nothing_is_fetched_or_written(
        self, wired: FakeS3Client, sec_cli: FakeSec, database: Path
    ) -> None:
        result = CliRunner().invoke(
            app, ["evidence", "rebuild-baseline", "--database", str(database)]
        )

        assert result.exit_code == 1
        assert "refusing without --live" in result.stdout
        assert wired.calls == [] and sec_cli.calls == []

    def test_chunk_000_is_refused_as_a_start(
        self, wired: FakeS3Client, sec_cli: FakeSec, database: Path
    ) -> None:
        result = CliRunner().invoke(
            app,
            ["evidence", "rebuild-baseline", "--live", "--start", "0", "--database", str(database)],
        )

        assert result.exit_code != 0
        assert wired.calls == [] and sec_cli.calls == []

    def test_the_command_runs_the_baseline_over_a_seeded_universe(
        self,
        environment: Path,
        wired: FakeS3Client,
        sec_cli: FakeSec,
        database: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Eight identities make one 250-chunk, so the chunk size is patched to five."""
        monkeypatch.setattr("stock_screener.sec_evidence.generation.PRODUCTION_CHUNK", 5)
        monkeypatch.setattr("stock_screener.sec_evidence.rebuild.PRODUCTION_CHUNK", 5)
        runner = CliRunner()
        plan = runner.invoke(
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
        assert plan.exit_code == 0, plan.stdout
        canary = runner.invoke(
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
        assert canary.exit_code == 0, canary.stdout
        digest = hashlib.sha256(database.read_bytes()).hexdigest()

        result = runner.invoke(
            app,
            [
                "evidence",
                "rebuild-baseline",
                "--live",
                "--database",
                str(database),
                "--expect-universe-count",
                "8",
                "--expect-database-sha",
                f"{database}={digest}",
            ],
        )

        assert result.exit_code == 0, result.stdout
        assert "BASELINE COMPLETE" in result.stdout
        assert "chunk 001: COMPLETE" in result.stdout
        assert "databases verified" in result.stdout
        assert "baseline-application-key" not in result.stdout
        assert "baseline-key-id" not in result.stdout
        status = runner.invoke(app, ["evidence", "status"])
        assert "baseline         : BASELINE_COMPLETE — 2 chunks" in status.stdout

    def test_a_database_digest_that_drifts_stops_before_any_request(
        self, environment: Path, wired: FakeS3Client, sec_cli: FakeSec, database: Path
    ) -> None:
        result = CliRunner().invoke(
            app,
            [
                "evidence",
                "rebuild-baseline",
                "--live",
                "--database",
                str(database),
                "--expect-database-sha",
                f"{database}={'0' * 64}",
            ],
        )

        assert result.exit_code == 1
        assert "database digest drift" in result.stdout
        assert sec_cli.calls == []
