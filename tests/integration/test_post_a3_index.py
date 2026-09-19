"""The post-A3 index: a verified baseline copy plus exactly the delta, never the corpus again.

A baseline index is built the B2.12 way over a small corpus; the manifest
then gains the A3 filings — one of them a co-registrant pair — and the
authorized plan names them. What must hold: the baseline is re-proven
before it is copied and refused when it is not itself; the delta is the
plan, no more and no less; only the delta's keys are read from the bucket;
bytes that do not reproduce the manifest yield no rows; a resume skips what
it committed and binds to what it started from; the baseline's rows
fingerprint identically before and after; the baseline index file is
byte-unchanged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from stock_screener.sec_evidence import (
    BaselineIndexFacts,
    DeltaSet,
    Entry,
    Manifest,
    Outcome,
    PostA3Bindings,
    PostA3Error,
    PostA3Report,
    baseline_paths,
    build_derived_index,
    build_post_a3_index,
    delta_entries,
    verify_baseline_index,
)
from stock_screener.sec_evidence.derived import INDEX_FILE, INDEX_META_FILE
from stock_screener.sec_evidence.post_a3 import (
    POST_A3_BUILDING_FILE,
    POST_A3_COMPLETE_MARKER,
    POST_A3_INDEX_FILE,
    POST_A3_META_FILE,
)
from stock_screener.sec_evidence.storage import filing_key, submissions_key

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from conftest import MemoryEvidenceStorage
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
    """A DOWNLOADED record vouching for `payload` under `key`."""
    segments = key.split("/")
    cik = int(segments[1].removeprefix("CIK").removesuffix(".json"))
    if segments[0] == "submissions":
        url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
        kind, accession = "SUBMISSIONS", None
    else:
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{segments[2]}/{segments[3]}"
        compact = segments[2]
        kind, accession = "FILING", f"{compact[:10]}-{compact[10:12]}-{compact[12:]}"
    base: dict[str, object] = {
        "url": url,
        "outcome": Outcome.DOWNLOADED,
        "status": 200,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "retrieved_at": "2026-09-19T00:00:00+00:00",
        "path": key,
        "request_kind": kind,
        "cik": cik,
        "accession": accession,
    }
    base.update(fields)
    return Entry(**base)  # type: ignore[arg-type]


ACC = {
    "a": "0000000100-24-000001",
    "b": "0000000100-24-000002",
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
                (ACC["shared"], "8-K", "2025-04-01", "shared.htm"),
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

#: The baseline: the three catalogues and filing `a`. The A3 delta: `b`, the
#: co-registrant pair under `shared`, and `d`.
BASELINE_KEYS = (
    submissions_key(100),
    submissions_key(200),
    submissions_key(300),
    filing_key(100, ACC["a"], "a.htm"),
)
DELTA_KEYS = (
    filing_key(100, ACC["b"], "b.htm"),
    filing_key(100, ACC["shared"], "shared.htm"),
    filing_key(200, ACC["shared"], "shared.htm"),
    filing_key(200, ACC["d"], "d.htm"),
)
PLAN_KEYS = [(100, ACC["b"]), (100, ACC["shared"]), (200, ACC["shared"]), (200, ACC["d"])]


class ReadCounting:
    """Counts every read by key and can serve other bytes for one key."""

    def __init__(self, inner: MemoryEvidenceStorage) -> None:
        self._inner = inner
        self.reads: dict[str, int] = {}
        self.tamper: dict[str, bytes] = {}

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def stat(self, key: str) -> ArtifactStat:
        return self._inner.stat(key)

    def read_bytes(self, key: str) -> bytes:
        self.reads[key] = self.reads.get(key, 0) + 1
        return self.tamper.get(key) or self._inner.read_bytes(key)

    def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
        return self._inner.write_bytes(key, data)

    def digest(self, key: str) -> str:
        return self._inner.digest(key)

    def list_prefix(self, prefix: str) -> Iterator[str]:
        return iter(list(self._inner.list_prefix(prefix)))


class World:
    """A baseline index built the B2.12 way, then a final manifest with the A3 delta."""

    def __init__(self, tmp_path: Path, memory_storage: type[MemoryEvidenceStorage]) -> None:
        self.storage = ReadCounting(memory_storage())
        for key, (payload, _) in CORPUS.items():
            self.storage.write_bytes(key, payload)
        (tmp_path / "manifests").mkdir(parents=True)
        self.manifest = Manifest(tmp_path / "manifests" / "evidence.jsonl")
        for key in BASELINE_KEYS:
            payload, fields = CORPUS[key]
            self.manifest.append(entry_for(key, payload, **fields))
        self.derived = tmp_path / "derived"
        self.derived.mkdir()
        report = build_derived_index(self.storage, self.manifest, self.derived)
        assert report.complete, report.failure
        self.baseline_sha = sha256_of(self.derived / INDEX_FILE)
        self.baseline_manifest_sha = sha256_of(self.manifest.path)
        self.storage.reads.clear()
        for key in DELTA_KEYS:
            payload, fields = CORPUS[key]
            self.manifest.append(entry_for(key, payload, **fields))
        self.final_manifest_sha = sha256_of(self.manifest.path)

    def baseline(self) -> BaselineIndexFacts:
        return verify_baseline_index(
            self.derived,
            expect_sha256=self.baseline_sha,
            expect_manifest_sha256=self.baseline_manifest_sha,
            expect_trusted=4,
            expect_filings=1,
            expect_submissions=3,
        )

    def delta(self, plan_keys: list[tuple[int, str]] | None = None) -> DeltaSet:
        return delta_entries(
            self.manifest, baseline_paths(self.derived / INDEX_FILE), plan_keys or PLAN_KEYS
        )

    def bindings(self, **overrides: object) -> PostA3Bindings:
        base: dict[str, object] = {
            "generation_id": "SEC_EVIDENCE_GENERATION_V2",
            "final_manifest_sha256": self.final_manifest_sha,
            "final_trusted": 8,
            "final_filings": 5,
            "final_submissions": 3,
            "baseline_index_sha256": self.baseline_sha,
            "baseline_manifest_sha256": self.baseline_manifest_sha,
            "plan_sha256": "0e" * 32,
            "a3_marker_sha256": "af" * 32,
            "delta_count": 4,
        }
        base.update(overrides)
        return PostA3Bindings(**base)  # type: ignore[arg-type]  # a test helper's loose kwargs

    def build(
        self, *, bindings: PostA3Bindings | None = None, stop_after: int | None = None
    ) -> PostA3Report:
        return build_post_a3_index(
            self.storage,
            self.delta(),
            self.derived,
            baseline=self.baseline(),
            bindings=bindings or self.bindings(),
            stop_after=stop_after,
        )


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def world(tmp_path: Path, memory_storage: type[MemoryEvidenceStorage]) -> World:
    return World(tmp_path, memory_storage)


class TestTheBaselineIsReProven:
    def test_the_built_baseline_passes_with_its_fingerprints(self, world: World) -> None:
        facts = world.baseline()

        assert facts.sha256 == world.baseline_sha and facts.integrity_check == "ok"
        assert facts.table_counts["filings"] == 1 and facts.table_counts["catalogue_stats"] == 3
        assert set(facts.fingerprints) == {
            "catalogue",
            "catalogue_stats",
            "filings",
            "securities",
            "cover",
        }

    def test_a_wrong_digest_is_a_conflict(self, world: World) -> None:
        with pytest.raises(PostA3Error, match="hashes to"):
            verify_baseline_index(
                world.derived,
                expect_sha256="00" * 32,
                expect_manifest_sha256=world.baseline_manifest_sha,
                expect_trusted=4,
                expect_filings=1,
                expect_submissions=3,
            )

    def test_metadata_naming_another_manifest_is_a_conflict(self, world: World) -> None:
        with pytest.raises(PostA3Error, match="does not bind"):
            verify_baseline_index(
                world.derived,
                expect_sha256=world.baseline_sha,
                expect_manifest_sha256=world.final_manifest_sha,
                expect_trusted=4,
                expect_filings=1,
                expect_submissions=3,
            )

    def test_an_index_whose_rows_moved_is_a_conflict(self, world: World) -> None:
        with closing(sqlite3.connect(world.derived / INDEX_FILE)) as db:
            db.execute("UPDATE filings SET form = 'X'")
            db.commit()
        meta = json.loads((world.derived / INDEX_META_FILE).read_text())
        meta["index_sha256"] = sha256_of(world.derived / INDEX_FILE)
        (world.derived / INDEX_META_FILE).write_text(json.dumps(meta))

        with pytest.raises(PostA3Error, match=r"does not bind|fingerprints moved"):
            verify_baseline_index(
                world.derived,
                expect_sha256=sha256_of(world.derived / INDEX_FILE),
                expect_manifest_sha256=world.baseline_manifest_sha,
                expect_trusted=4,
                expect_filings=1,
                expect_submissions=3,
            )


class TestTheDeltaIsThePlan:
    def test_the_manifest_beyond_the_baseline_is_exactly_the_plan(self, world: World) -> None:
        delta = world.delta()

        assert delta.exact and len(delta.entries) == 4
        assert delta.final_filings - delta.baseline_filings == 4
        assert delta.co_registrant_rows == 1
        assert [e.path for e in delta.entries] == sorted(DELTA_KEYS)

    def test_a_plan_target_the_manifest_lacks_is_missing(self, world: World) -> None:
        delta = world.delta([*PLAN_KEYS, (300, "0000000300-24-000009")])

        assert delta.missing == ((300, "0000000300-24-000009"),) and not delta.exact

    def test_a_manifest_filing_the_plan_lacks_is_an_extra(self, world: World) -> None:
        delta = world.delta(PLAN_KEYS[:3])

        assert delta.extras == ((200, ACC["d"]),) and not delta.exact
        with pytest.raises(PostA3Error, match="not the authorized plan"):
            build_post_a3_index(
                world.storage,
                delta,
                world.derived,
                baseline=world.baseline(),
                bindings=world.bindings(),
            )


class TestTheIncrementalBuild:
    def test_only_the_delta_is_read_and_the_baseline_file_is_untouched(self, world: World) -> None:
        report = world.build()

        assert report.complete, report.failure
        assert report.baseline_copied and report.copy_verified
        assert set(world.storage.reads) == set(DELTA_KEYS)
        assert all(n == 1 for n in world.storage.reads.values())
        assert sha256_of(world.derived / INDEX_FILE) == world.baseline_sha
        assert (world.derived / POST_A3_INDEX_FILE).is_file()
        assert not (world.derived / POST_A3_BUILDING_FILE).exists()
        assert report.build.table_counts["filings"] == 5 and report.added["filings"] == 4
        assert report.added["catalogue"] == 0 and report.added["catalogue_stats"] == 0
        assert report.build.forms == {"10-K": 1, "8-K": 3}
        assert all(not f["changed"] for f in report.invariance.values())
        assert report.invariance["filings"]["rows"] == 1
        marker = json.loads((world.derived / POST_A3_COMPLETE_MARKER).read_text())
        assert marker["stage"] == "POST_A3_INDEX_COMPLETE"
        assert marker["index_sha256"] == sha256_of(world.derived / POST_A3_INDEX_FILE)
        assert marker["final_manifest_sha256"] == world.final_manifest_sha
        assert marker["baseline_index_sha256"] == world.baseline_sha
        meta = json.loads((world.derived / POST_A3_META_FILE).read_text())
        assert meta["baseline_fingerprints"] == {
            k: {"rows": v.rows, "sha256": v.sha256}
            for k, v in world.baseline().fingerprints.items()
        }

    def test_co_registrant_copies_are_two_filing_rows(self, world: World) -> None:
        world.build()

        with closing(sqlite3.connect(world.derived / POST_A3_INDEX_FILE)) as db:
            rows = db.execute(
                "SELECT cik FROM filings WHERE accession = ? ORDER BY cik", (ACC["shared"],)
            ).fetchall()
            delta = db.execute("SELECT COUNT(*) FROM post_a3_delta").fetchone()[0]
        assert rows == [(100,), (200,)] and delta == 4

    def test_bytes_that_do_not_reproduce_the_manifest_yield_no_rows(self, world: World) -> None:
        target = filing_key(200, ACC["d"], "d.htm")
        world.storage.tamper[target] = cover("Other", "OTH", None)

        report = world.build()

        assert not report.complete and "IndexIntegrityError" in str(report.failure)
        with closing(sqlite3.connect(world.derived / POST_A3_BUILDING_FILE)) as db:
            assert (
                db.execute("SELECT COUNT(*) FROM filings WHERE path = ?", (target,)).fetchone()[0]
                == 0
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM build_ledger WHERE path = ?", (target,)
                ).fetchone()[0]
                == 0
            )
        assert not (world.derived / POST_A3_INDEX_FILE).exists()

    def test_a_resume_skips_what_it_committed_and_fingerprints_like_one_pass(
        self, world: World, tmp_path: Path, memory_storage: type[MemoryEvidenceStorage]
    ) -> None:
        first = world.build(stop_after=2)
        reads_after_first = dict(world.storage.reads)
        second = world.build()
        uninterrupted = World(tmp_path / "again", memory_storage)
        straight = uninterrupted.build()

        assert first.build.stopped_early and not first.complete
        assert second.complete and second.build.skipped_resume == 2
        assert all(world.storage.reads[k] == 1 for k in reads_after_first)
        assert second.build.fingerprints == straight.build.fingerprints

    def test_a_resume_bound_to_other_inputs_is_refused(self, world: World) -> None:
        world.build(stop_after=1)

        with pytest.raises(PostA3Error, match="bound to other inputs"):
            world.build(bindings=world.bindings(plan_sha256="a3" * 32))

    def test_a_completed_index_is_not_rebuilt_without_fresh(self, world: World) -> None:
        world.build()

        with pytest.raises(PostA3Error, match="already complete"):
            world.build()


class TestTheBaselineIsInvariant:
    def test_the_delta_changes_no_baseline_row(self, world: World) -> None:
        before = world.baseline().fingerprints

        report = world.build()

        with closing(sqlite3.connect(world.derived / POST_A3_INDEX_FILE)) as db:
            baseline_rows = db.execute(
                "SELECT COUNT(*) FROM filings WHERE url NOT IN (SELECT url FROM post_a3_delta)"
            ).fetchone()[0]
        assert baseline_rows == 1
        assert report.invariance["filings"]["before"] == before["filings"].sha256
        assert report.invariance["filings"]["after"] == before["filings"].sha256
        assert report.invariance["catalogue"]["after"] == before["catalogue"].sha256
        assert report.build.fingerprints["filings"].rows == 5
        assert report.build.fingerprints["filings"].sha256 != before["filings"].sha256
