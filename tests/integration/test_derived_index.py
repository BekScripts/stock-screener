"""The V2 derived index build: one read per artifact, ledgered, resumable, fingerprinted.

What must hold, against a backend that is a dict: every trusted artifact is
downloaded once and that download is both the verification and the parse;
bytes that do not reproduce the manifest stop the build with nothing
recorded; an interrupted build resumes without re-reading what it committed
and ends with the same fingerprints as an uninterrupted one; a different
manifest is refused; a parser failure is ledgered and blocks completion; two
builds of one corpus fingerprint identically; and the finished index opens
for every existing consumer. The mirror carries the small metadata and never
the index.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from stock_screener.cli import app
from stock_screener.config import get_settings
from stock_screener.sec_evidence import (
    ARTIFACT_PREFIXES,
    INDEX_BUILDING_FILE,
    INDEX_COMPLETE_MARKER,
    INDEX_FILE,
    INDEX_FINGERPRINTS_FILE,
    INDEX_META_FILE,
    ArtifactOutcome,
    B2Configuration,
    B2EvidenceStorage,
    ControlMirror,
    Entry,
    IndexBuildError,
    Manifest,
    Outcome,
    ResumeRefusedError,
    StorageUnavailableError,
    build_derived_index,
    filing_key,
    index_complete_facts,
    mirror_index_metadata,
    planned_inputs,
    submissions_key,
)
from stock_screener.sec_evidence.coverage import (
    catalogue_opportunity,
    class_breakdown,
    form_value,
    span_statistics,
)
from stock_screener.sec_evidence.targets import held_keys

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from conftest import FakeS3Client, MemoryEvidenceStorage
    from stock_screener.sec_evidence import ArtifactStat, StoredArtifact

pytestmark = pytest.mark.integration


def cover(title: str, symbol: str | None, exchange: str | None) -> bytes:
    """An inline-XBRL cover page naming one registered class."""
    facts = [f'<ix:nonNumeric contextRef="c1" name="dei:Security12bTitle">{title}</ix:nonNumeric>']
    if symbol is not None:
        facts.append(
            f'<ix:nonNumeric contextRef="c1" name="dei:TradingSymbol">{symbol}</ix:nonNumeric>'
        )
    if exchange is not None:
        facts.append(
            '<ix:nonNumeric contextRef="c1" name="dei:SecurityExchangeName">'
            f"{exchange}</ix:nonNumeric>"
        )
    return f"<html><body>{''.join(facts)}</body></html>".encode()


def catalogue(cik: int, filings: list[tuple[str, str, str, str]]) -> bytes:
    """A submissions catalogue in the SEC's column-wise shape."""
    return json.dumps(
        {
            "cik": f"{cik:010d}",
            "filings": {
                "recent": {
                    "accessionNumber": [f[0] for f in filings],
                    "form": [f[1] for f in filings],
                    "filingDate": [f[2] for f in filings],
                    "reportDate": ["" for _ in filings],
                    "primaryDocument": [f[3] for f in filings],
                    "acceptanceDateTime": [f"{f[2]}T16:05:00.000Z" for f in filings],
                },
                "files": [],
            },
        }
    ).encode()


def entry_for(key: str, payload: bytes, **fields: object) -> Entry:
    """A DOWNLOADED record vouching for `payload` under `key`, consistent with the key."""
    segments = key.split("/")
    cik = int(segments[1].removeprefix("CIK").removesuffix(".json"))
    if segments[0] == "submissions":
        url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
        kind = "SUBMISSIONS"
        accession = None
    else:
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{segments[2]}/{segments[3]}"
        kind = "FILING"
        # The key carries the accession without its dashes; the manifest records it with them.
        compact = segments[2]
        accession = f"{compact[:10]}-{compact[10:12]}-{compact[12:]}"
    base: dict[str, object] = {
        "url": url,
        "outcome": Outcome.DOWNLOADED,
        "status": 200,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "retrieved_at": "2026-09-18T00:00:00+00:00",
        "path": key,
        "request_kind": kind,
        "cik": cik,
        "accession": accession,
    }
    base.update(fields)
    return Entry(**base)  # type: ignore[arg-type]  # a test helper's loose kwargs


#: The corpus: three filers. 100 files two 10-Ks and an 8-K; 200 files one 8-K
#: and shares one accession with 100 (a co-registrant); 300's catalogue names
#: nothing that qualifies. One filing carries no cover facts at all.
ACC = {
    "a": "0000000100-24-000001",
    "b": "0000000100-24-000002",
    "c": "0000000100-24-000003",
    "shared": "0000000200-24-000001",
    "d": "0000000200-24-000002",
}
SHARED_BODY = cover("Common Stock, par value $0.01", "SHR", "New York Stock Exchange")
CORPUS: dict[str, tuple[bytes, dict[str, object]]] = {
    submissions_key(100): (
        catalogue(
            100,
            [
                (ACC["a"], "10-K", "2024-02-01", "a.htm"),
                (ACC["b"], "10-K", "2025-02-01", "b.htm"),
                (ACC["c"], "8-K", "2025-03-01", "c.htm"),
                (ACC["shared"], "8-K", "2025-04-01", "shared.htm"),
                ("0000000100-24-000009", "4", "2025-05-01", "form4.xml"),
            ],
        ),
        {},
    ),
    submissions_key(200): (
        catalogue(
            200,
            [
                (ACC["shared"], "8-K", "2025-04-01", "shared.htm"),
                (ACC["d"], "8-K", "2025-06-01", "d.htm"),
            ],
        ),
        {},
    ),
    submissions_key(300): (
        catalogue(300, [("0000000300-24-000001", "4", "2025-01-01", "f.xml")]),
        {},
    ),
    filing_key(100, ACC["a"], "a.htm"): (
        cover("Common Stock", "AAA", "The Nasdaq Stock Market LLC"),
        {"form": "10-K", "filing_date": "2024-02-01"},
    ),
    filing_key(100, ACC["b"], "b.htm"): (
        cover("Common Stock", "AAA", "The Nasdaq Stock Market LLC"),
        {"form": "10-K", "filing_date": "2025-02-01"},
    ),
    filing_key(100, ACC["c"], "c.htm"): (
        b"<html><body>An 8-K filed before inline XBRL cover facts.</body></html>",
        {"form": "8-K", "filing_date": "2025-03-01"},
    ),
    filing_key(100, ACC["shared"], "shared.htm"): (
        SHARED_BODY,
        {"form": "8-K", "filing_date": "2025-04-01"},
    ),
    filing_key(200, ACC["shared"], "shared.htm"): (
        SHARED_BODY,
        {"form": "8-K", "filing_date": "2025-04-01"},
    ),
    filing_key(200, ACC["d"], "d.htm"): (
        cover("7.5% Notes due 2030", None, "New York Stock Exchange"),
        {"form": "8-K", "filing_date": "2025-06-01"},
    ),
}
SUBMISSIONS = 3
FILINGS = 6


class CountingStorage:
    """A backend over the memory one that counts every operation by key.

    Two hooks. `tamper` serves different bytes for a key than the manifest
    vouches for; `fail_after` makes every read past the N-th raise
    `StorageUnavailableError` — the bucket going away mid-build.
    """

    def __init__(self, inner: MemoryEvidenceStorage) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.reads: dict[str, int] = {}
        self.other: list[tuple[str, str]] = []
        self.tamper: dict[str, bytes] = {}
        self.fail_after: int | None = None

    def exists(self, key: str) -> bool:
        self.other.append(("exists", key))
        return self._inner.exists(key)

    def stat(self, key: str) -> ArtifactStat:
        self.other.append(("stat", key))
        return self._inner.stat(key)

    def read_bytes(self, key: str) -> bytes:
        with self._lock:
            if self.fail_after is not None and sum(self.reads.values()) >= self.fail_after:
                raise StorageUnavailableError("the bucket went away")
            self.reads[key] = self.reads.get(key, 0) + 1
        if key in self.tamper:
            return self.tamper[key]
        return self._inner.read_bytes(key)

    def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
        return self._inner.write_bytes(key, data)

    def digest(self, key: str) -> str:
        self.other.append(("digest", key))
        return self._inner.digest(key)

    def list_prefix(self, prefix: str) -> Iterator[str]:
        self.other.append(("list_prefix", prefix))
        return self._inner.list_prefix(prefix)


@pytest.fixture
def storage(memory_storage: type[MemoryEvidenceStorage]) -> CountingStorage:
    inner = memory_storage()
    for key, (payload, _) in CORPUS.items():
        inner.write_bytes(key, payload)
    return CountingStorage(inner)


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    (tmp_path / "manifests").mkdir()
    log = Manifest(tmp_path / "manifests" / "evidence.jsonl")
    for key, (payload, fields) in CORPUS.items():
        log.append(entry_for(key, payload, **fields))
    return log


@pytest.fixture
def derived(tmp_path: Path) -> Path:
    path = tmp_path / "derived"
    path.mkdir()
    return path


def ledger(derived: Path, name: str = INDEX_FILE) -> dict[str, tuple[str, str | None]]:
    with closing(sqlite3.connect(derived / name)) as db:
        return {
            str(path): (str(status), detail)
            for path, status, detail in db.execute("SELECT path, status, detail FROM build_ledger")
        }


class TestOneReadIsTheVerificationAndTheParse:
    def test_every_artifact_is_read_exactly_once_and_never_digested(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        report = build_derived_index(storage, manifest, derived)

        assert report.complete
        assert storage.reads == dict.fromkeys(CORPUS, 1)
        assert storage.other == []
        assert report.body_reads == SUBMISSIONS + FILINGS
        assert report.body_bytes == sum(len(payload) for payload, _ in CORPUS.values())

    def test_the_rows_carry_the_existing_extraction_semantics(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        report = build_derived_index(storage, manifest, derived)

        assert report.submissions_parsed == SUBMISSIONS
        assert report.submissions_empty == 1
        assert report.filings_parsed == FILINGS
        assert report.forms == {"10-K": 2, "8-K": 4}
        # Five qualifying catalogue rows: four from 100 (the Form 4 is dropped),
        # two from 200 — the shared accession is one row per CIK.
        assert report.catalogue_rows == 6
        with closing(sqlite3.connect(derived / INDEX_FILE)) as db:
            symbols = dict(
                db.execute("SELECT accession, normalized_symbol FROM securities WHERE cik = 100")
            )
            evidence = dict(db.execute("SELECT accession, evidence_date FROM filings"))
            sources = {row[0] for row in db.execute("SELECT DISTINCT date_source FROM filings")}
            no_cover = db.execute(
                "SELECT cover_securities, parsed FROM filings WHERE accession = ?", (ACC["c"],)
            ).fetchone()
        assert symbols[ACC["a"]] == "AAA"
        assert evidence[ACC["a"]] == "2024-02-01"
        assert sources == {"ACCEPTED"}
        assert no_cover == (0, 1)

    def test_co_registrant_rows_are_kept_apart_by_cik_and_accession(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        report = build_derived_index(storage, manifest, derived)

        with closing(sqlite3.connect(derived / INDEX_FILE)) as db:
            rows = db.execute(
                "SELECT cik, sha256 FROM filings WHERE accession = ? ORDER BY cik", (ACC["shared"],)
            ).fetchall()
        assert [cik for cik, _ in rows] == [100, 200]
        assert len({sha for _, sha in rows}) == 1
        assert report.diagnostics["co_registrant_accessions"] == 1
        assert report.diagnostics["co_registrant_surplus_rows"] == 1
        assert report.diagnostics["duplicate_filing_identities"] == 0

    def test_missing_cover_fields_stay_missing(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        report = build_derived_index(storage, manifest, derived)

        with closing(sqlite3.connect(derived / INDEX_FILE)) as db:
            row = db.execute(
                "SELECT raw_symbol, normalized_symbol, no_trading_symbol, raw_exchange"
                " FROM securities WHERE accession = ?",
                (ACC["d"],),
            ).fetchone()
        assert row == (None, None, 0, "New York Stock Exchange")
        assert report.diagnostics["trading_symbol_rows"] == 4
        assert report.diagnostics["security12b_title_rows"] == 5
        assert report.diagnostics["security_exchange_name_rows"] == 5

    def test_the_input_order_is_catalogues_then_filings_each_by_key(
        self, manifest: Manifest
    ) -> None:
        indexed, other = planned_inputs(manifest)

        kinds = [entry.request_kind for entry in indexed]
        assert kinds == ["SUBMISSIONS"] * SUBMISSIONS + ["FILING"] * FILINGS
        paths = [entry.path or "" for entry in indexed]
        assert paths[:SUBMISSIONS] == sorted(paths[:SUBMISSIONS])
        assert paths[SUBMISSIONS:] == sorted(paths[SUBMISSIONS:])
        assert other == []


class TestBytesThatDoNotReproduceTheManifestStopTheBuild:
    @pytest.fixture
    def target(self) -> str:
        return filing_key(100, ACC["b"], "b.htm")

    def test_a_digest_mismatch_records_nothing_for_the_artifact(
        self, storage: CountingStorage, manifest: Manifest, derived: Path, target: str
    ) -> None:
        payload, _ = CORPUS[target]
        storage.tamper[target] = payload[:-1] + b"?"  # same length, other bytes

        report = build_derived_index(storage, manifest, derived)

        assert not report.complete
        assert report.sha_mismatches == 1 and report.byte_mismatches == 0
        assert "hashes to" in str(report.failure)
        assert target not in ledger(derived, INDEX_BUILDING_FILE)
        assert storage.reads[target] == 1
        assert not (derived / INDEX_COMPLETE_MARKER).exists()
        assert not (derived / INDEX_FILE).exists()
        with closing(sqlite3.connect(derived / INDEX_BUILDING_FILE)) as db:
            rows = db.execute("SELECT COUNT(*) FROM filings WHERE path = ?", (target,)).fetchone()
        assert rows == (0,)

    def test_a_size_mismatch_records_nothing_for_the_artifact(
        self, storage: CountingStorage, manifest: Manifest, derived: Path, target: str
    ) -> None:
        payload, _ = CORPUS[target]
        storage.tamper[target] = payload + b"\n"

        report = build_derived_index(storage, manifest, derived)

        assert not report.complete
        assert report.byte_mismatches == 1 and report.sha_mismatches == 0
        assert "bytes" in str(report.failure)
        assert target not in ledger(derived, INDEX_BUILDING_FILE)
        assert not (derived / INDEX_COMPLETE_MARKER).exists()

    def test_a_missing_object_stops_the_build(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        gone = "filings/0000000100/0000000100-24-000099/gone.htm"
        manifest.append(entry_for(gone, b"never stored", form="8-K", filing_date="2025-01-01"))

        report = build_derived_index(storage, manifest, derived)

        assert not report.complete
        assert report.missing_objects == 1
        assert "absent" in str(report.failure)
        assert gone not in ledger(derived, INDEX_BUILDING_FILE)

    def test_the_artifacts_before_the_bad_one_stay_committed_and_the_build_resumes(
        self, storage: CountingStorage, manifest: Manifest, derived: Path, target: str
    ) -> None:
        payload, _ = CORPUS[target]
        storage.tamper[target] = b"x" * len(payload)
        first = build_derived_index(storage, manifest, derived)
        del storage.tamper[target]

        second = build_derived_index(storage, manifest, derived)

        assert not first.complete and second.complete
        assert second.skipped_resume == first.processed
        assert storage.reads == dict.fromkeys(CORPUS, 1) | {target: 2}


class TestResume:
    def test_an_interrupted_build_resumes_without_re_reading_what_it_committed(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        partial = build_derived_index(storage, manifest, derived, batch_size=2, stop_after=5)

        assert partial.stopped_early and not partial.complete
        assert partial.processed == 5
        assert set(ledger(derived, INDEX_BUILDING_FILE)) == set(storage.reads)
        assert not (derived / INDEX_FILE).exists()

        resumed = build_derived_index(storage, manifest, derived, batch_size=2)

        assert resumed.complete
        assert resumed.skipped_resume == 5
        assert resumed.processed == SUBMISSIONS + FILINGS - 5
        assert storage.reads == dict.fromkeys(CORPUS, 1)

    def test_a_resumed_build_fingerprints_identically_to_an_uninterrupted_one(
        self,
        memory_storage: type[MemoryEvidenceStorage],
        storage: CountingStorage,
        manifest: Manifest,
        derived: Path,
        tmp_path: Path,
    ) -> None:
        other = tmp_path / "uninterrupted"
        other.mkdir()
        straight = build_derived_index(storage, manifest, other)

        build_derived_index(storage, manifest, derived, batch_size=1, stop_after=4)
        resumed = build_derived_index(storage, manifest, derived, batch_size=3)

        assert straight.complete and resumed.complete
        assert resumed.fingerprints == straight.fingerprints
        assert resumed.table_counts == straight.table_counts
        assert resumed.ledger == straight.ledger

    def test_a_resume_against_a_different_manifest_is_refused(
        self, storage: CountingStorage, manifest: Manifest, derived: Path, tmp_path: Path
    ) -> None:
        build_derived_index(storage, manifest, derived, stop_after=2)
        # The same corpus, one more record: a different manifest, so a different SHA.
        extra = "filings/0000000300/0000000300-24-000007/g.htm"
        storage.write_bytes(extra, cover("Common", "GGG", "NYSE"))
        manifest.append(
            entry_for(extra, cover("Common", "GGG", "NYSE"), form="8-K", filing_date="2025-07-01")
        )

        with pytest.raises(ResumeRefusedError, match="manifest_sha256 was"):
            build_derived_index(storage, manifest, derived)

        assert sum(storage.reads.values()) == 2
        assert not (derived / INDEX_FILE).exists()

    def test_a_resume_whose_ledger_names_other_bytes_is_refused(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        build_derived_index(storage, manifest, derived, stop_after=2)
        with closing(sqlite3.connect(derived / INDEX_BUILDING_FILE)) as db:
            db.execute("UPDATE build_ledger SET expected_sha256 = ?", ("0" * 64,))
            db.commit()
        # Keep the manifest digest binding intact so the ledger check is what fires.

        with pytest.raises(ResumeRefusedError, match="the ledger recorded"):
            build_derived_index(storage, manifest, derived)

    def test_fresh_discards_the_in_progress_build(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        build_derived_index(storage, manifest, derived, stop_after=3)

        report = build_derived_index(storage, manifest, derived, fresh=True)

        assert report.complete
        assert report.skipped_resume == 0
        assert report.processed == SUBMISSIONS + FILINGS

    def test_a_completed_build_is_not_repeated_without_fresh(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        build_derived_index(storage, manifest, derived)
        reads = dict(storage.reads)

        with pytest.raises(IndexBuildError, match="already complete"):
            build_derived_index(storage, manifest, derived)

        assert storage.reads == reads


class TestPrefetchChangesOnlyTheWaiting:
    def test_a_prefetched_build_reads_each_artifact_once_and_fingerprints_identically(
        self, storage: CountingStorage, manifest: Manifest, tmp_path: Path
    ) -> None:
        sequential, pooled = tmp_path / "one", tmp_path / "four"
        sequential.mkdir()
        pooled.mkdir()

        first = build_derived_index(storage, manifest, sequential, prefetch=1)
        second = build_derived_index(storage, manifest, pooled, prefetch=4, batch_size=2)

        assert first.complete and second.complete
        assert second.fingerprints == first.fingerprints
        assert storage.reads == dict.fromkeys(CORPUS, 2)
        assert second.reads_discarded == 0

    def test_a_stop_after_under_prefetch_downloads_nothing_beyond_the_stop(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        partial = build_derived_index(storage, manifest, derived, prefetch=4, stop_after=3)

        assert partial.stopped_early and partial.processed == 3
        assert sum(storage.reads.values()) == 3
        assert partial.reads_discarded == 0

    def test_a_failure_under_prefetch_discards_at_most_the_bodies_in_flight(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        target = filing_key(100, ACC["a"], "a.htm")
        payload, _ = CORPUS[target]
        storage.tamper[target] = payload + b"!"

        stopped = build_derived_index(storage, manifest, derived, prefetch=3)
        assert target not in ledger(derived, INDEX_BUILDING_FILE)
        del storage.tamper[target]
        resumed = build_derived_index(storage, manifest, derived, prefetch=3)

        assert not stopped.complete and stopped.byte_mismatches == 1
        assert stopped.reads_discarded <= 3  # never more than the depth in flight
        assert resumed.complete
        assert ledger(derived)[target] == (ArtifactOutcome.COMMITTED, None)
        assert all(count <= 2 for count in storage.reads.values())
        assert sum(count - 1 for count in storage.reads.values()) == 1 + stopped.reads_discarded


class TestAStorageFaultLeavesAResumableBuild:
    def test_the_build_stops_with_no_completion_and_resumes_when_storage_returns(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        storage.fail_after = 4

        stopped = build_derived_index(storage, manifest, derived, batch_size=3)

        assert not stopped.complete and stopped.failure is not None
        assert "StorageUnavailableError" in stopped.failure
        assert stopped.storage_failures == 1
        assert stopped.processed == 4
        # Every processed artifact is durable, including the one past the last full batch.
        assert len(ledger(derived, INDEX_BUILDING_FILE)) == 4
        assert not (derived / INDEX_COMPLETE_MARKER).exists()
        assert not (derived / INDEX_FILE).exists()

        storage.fail_after = None
        resumed = build_derived_index(storage, manifest, derived, batch_size=3)

        assert resumed.complete
        assert resumed.skipped_resume == 4
        assert storage.reads == dict.fromkeys(CORPUS, 1)

    def test_the_manifest_is_never_written(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        before = manifest.path.read_bytes()
        storage.fail_after = 2

        build_derived_index(storage, manifest, derived)

        assert manifest.path.read_bytes() == before


class TestAParserFailureIsLedgeredAndBlocksCompletion:
    def test_a_catalogue_that_is_not_json_is_failed_not_empty(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        bad = submissions_key(400)
        storage.write_bytes(bad, b"{not json")
        manifest.append(entry_for(bad, b"{not json"))

        report = build_derived_index(storage, manifest, derived)

        assert not report.complete
        assert report.submissions_failed == 1 and report.submissions_empty == 1
        assert report.parse_failures == 1
        assert "ledgered FAILED" in str(report.failure)
        assert ledger(derived, INDEX_BUILDING_FILE)[bad][0] == ArtifactOutcome.FAILED
        assert ledger(derived, INDEX_BUILDING_FILE)[bad][1] is not None
        assert "INVALID_JSON" in str(ledger(derived, INDEX_BUILDING_FILE)[bad][1])
        assert not (derived / INDEX_COMPLETE_MARKER).exists()
        assert not (derived / INDEX_FILE).exists()

    @pytest.mark.parametrize(
        ("payload", "reason"),
        [(b"[]", "NOT_AN_OBJECT"), (b'{"cik": "400", "filings": []}', "MALFORMED_CATALOGUE")],
    )
    def test_a_catalogue_of_the_wrong_shape_is_classified(
        self,
        storage: CountingStorage,
        manifest: Manifest,
        derived: Path,
        payload: bytes,
        reason: str,
    ) -> None:
        bad = submissions_key(400)
        storage.write_bytes(bad, payload)
        manifest.append(entry_for(bad, payload))

        report = build_derived_index(storage, manifest, derived)

        assert report.parse_failures == 1
        assert reason in str(ledger(derived, INDEX_BUILDING_FILE)[bad][1])

    def test_a_filing_the_parser_raises_on_is_failed_and_recorded(
        self,
        storage: CountingStorage,
        manifest: Manifest,
        derived: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from api_clients import parse_cover_page
        from stock_screener.sec_evidence import extract

        poison = filing_key(100, ACC["c"], "c.htm")
        real = parse_cover_page

        def fragile(document: str) -> object:
            if "before inline XBRL" in document:
                raise ValueError("unbalanced cover page")
            return real(document)

        monkeypatch.setattr(extract, "parse_cover_page", fragile)

        report = build_derived_index(storage, manifest, derived)

        assert not report.complete
        assert report.filings_failed == 1 and report.filings_parsed == FILINGS - 1
        status, detail = ledger(derived, INDEX_BUILDING_FILE)[poison]
        assert status == ArtifactOutcome.FAILED
        assert "unbalanced cover page" in str(detail)
        with closing(sqlite3.connect(derived / INDEX_BUILDING_FILE)) as db:
            row = db.execute(
                "SELECT parsed, parse_error FROM filings WHERE path = ?", (poison,)
            ).fetchone()
        assert row == (0, "ValueError: unbalanced cover page")
        assert not (derived / INDEX_COMPLETE_MARKER).exists()


class TestDeterminism:
    def test_two_builds_of_one_corpus_fingerprint_identically(
        self, storage: CountingStorage, manifest: Manifest, tmp_path: Path
    ) -> None:
        first_dir, second_dir = tmp_path / "one", tmp_path / "two"
        first_dir.mkdir()
        second_dir.mkdir()

        first = build_derived_index(storage, manifest, first_dir)
        second = build_derived_index(storage, manifest, second_dir)

        assert first.complete and second.complete
        assert first.fingerprints == second.fingerprints
        assert first.table_counts == second.table_counts
        assert {name: print_.rows for name, print_ in first.fingerprints.items()} == {
            "catalogue": 6,
            "catalogue_stats": 3,
            "filings": FILINGS,
            "securities": 5,
            "cover": 5,
        }

    def test_the_marker_and_the_fingerprint_file_agree_with_the_report(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        report = build_derived_index(storage, manifest, derived, facts={"repo_commit": "abc"})

        marker = index_complete_facts(derived)
        prints = json.loads((derived / INDEX_FINGERPRINTS_FILE).read_text(encoding="utf-8"))
        meta = json.loads((derived / INDEX_META_FILE).read_text(encoding="utf-8"))
        assert marker is not None
        assert marker["stage"] == "DERIVED_INDEX_COMPLETE"
        assert marker["manifest_sha256"] == report.manifest_sha256
        assert marker["parsed"] == SUBMISSIONS + FILINGS
        assert marker["parse_failures"] == 0
        assert marker["index_sha256"] == report.index_sha256
        assert marker["fingerprints"] == prints["fingerprints"]
        assert prints["fingerprints"]["securities"]["sha256"] == (
            report.fingerprints["securities"].sha256
        )
        assert meta["repo_commit"] == "abc"
        assert meta["index_sha256"] == report.index_sha256
        assert meta["ledger"] == {"FILING:COMMITTED": FILINGS, "SUBMISSIONS:COMMITTED": SUBMISSIONS}
        assert "build_started_at" in meta and "build_completed_at" in meta
        assert report.integrity_check == "ok"
        assert (
            report.index_sha256 == hashlib.sha256((derived / INDEX_FILE).read_bytes()).hexdigest()
        )
        assert not (derived / INDEX_BUILDING_FILE).exists()

    def test_a_trusted_kind_the_index_does_not_consume_is_accounted_not_read(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        text = "fulltext/0000000100/0000000100-24-000001/a.txt"
        storage.write_bytes(text, b"plain text")
        manifest.append(entry_for(text, b"plain text", request_kind="FULLTEXT", accession=ACC["a"]))

        report = build_derived_index(storage, manifest, derived)

        assert report.complete
        assert report.not_indexed == 1
        assert text not in storage.reads
        assert ledger(derived)[text] == (ArtifactOutcome.NOT_INDEXED, "kind=FULLTEXT")


class TestTheExistingConsumersOpenIt:
    def test_coverage_and_targets_read_the_v2_index_unchanged(
        self, storage: CountingStorage, manifest: Manifest, derived: Path
    ) -> None:
        build_derived_index(storage, manifest, derived)

        with closing(sqlite3.connect(derived / INDEX_FILE)) as db:
            spans = span_statistics(db)
            forms = {value.form: value.documents for value in form_value(db)}
            breakdown = class_breakdown(db)
            opportunity = catalogue_opportunity(db)
            held = held_keys(db)

        assert {span.cik for span in spans} == {100, 200}
        assert forms == {"10-K": 2, "8-K": 4}
        assert sum(breakdown.by_class.values()) == 5
        assert opportunity.qualifying == 6 and opportunity.downloaded == 6
        assert (100, ACC["a"]) in held and (200, ACC["shared"]) in held
        assert len(held) == FILINGS


B2_CONFIG = B2Configuration(
    endpoint_url="https://s3.us-west-004.backblazeb2.com",
    bucket="derived-test-bucket",
    key_id=SecretStr("derived-key-id"),
    application_key=SecretStr("derived-application-key"),
)
GENERATION = "SEC_EVIDENCE_GENERATION_V2"


class TestTheMirror:
    def test_the_metadata_is_mirrored_as_snapshots_and_the_index_is_not(
        self,
        storage: CountingStorage,
        manifest: Manifest,
        derived: Path,
        fake_s3: type[FakeS3Client],
    ) -> None:
        build_derived_index(storage, manifest, derived)
        client = fake_s3()
        bucket = B2EvidenceStorage(B2_CONFIG, client=client)
        mirror = ControlMirror(bucket, GENERATION)

        records = mirror_index_metadata(mirror, derived)

        assert [record.outcome for record in records] == ["MIRRORED"] * 3
        keys = set(client.objects)
        assert all(key.startswith(f"control/{GENERATION}/derived/") for key in keys)
        assert not any(key.split("/")[0] in ARTIFACT_PREFIXES for key in keys)
        assert not any(key.endswith(".db") for key in keys)
        for record in records:
            local = derived / record.local_path.rsplit("/", 1)[-1]
            assert hashlib.sha256(client.objects[record.key][0]).hexdigest() == record.sha256
            assert record.sha256 == hashlib.sha256(local.read_bytes()).hexdigest()
        newest = mirror.newest_snapshot("derived/index-complete")
        assert newest is not None
        assert json.loads(newest[1])["stage"] == "DERIVED_INDEX_COMPLETE"

    def test_mirroring_twice_is_a_hit_never_a_second_put(
        self,
        storage: CountingStorage,
        manifest: Manifest,
        derived: Path,
        fake_s3: type[FakeS3Client],
    ) -> None:
        build_derived_index(storage, manifest, derived)
        client = fake_s3()
        mirror = ControlMirror(B2EvidenceStorage(B2_CONFIG, client=client), GENERATION)
        mirror_index_metadata(mirror, derived)
        puts = sum(1 for op, _ in client.calls if op == "put_object")

        records = mirror_index_metadata(mirror, derived)

        assert [record.outcome for record in records] == ["MIRROR_HIT"] * 3
        # Three new descriptors, no re-upload of the files themselves.
        assert sum(1 for op, _ in client.calls if op == "put_object") == puts + 3


class TestTheCommand:
    @pytest.fixture
    def environment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
        control = tmp_path / "control"
        (control / "manifests").mkdir(parents=True)
        (control / "plans" / "generation_v2").mkdir(parents=True)
        monkeypatch.setenv("ENVIRONMENT", "test")
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control))
        monkeypatch.setenv("B2_ENDPOINT_URL", B2_CONFIG.endpoint_url)
        monkeypatch.setenv("B2_BUCKET", B2_CONFIG.bucket)
        monkeypatch.setenv("B2_KEY_ID", "derived-key-id")
        monkeypatch.setenv("B2_APPLICATION_KEY", "derived-application-key")
        get_settings.cache_clear()
        yield control
        get_settings.cache_clear()

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
    def seeded(self, environment: Path, wired: FakeS3Client) -> tuple[FakeS3Client, str]:
        """The corpus in the bucket, the manifest on the control root, the marker mirrored."""
        for key, (payload, _) in CORPUS.items():
            wired.objects[key] = (payload, {"sha256": hashlib.sha256(payload).hexdigest()})
        log = Manifest(environment / "manifests" / "evidence.jsonl")
        for key, (payload, fields) in CORPUS.items():
            log.append(entry_for(key, payload, **fields))
        manifest_sha = hashlib.sha256(log.path.read_bytes()).hexdigest()
        marker = json.dumps(
            {
                "generation_id": GENERATION,
                "stage": "BASELINE_COMPLETE",
                "manifest_sha256": manifest_sha,
                "trusted": len(CORPUS),
                "universe_sha256": "b8" * 32,
                "resolution_sha256": "16" * 32,
            },
            indent=2,
            sort_keys=True,
        ).encode()
        (environment / "plans" / "generation_v2" / "baseline_complete.json").write_bytes(marker)
        wired.objects[f"control/{GENERATION}/baseline_complete.json"] = (marker, {})
        return wired, manifest_sha

    def test_the_command_builds_verifies_and_mirrors_without_touching_evidence(
        self, environment: Path, seeded: tuple[FakeS3Client, str]
    ) -> None:
        client, manifest_sha = seeded

        result = CliRunner().invoke(
            app,
            [
                "evidence",
                "build-index",
                "--expect-manifest-sha",
                manifest_sha,
                "--expect-trusted",
                str(len(CORPUS)),
            ],
        )

        assert result.exit_code == 0, result.stdout
        assert "DERIVED INDEX COMPLETE" in result.stdout
        assert "baseline mirror  : verified" in result.stdout
        gets = [key for op, key in client.calls if op == "get_object"]
        assert sorted(k for k in gets if k.split("/")[0] in ARTIFACT_PREFIXES) == sorted(CORPUS)
        assert not any(
            key.split("/")[0] in ARTIFACT_PREFIXES for op, key in client.calls if op == "put_object"
        )
        assert not any(op.startswith("delete") for op, _ in client.calls)
        derived = environment / "derived"
        assert (derived / INDEX_FILE).is_file()
        assert index_complete_facts(derived) is not None
        assert any(
            k.startswith(f"control/{GENERATION}/derived/index-complete/") for k in client.objects
        )
        assert not any(k.endswith(".db") for k in client.objects)
        assert "derived-application-key" not in result.stdout

    def test_the_command_refuses_a_manifest_the_baseline_does_not_name(
        self, environment: Path, seeded: tuple[FakeS3Client, str]
    ) -> None:
        client, _ = seeded
        marker = environment / "plans" / "generation_v2" / "baseline_complete.json"
        facts = json.loads(marker.read_text(encoding="utf-8"))
        facts["manifest_sha256"] = "00" * 32
        marker.write_text(json.dumps(facts), encoding="utf-8")

        result = CliRunner().invoke(app, ["evidence", "build-index"])

        assert result.exit_code == 1
        assert "BASELINE_COMPLETE is absent or names a different manifest" in result.stdout
        assert not any(op == "get_object" for op, _ in client.calls)

    def test_the_command_refuses_when_the_mirrored_marker_differs(
        self, environment: Path, seeded: tuple[FakeS3Client, str]
    ) -> None:
        client, _ = seeded
        client.objects[f"control/{GENERATION}/baseline_complete.json"] = (b"{}", {})

        result = CliRunner().invoke(app, ["evidence", "build-index"])

        assert result.exit_code == 1
        assert "mirrored BASELINE_COMPLETE marker differs" in result.stdout
        assert not any(
            key.split("/")[0] in ARTIFACT_PREFIXES for op, key in client.calls if op == "get_object"
        )

    def test_stop_after_leaves_a_resumable_build_the_status_reports(
        self, environment: Path, seeded: tuple[FakeS3Client, str]
    ) -> None:
        first = CliRunner().invoke(app, ["evidence", "build-index", "--stop-after", "3"])
        status = CliRunner().invoke(app, ["evidence", "status"])

        assert first.exit_code == 1
        assert "STOPPED EARLY" in first.stdout
        assert "derived index    : in progress (resumable)" in status.stdout

        second = CliRunner().invoke(app, ["evidence", "build-index"])
        after = CliRunner().invoke(app, ["evidence", "status"])

        assert second.exit_code == 0, second.stdout
        assert "skipped by resume 3" in second.stdout
        assert "derived index    : DERIVED_INDEX_COMPLETE" in after.stdout
