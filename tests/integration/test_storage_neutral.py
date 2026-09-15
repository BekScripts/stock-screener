"""Proof that the evidence runtime needs a storage backend and not a disk.

Phase B2.3 routed harvesting, parsing, cache verification and reconciliation
through `EvidenceStorage`. The claim worth testing is not that the local
backend still works — the pre-B2 suites already say so — but that **nothing
above the backend needs a `Path`**. The only way to show that is to run the
same functions against a backend that has no path to give: the dict-backed
`MemoryEvidenceStorage` from `conftest`.

Every scenario here is built through the storage contract alone, so the same
code runs against the local backend too. Where it does, the two must agree —
that is the equivalence Phase B2.2 promised, restated for the audit.

The manifest is a control-plane file and still lives on disk. That is
deliberate (B2.5 owns its relocation) and is why these are integration tests
rather than unit tests: `tmp_path` holds the manifest, never an artifact.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from api_clients.sec_documents import FetchedDocument
from stock_screener.sec_evidence import (
    ARTIFACT_PREFIXES,
    Budget,
    Entry,
    LocalEvidenceStorage,
    Manifest,
    Outcome,
    RequestKind,
    StorageUnavailableError,
    Target,
    UnsafeStorageKeyError,
    build_index,
    filing_key,
    filing_targets,
    harvest_artifacts,
    reconcile,
    reconcile_subset,
    resolve_ciks,
    submissions_key,
    verify_digests_of,
)
from stock_screener.sec_evidence.sample import Selection

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from conftest import MemoryEvidenceStorage
    from stock_screener.sec_evidence import EvidenceStorage

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

    def __init__(self, payload: bytes) -> None:
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
            retrieved_at=datetime(2026, 9, 14, tzinfo=UTC),
        )

    def close(self) -> None:
        return None


def budget() -> Budget:
    return Budget(max_requests=10, max_bytes=10**6)


def trusted(key: str, payload: bytes) -> Entry:
    """A DOWNLOADED manifest record vouching for `payload` under `key`.

    The URL, CIK and accession are derived from the key so that the record is
    internally consistent — the audit checks that a path agrees with its URL,
    and a helper that produced contradictory ones would fail every scenario
    for the wrong reason.
    """
    segments = key.split("/")
    cik: int | None = None
    accession: str | None = None
    url = f"https://www.sec.gov/{key}"
    kind = "FILING"
    if segments[0] == "submissions" and len(segments) == 2:
        kind = "SUBMISSIONS"
        cik = int(segments[1].removeprefix("CIK").removesuffix(".json"))
        url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    elif segments[0] == "filings" and len(segments) == 4:
        cik = int(segments[1])
        accession = segments[2]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{segments[2]}/{segments[3]}"
    return Entry(
        url=url,
        outcome=Outcome.DOWNLOADED,
        status=200,
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
        retrieved_at="2026-09-14T00:00:00+00:00",
        path=key,
        request_kind=kind,
        cik=cik,
        accession=accession,
    )


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    """The control-plane log. On disk by design; it holds no artifact bytes."""
    (tmp_path / "manifests").mkdir()
    return Manifest(tmp_path / "manifests" / "evidence.jsonl")


@pytest.fixture
def derived(tmp_path: Path) -> Path:
    """Where a derived index goes — control plane, so a path is correct here."""
    (tmp_path / "derived").mkdir()
    return tmp_path / "derived" / "index.db"


@pytest.fixture(params=["memory", "local"])
def backend(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    memory_storage: type[MemoryEvidenceStorage],
) -> EvidenceStorage:
    """Both backends, so every scenario is also an equivalence check."""
    factories: dict[str, Callable[[], EvidenceStorage]] = {
        "memory": memory_storage,
        "local": lambda: LocalEvidenceStorage(_artifact_root(tmp_path)),
    }
    return factories[request.param]()


def _artifact_root(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir(exist_ok=True)
    return root


class TestReconciliationIsBackendNeutral:
    """The full audit, built through the contract and run against both backends.

    Ordinary artifact counts retain their meaning. Unsafe keys are explicitly
    path violations, and storage failures intentionally propagate.
    """

    def test_a_trusted_artifact_reconciles_clean(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        key = "submissions/CIK0000000001.json"
        backend.write_bytes(key, b'{"ok":1}')
        manifest.append(trusted(key, b'{"ok":1}'))

        state = reconcile(backend, manifest, verify_digests=True)

        assert state.clean
        assert state.trusted == 1
        assert state.files_on_disk == 1
        assert state.logical_bytes == 8

    def test_bytes_nobody_vouches_for_are_an_orphan_file(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        backend.write_bytes("filings/0000000001/x/stray.htm", b"junk")

        state = reconcile(backend, manifest, verify_digests=True)

        assert state.orphan_files == 1
        assert state.trusted == 0
        assert not state.clean

    def test_a_trusted_record_with_no_object_is_an_orphan_record(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        manifest.append(trusted("filings/0000000001/x/gone.htm", b"never written"))

        state = reconcile(backend, manifest, verify_digests=True)

        assert state.orphan_records == 1
        assert state.files_on_disk == 0
        assert not state.clean

    def test_an_object_of_the_wrong_size_is_a_byte_mismatch(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        key = "filings/0000000001/x/short.htm"
        backend.write_bytes(key, b"truncated")
        manifest.append(trusted(key, b"the complete original document"))

        state = reconcile(backend, manifest, verify_digests=True)

        assert state.byte_mismatches == 1
        assert state.hash_mismatches == 1, "different bytes cannot share a digest"
        assert not state.clean

    def test_an_object_of_the_right_size_and_wrong_bytes_is_a_hash_mismatch(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        """Size agreeing is not integrity — only the digest is."""
        key = "filings/0000000001/x/swapped.htm"
        backend.write_bytes(key, b"BBBBBBBB")
        manifest.append(trusted(key, b"AAAAAAAA"))

        state = reconcile(backend, manifest, verify_digests=True)

        assert state.hash_mismatches == 1
        assert state.byte_mismatches == 0
        assert not state.clean

    def test_a_trusted_zero_byte_object_is_reported(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        key = "filings/0000000001/x/empty.htm"
        backend.write_bytes(key, b"")
        manifest.append(trusted(key, b""))

        state = reconcile(backend, manifest, verify_digests=True)

        assert state.zero_byte == 1
        assert state.hash_mismatches == 0, "the digest of nothing is still the digest recorded"
        assert not state.clean

    def test_an_unsafe_manifest_key_is_a_path_violation_not_a_lookup(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        manifest.append(trusted("../outside.htm", b"x"))

        state = reconcile(backend, manifest, verify_digests=True)

        assert state.path_violations == 1
        assert state.orphan_records == 0
        assert not state.clean

    def test_every_scenario_at_once_counts_each_exactly_once(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        good = "submissions/CIK0000000001.json"
        backend.write_bytes(good, b'{"ok":1}')
        manifest.append(trusted(good, b'{"ok":1}'))
        backend.write_bytes("fulltext/orphan.json", b"nobody vouched")
        manifest.append(trusted("filings/0000000002/x/gone.htm", b"never written"))
        short = "filings/0000000003/x/short.htm"
        backend.write_bytes(short, b"trunc")
        manifest.append(trusted(short, b"the whole thing"))
        empty = "filings/0000000004/x/empty.htm"
        backend.write_bytes(empty, b"")
        manifest.append(trusted(empty, b""))

        state = reconcile(backend, manifest, verify_digests=True)

        assert state.manifest_records == 4
        assert state.trusted == 4
        assert state.files_on_disk == 4
        assert state.orphan_files == 1
        assert state.orphan_records == 1
        assert state.byte_mismatches == 1
        assert state.hash_mismatches == 1
        assert state.zero_byte == 1
        assert state.temporary_files == 0


class TestTheFullAuditEnumeratesExactlyTheArtifactPrefixes:
    def test_the_full_pass_lists_each_artifact_prefix_once(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage()

        reconcile(storage, manifest, verify_digests=True)

        assert storage.listed == list(ARTIFACT_PREFIXES)

    def test_a_control_plane_prefix_is_never_listed(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        """Even a backend holding control-plane keys is not asked about them."""
        storage = memory_storage()
        storage.write_bytes("manifests/evidence.jsonl", b"{}")
        storage.write_bytes("checkpoints/chunk-001.json", b"{}")
        storage.write_bytes("derived/index.db", b"")

        state = reconcile(storage, manifest, verify_digests=True)

        assert state.orphan_files == 0
        assert state.files_on_disk == 0
        assert not any(
            p.startswith(("manifests", "checkpoints", "derived")) for p in storage.listed
        )


class TestSubsetReconciliationStaysScoped:
    """Per-batch verification is O(batch). A backend that records what it was
    asked to list makes that a fact rather than a hope."""

    @pytest.mark.parametrize(
        ("actual", "recorded", "expected"),
        [
            (b"good", b"good", {}),
            (None, b"gone", {"orphan_records": 1}),
            (b"short", b"long original", {"byte_mismatches": 1, "hash_mismatches": 1}),
            (b"BBBB", b"AAAA", {"hash_mismatches": 1}),
            (b"", b"", {"zero_byte": 1}),
        ],
    )
    def test_subset_counters_match_the_full_audit(
        self,
        backend: EvidenceStorage,
        manifest: Manifest,
        actual: bytes | None,
        recorded: bytes,
        expected: dict[str, int],
    ) -> None:
        entry = trusted(FILING_KEY, recorded)
        manifest.append(entry)
        if actual is not None:
            backend.write_bytes(FILING_KEY, actual)

        scoped = reconcile_subset(backend, manifest, [entry.url])
        full = reconcile(backend, manifest)

        assert scoped.problems == full.problems == expected

    def test_current_older_trusted_and_orphan_siblings_are_distinguished(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage()
        prefix = "filings/0000000001/acc"
        for name in ("current", "older", "orphan"):
            key = f"{prefix}/{name}.htm"
            storage.write_bytes(key, name.encode())
            if name != "orphan":
                manifest.append(trusted(key, name.encode()))

        state = reconcile_subset(
            storage, manifest, [trusted(f"{prefix}/current.htm", b"current").url]
        )

        assert state.trusted == 1
        assert state.files_on_disk == 3
        assert state.orphan_files == 1
        assert state.examples["orphan_files"] == [f"{prefix}/orphan.htm"]
        assert state.orphan_records == state.hash_mismatches == state.byte_mismatches == 0
        assert storage.listed == [prefix]

    @pytest.mark.parametrize("key", ["../outside.htm", "manifests/evidence.jsonl", "/abs.htm"])
    @pytest.mark.parametrize("subset", [False, True])
    def test_an_unsafe_record_is_only_a_path_violation(
        self,
        memory_storage: type[MemoryEvidenceStorage],
        manifest: Manifest,
        key: str,
        subset: bool,
    ) -> None:
        storage = memory_storage(failing=frozenset({"exists", "stat", "digest", "read_bytes"}))
        entry = trusted(key, b"x")
        manifest.append(entry)

        state = (
            reconcile_subset(storage, manifest, [entry.url])
            if subset
            else reconcile(storage, manifest)
        )

        assert state.path_violations == 1
        assert state.orphan_records == 0
        assert state.orphan_files == 0
        assert not state.clean
        if subset:
            assert storage.listed == []

    def test_a_batch_lists_only_the_prefixes_its_own_artifacts_live_under(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage()
        for cik in range(1, 6):
            key = f"filings/{cik:010d}/acc/doc.htm"
            storage.write_bytes(key, b"x")
            manifest.append(trusted(key, b"x"))
        mine = trusted("filings/0000000003/acc/doc.htm", b"x")

        state = reconcile_subset(storage, manifest, [mine.url], verify_digests=True)

        assert state.clean
        assert storage.listed == ["filings/0000000003/acc"]
        assert "" not in storage.listed
        assert "filings" not in storage.listed

    def test_a_stray_object_beside_the_batch_is_an_orphan(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        key = "filings/0000000001/acc/doc.htm"
        backend.write_bytes(key, b"x")
        manifest.append(trusted(key, b"x"))
        backend.write_bytes("filings/0000000001/acc/stray.htm", b"junk")

        state = reconcile_subset(backend, manifest, [trusted(key, b"x").url], verify_digests=True)

        assert state.orphan_files == 1
        assert state.files_on_disk == 2
        assert not state.clean

    def test_an_earlier_batch_in_the_same_prefix_is_claimed_not_orphaned(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        earlier = "submissions/CIK0000000001.json"
        later = "submissions/CIK0000000002.json"
        backend.write_bytes(earlier, b"1")
        backend.write_bytes(later, b"2")
        manifest.append(trusted(earlier, b"1"))
        manifest.append(trusted(later, b"2"))

        state = reconcile_subset(backend, manifest, [trusted(later, b"2").url], verify_digests=True)

        assert state.orphan_files == 0
        assert state.trusted == 1
        assert state.clean

    def test_a_stray_object_outside_the_batch_prefixes_is_out_of_scope(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        """The deliberate limit — and why the scheduled full pass is kept."""
        key = "filings/0000000001/acc/doc.htm"
        backend.write_bytes(key, b"x")
        manifest.append(trusted(key, b"x"))
        backend.write_bytes("filings/0000000009/acc/stray.htm", b"junk")

        scoped = reconcile_subset(backend, manifest, [trusted(key, b"x").url], verify_digests=True)
        full = reconcile(backend, manifest, verify_digests=True)

        assert scoped.orphan_files == 0
        assert full.orphan_files == 1

    def test_the_subset_and_full_passes_agree_on_the_named_artifacts(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        keys = [f"filings/{cik:010d}/acc/doc.htm" for cik in range(1, 8)]
        for cik, key in enumerate(keys, start=1):
            backend.write_bytes(key, f"payload {cik}".encode())
            manifest.append(trusted(key, f"payload {cik}".encode()))
        backend.write_bytes(keys[4], b"tampered")

        scoped = reconcile_subset(
            backend, manifest, [trusted(k, b"").url for k in keys], verify_digests=True
        )
        full = reconcile(backend, manifest, verify_digests=True)

        assert scoped.hash_mismatches == full.hash_mismatches == 1
        assert scoped.trusted == full.trusted == 7


class TestDigestVerificationTakesStorageNotPaths:
    def test_an_unsafe_key_is_not_counted_as_missing(self, backend: EvidenceStorage) -> None:
        with pytest.raises(UnsafeStorageKeyError, match="dot segment"):
            verify_digests_of(backend, [trusted("../escape.htm", b"x")])

    def test_digests_are_recomputed_from_the_backend(
        self, backend: EvidenceStorage, manifest: Manifest
    ) -> None:
        good = "filings/0000000001/acc/good.htm"
        bad = "filings/0000000001/acc/bad.htm"
        gone = "filings/0000000001/acc/gone.htm"
        backend.write_bytes(good, b"good")
        backend.write_bytes(bad, b"not what was recorded")
        entries = [trusted(good, b"good"), trusted(bad, b"recorded"), trusted(gone, b"never")]

        result = verify_digests_of(backend, entries)

        assert result == {"checked": 2, "hash_mismatches": 1, "byte_mismatches": 1, "missing": 1}


class TestHarvestingThroughAMemoryBackend:
    """The harvester's artifact path, with no directory anywhere in sight."""

    def test_a_fetched_document_lands_under_its_key_and_in_the_manifest(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage()
        source = Source(COVER)

        report = harvest_artifacts([FILING], storage, manifest, source, budget=budget())

        assert report.by_outcome == {"DOWNLOADED": 1}
        assert storage.exists(FILING_KEY)
        assert storage.digest(FILING_KEY) == hashlib.sha256(COVER).hexdigest()
        (entry,) = manifest.entries()
        assert entry.path == FILING_KEY
        assert entry.sha256 == hashlib.sha256(COVER).hexdigest()
        assert entry.bytes == len(COVER)

    def test_replay_is_a_cache_hit_decided_by_the_backend(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage()
        source = Source(COVER)
        harvest_artifacts([FILING], storage, manifest, source, budget=budget())

        replay = harvest_artifacts([FILING], storage, manifest, source, budget=budget())

        assert replay.by_outcome == {"CACHE_HIT": 1}
        assert source.calls == 1

    def test_a_tampered_object_is_refetched(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage()
        source = Source(COVER)
        harvest_artifacts([FILING], storage, manifest, source, budget=budget())
        storage.write_bytes(FILING_KEY, b"tampered")

        replay = harvest_artifacts([FILING], storage, manifest, source, budget=budget())

        assert replay.by_outcome == {"INVALID_CACHE": 1}
        assert source.calls == 2
        assert storage.digest(FILING_KEY) == hashlib.sha256(COVER).hexdigest()

    def test_a_vanished_object_is_refetched(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage()
        source = Source(COVER)
        harvest_artifacts([FILING], storage, manifest, source, budget=budget())
        storage = memory_storage()  # the same manifest, an empty backend

        replay = harvest_artifacts([FILING], storage, manifest, source, budget=budget())

        assert replay.by_outcome == {"INVALID_CACHE": 1}
        assert storage.exists(FILING_KEY)

    def test_a_dry_run_writes_nothing_to_the_backend(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage()
        source = Source(COVER)

        harvest_artifacts([FILING], storage, manifest, source, budget=budget(), dry_run=True)

        assert source.calls == 0
        assert list(storage.list_prefix("")) == []

    def test_the_harvest_then_reconciles_clean_through_the_same_backend(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage()
        harvest_artifacts([FILING], storage, manifest, Source(COVER), budget=budget())

        state = reconcile_subset(storage, manifest, [FILING.url], verify_digests=True)

        assert state.clean
        assert state.trusted == 1


class TestParsingThroughAMemoryBackend:
    @pytest.mark.parametrize("key", ["../outside.htm", "manifests/evidence.jsonl", ""])
    def test_unsafe_manifest_keys_are_integrity_errors(
        self,
        memory_storage: type[MemoryEvidenceStorage],
        manifest: Manifest,
        derived: Path,
        key: str,
    ) -> None:
        manifest.append(trusted(key, b"x"))

        with pytest.raises(UnsafeStorageKeyError, match=r"dot segment|artifact prefixes|empty"):
            build_index(memory_storage(), manifest, derived)

    def test_non_cover_text_keeps_the_tolerant_parser_behavior(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest, derived: Path
    ) -> None:
        storage = memory_storage()
        storage.write_bytes(FILING_KEY, b"not a cover page")
        manifest.append(trusted(FILING_KEY, b"not a cover page"))

        report = build_index(storage, manifest, derived)

        assert report.filings_parsed == 1
        assert report.parse_failures == 0
        assert report.security_rows == 0
        assert report.missing_files == 0

    @pytest.mark.parametrize("payload", [b"invalid json", b"\xff", b"[]"])
    def test_malformed_submissions_still_expand_to_nothing(
        self, memory_storage: type[MemoryEvidenceStorage], payload: bytes
    ) -> None:
        storage = memory_storage()
        storage.write_bytes(submissions_key(320193), payload)
        resolutions = resolve_ciks(
            [
                Selection(
                    bucket="DELISTED_WITH_CIK",
                    symbol="AAPL",
                    name="AAPL",
                    cik=320193,
                    historical_security_id=1,
                )
            ],
            None,
        )

        assert filing_targets(storage, resolutions) == []

    def test_the_index_is_built_from_backend_bytes(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest, derived: Path
    ) -> None:
        storage = memory_storage()
        harvest_artifacts([FILING], storage, manifest, Source(COVER), budget=budget())

        report = build_index(storage, manifest, derived)

        assert report.filings_seen == 1
        assert report.filings_parsed == 1
        assert report.security_rows == 1
        assert report.missing_files == 0

    def test_a_vanished_object_is_counted_missing_not_fatal(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest, derived: Path
    ) -> None:
        storage = memory_storage()
        manifest.append(trusted("filings/0000000001/acc/gone.htm", b"never written"))

        report = build_index(storage, manifest, derived)

        assert report.missing_files == 1
        assert report.parse_failures == 0

    def test_filing_expansion_reads_the_submissions_object(
        self, memory_storage: type[MemoryEvidenceStorage]
    ) -> None:
        storage = memory_storage()
        payload = {
            "cik": "0000320193",
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-24-000001"],
                    "form": ["10-K"],
                    "filingDate": ["2024-11-01"],
                    "reportDate": [""],
                    "primaryDocument": ["aapl.htm"],
                }
            },
        }
        storage.write_bytes(submissions_key(320193), json.dumps(payload).encode())
        resolutions = resolve_ciks(
            [
                Selection(
                    bucket="DELISTED_WITH_CIK",
                    symbol="AAPL",
                    name="AAPL",
                    cik=320193,
                    historical_security_id=1,
                )
            ],
            None,
        )

        (target,) = filing_targets(storage, resolutions, filings_per_security=5)

        assert target.accession == "0000320193-24-000001"
        assert target.document == "aapl.htm"

    def test_an_unharvested_submissions_object_expands_to_nothing(
        self, memory_storage: type[MemoryEvidenceStorage]
    ) -> None:
        storage = memory_storage()
        resolutions = resolve_ciks(
            [
                Selection(
                    bucket="DELISTED_WITH_CIK",
                    symbol="AAPL",
                    name="AAPL",
                    cik=320193,
                    historical_security_id=1,
                )
            ],
            None,
        )

        assert filing_targets(storage, resolutions, filings_per_security=5) == []


class TestBackendFailuresStayLoud:
    """A fault is a fault. It is never "absent", never "empty", never "miss"."""

    @pytest.mark.parametrize("subset", [False, True])
    @pytest.mark.parametrize("operation", ["exists", "stat", "digest"])
    def test_per_artifact_uncertainty_aborts_the_audit(
        self,
        memory_storage: type[MemoryEvidenceStorage],
        manifest: Manifest,
        subset: bool,
        operation: str,
    ) -> None:
        storage = memory_storage(failing=frozenset({operation}))
        storage.write_bytes(FILING_KEY, COVER)
        entry = trusted(FILING_KEY, COVER)
        manifest.append(entry)

        with pytest.raises(StorageUnavailableError, match=operation):
            if subset:
                reconcile_subset(storage, manifest, [entry.url])
            else:
                reconcile(storage, manifest)

    def test_raw_io_error_is_not_swallowed_by_the_parser(
        self,
        memory_storage: type[MemoryEvidenceStorage],
        manifest: Manifest,
        derived: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_read(self: MemoryEvidenceStorage, key: str) -> bytes:
            raise OSError("raw read failure")

        monkeypatch.setattr(memory_storage, "read_bytes", fail_read)
        manifest.append(trusted(FILING_KEY, COVER))

        with pytest.raises(OSError, match="raw read failure"):
            build_index(memory_storage(), manifest, derived)

    def test_a_list_failure_does_not_become_zero_orphans(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage(failing=frozenset({"list_prefix"}))
        key = "filings/0000000001/acc/doc.htm"
        storage.write_bytes(key, b"x")
        manifest.append(trusted(key, b"x"))

        with pytest.raises(StorageUnavailableError, match="list_prefix"):
            reconcile(storage, manifest, verify_digests=True)

    def test_a_list_failure_stops_the_subset_pass_too(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage(failing=frozenset({"list_prefix"}))
        key = "filings/0000000001/acc/doc.htm"
        storage.write_bytes(key, b"x")
        manifest.append(trusted(key, b"x"))

        with pytest.raises(StorageUnavailableError, match="list_prefix"):
            reconcile_subset(storage, manifest, [trusted(key, b"x").url], verify_digests=True)

    def test_a_stat_failure_stops_reconciliation(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage(failing=frozenset({"stat"}))
        key = "filings/0000000001/acc/doc.htm"
        storage.write_bytes(key, b"x")
        manifest.append(trusted(key, b"x"))

        with pytest.raises(StorageUnavailableError, match="stat"):
            reconcile(storage, manifest, verify_digests=False)

    def test_a_digest_failure_does_not_become_a_cache_miss(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        """Refetching over a fault would spend requests to paper over an outage."""
        storage = memory_storage()
        source = Source(COVER)
        harvest_artifacts([FILING], storage, manifest, source, budget=budget())
        storage.failing = frozenset({"digest"})

        with pytest.raises(StorageUnavailableError, match="digest"):
            harvest_artifacts([FILING], storage, manifest, source, budget=budget())

        assert source.calls == 1

    def test_a_write_failure_records_no_provenance(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest
    ) -> None:
        storage = memory_storage(failing=frozenset({"write_bytes"}))

        with pytest.raises(StorageUnavailableError, match="write_bytes"):
            harvest_artifacts([FILING], storage, manifest, Source(COVER), budget=budget())

        assert list(manifest.entries()) == [], "nothing vouched for, nothing recorded"

    def test_a_read_failure_does_not_become_a_missing_file(
        self, memory_storage: type[MemoryEvidenceStorage], manifest: Manifest, derived: Path
    ) -> None:
        storage = memory_storage()
        harvest_artifacts([FILING], storage, manifest, Source(COVER), budget=budget())
        storage.failing = frozenset({"read_bytes"})

        with pytest.raises(StorageUnavailableError, match="read_bytes"):
            build_index(storage, manifest, derived)

    def test_a_read_failure_stops_filing_expansion(
        self, memory_storage: type[MemoryEvidenceStorage]
    ) -> None:
        storage = memory_storage()
        storage.write_bytes(submissions_key(320193), b"{}")
        storage.failing = frozenset({"read_bytes"})
        resolutions = resolve_ciks(
            [
                Selection(
                    bucket="DELISTED_WITH_CIK",
                    symbol="AAPL",
                    name="AAPL",
                    cik=320193,
                    historical_security_id=1,
                )
            ],
            None,
        )

        with pytest.raises(StorageUnavailableError, match="read_bytes"):
            filing_targets(storage, resolutions, filings_per_security=5)
