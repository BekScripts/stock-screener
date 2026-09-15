"""Executing a frozen target manifest instead of rediscovering filings.

The whole value of a validation batch is that the population is frozen. So the
tests that matter most here are the ones that make a *changed* population fatal
rather than silently accommodated: a wrong digest, a wrong row count, a repeated
filing identity, or a target the corpus already holds. Each of those would turn
"we validated the planner" into a sentence with no evidence behind it.

The second theme is that this path adds no new evidence semantics. It builds the
same `Target` records the production harvest builds, hands them to the same
`harvest()`, and produces artifacts indistinguishable from discovered ones — so
the tests assert on the existing contract rather than a parallel one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from api_clients.sec_documents import FetchedDocument
from stock_screener.config import Settings
from stock_screener.sec_evidence import (
    EVIDENCE_MANIFEST,
    Budget,
    Entry,
    Manifest,
    Outcome,
    RequestKind,
    harvest,
    open_evidence_root,
    open_index,
)
from stock_screener.sec_evidence.targets import (
    ManifestTarget,
    TargetManifestError,
    held_from_manifest,
    held_keys,
    load_manifest,
    resolve_documents,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

    from stock_screener.sec_evidence import EvidenceRoot

pytestmark = pytest.mark.integration


def row(
    cik: int = 1, accession: str = "0000000001-24-000001", **overrides: object
) -> dict[str, object]:
    """One manifest record."""
    record: dict[str, object] = {
        "plan_version": "v1-batch1",
        "rank": 1,
        "cik": cik,
        "accession": accession,
        "form": "8-K",
        "filing_date": "2024-03-15",
        "catalogue_source": f"submissions/CIK{cik:010d}.json",
        "already_downloaded": False,
        "potential_months": 3,
        "yield_adjusted_months": 2.9,
        "estimated_bytes": 41756,
        "first_target_month": "2024-03-31",
        "last_target_month": "2024-05-31",
        "reason": "covers 3 month-end(s)",
        "selection_strategy": "VALIDATION_BATCH_1",
    }
    record.update(overrides)
    return record


def write_manifest(path: Path, rows: list[dict[str, object]]) -> str:
    """Write a manifest and return its digest."""
    body = "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n"
    path.write_text(body, encoding="utf-8")
    return hashlib.sha256(body.encode()).hexdigest()


def _document(url: str, payload: bytes) -> FetchedDocument:
    """A fetched document shaped exactly as `SecDocuments` returns one."""
    return FetchedDocument(
        url=url,
        status=200,
        body=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        content_type="text/html",
        etag=None,
        last_modified=None,
        retrieved_at=datetime.now(UTC),
    )


@pytest.fixture
def root(tmp_path: Path) -> EvidenceRoot:
    base = tmp_path / "sec"
    base.mkdir()
    return open_evidence_root(Settings(sec_evidence_dir=str(base)))


@pytest.fixture
def catalogue(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A derived index describing one catalogue filing and holding none."""
    db = open_index(tmp_path / "index.db")
    db.execute(
        "INSERT INTO catalogue VALUES (1,'0000000001-24-000001','8-K','2024-03-15',"
        "'2024-03-15T18:00:00.000Z',NULL,'doc.htm',"
        "'https://www.sec.gov/Archives/edgar/data/1/000000000124000001/doc.htm')"
    )
    db.commit()
    try:
        yield db
    finally:
        db.close()


class TestManifestVerificationIsFatal:
    """A validation batch whose population changed validates nothing."""

    def test_a_matching_digest_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "m.jsonl"
        sha = write_manifest(path, [row()])

        targets, audit = load_manifest(path, expect_sha256=sha, expect_rows=1)

        assert len(targets) == 1
        assert audit.digest == sha
        assert audit.forms == {"8-K": 1}

    def test_a_wrong_digest_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "m.jsonl"
        write_manifest(path, [row()])

        with pytest.raises(TargetManifestError, match="not the frozen manifest"):
            load_manifest(path, expect_sha256="0" * 64)

    def test_a_wrong_row_count_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "m.jsonl"
        write_manifest(path, [row()])

        with pytest.raises(TargetManifestError, match="expected 800"):
            load_manifest(path, expect_rows=800)

    def test_a_repeated_filing_identity_raises(self, tmp_path: Path) -> None:
        """`(cik, accession)` must be unique or the accounting cannot balance."""
        path = tmp_path / "m.jsonl"
        write_manifest(path, [row(), row()])

        with pytest.raises(TargetManifestError, match="repeats filing identities"):
            load_manifest(path)

    def test_the_same_accession_under_two_ciks_is_not_a_duplicate(self, tmp_path: Path) -> None:
        """Co-registrant filings are distinct provenance, not a repeat."""
        path = tmp_path / "m.jsonl"
        write_manifest(path, [row(cik=7323), row(cik=65984)])

        targets, audit = load_manifest(path)

        assert audit.unique_keys == 2
        assert {t.cik for t in targets} == {7323, 65984}

    def test_a_missing_manifest_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TargetManifestError, match="no target manifest"):
            load_manifest(tmp_path / "absent.jsonl")

    def test_malformed_json_raises_rather_than_skipping_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "m.jsonl"
        path.write_text(json.dumps(row()) + "\nnot json\n", encoding="utf-8")

        with pytest.raises(TargetManifestError, match="line 2 is not JSON"):
            load_manifest(path)

    def test_a_record_missing_its_identity_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "m.jsonl"
        broken = row()
        del broken["accession"]
        path.write_text(json.dumps(broken) + "\n", encoding="utf-8")

        with pytest.raises(TargetManifestError, match="malformed"):
            load_manifest(path)


class TestResolutionUsesNoNetwork:
    def test_a_document_name_comes_from_the_stored_catalogue(
        self, catalogue: sqlite3.Connection
    ) -> None:
        target = ManifestTarget(1, "0000000001-24-000001", "8-K", "2024-03-15", 3, 2.9, 41756)

        resolved, unresolved = resolve_documents([target], catalogue)

        assert not unresolved
        assert resolved[0].document == "doc.htm"
        assert resolved[0].kind is RequestKind.FILING
        assert resolved[0].url.endswith("/doc.htm")

    def test_an_unknown_target_is_unresolved_rather_than_guessed(
        self, catalogue: sqlite3.Connection
    ) -> None:
        """A constructed URL would 404, spend budget, and look like an SEC fault."""
        target = ManifestTarget(999, "9999999999-24-000001", "8-K", "2024-03-15", 1, 1.0, 100)

        resolved, unresolved = resolve_documents([target], catalogue)

        assert not resolved
        assert unresolved[0].key == (999, "9999999999-24-000001")

    def test_resolution_carries_the_filing_identity_through(
        self, catalogue: sqlite3.Connection
    ) -> None:
        target = ManifestTarget(1, "0000000001-24-000001", "8-K", "2024-03-15", 3, 2.9, 41756)

        resolved, _ = resolve_documents([target], catalogue)

        assert resolved[0].cik == 1
        assert resolved[0].accession == "0000000001-24-000001"
        assert resolved[0].form == "8-K"

    def test_held_keys_reads_the_filing_identity_pair(self, catalogue: sqlite3.Connection) -> None:
        catalogue.execute("INSERT INTO filings (url, cik, accession) VALUES ('u', 7323, 'acc-1')")
        catalogue.execute("INSERT INTO filings (url, cik, accession) VALUES ('v', 65984, 'acc-1')")
        catalogue.commit()

        assert held_keys(catalogue) == {(7323, "acc-1"), (65984, "acc-1")}


class TestExplicitTargetsUseTheExistingWriter:
    """No second evidence format: a manifest artifact is an ordinary artifact."""

    def test_a_resolved_target_harvests_through_the_normal_contract(
        self, root: EvidenceRoot, catalogue: sqlite3.Connection
    ) -> None:
        payload = b"<html>cover</html>"

        class Source:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def fetch(self, url: str) -> FetchedDocument:

                self.urls.append(url)
                return _document(url, payload)

            def close(self) -> None:
                return None

        target = ManifestTarget(1, "0000000001-24-000001", "8-K", "2024-03-15", 3, 2.9, 41756)
        resolved, _ = resolve_documents([target], catalogue)
        source = Source()

        report = harvest(resolved, root, source, budget=Budget(max_requests=10, max_bytes=10**6))

        assert report.by_outcome.get("DOWNLOADED") == 1
        entries = list(Manifest(root.manifests / EVIDENCE_MANIFEST).entries())
        assert entries[0].sha256 == hashlib.sha256(payload).hexdigest()
        assert entries[0].request_kind == "FILING"
        assert entries[0].cik == 1
        assert entries[0].accession == "0000000001-24-000001"

    def test_a_replay_of_held_targets_issues_no_request(
        self, root: EvidenceRoot, catalogue: sqlite3.Connection
    ) -> None:
        """The convergence guarantee: nothing already trusted is fetched again."""
        payload = b"<html>cover</html>"

        class Source:
            def __init__(self) -> None:
                self.calls = 0

            def fetch(self, url: str) -> FetchedDocument:

                self.calls += 1
                return _document(url, payload)

            def close(self) -> None:
                return None

        target = ManifestTarget(1, "0000000001-24-000001", "8-K", "2024-03-15", 3, 2.9, 41756)
        resolved, _ = resolve_documents([target], catalogue)
        first = Source()
        harvest(resolved, root, first, budget=Budget(max_requests=10, max_bytes=10**6))

        second = Source()
        report = harvest(resolved, root, second, budget=Budget(max_requests=10, max_bytes=10**6))

        assert second.calls == 0, "a trusted artifact must never be re-fetched"
        assert report.by_outcome.get("DOWNLOADED", 0) == 0
        assert report.by_outcome.get("CACHE_HIT") == 1
        assert report.bytes_written == 0

    def test_the_budget_stops_the_run_before_it_overspends(
        self, root: EvidenceRoot, catalogue: sqlite3.Connection
    ) -> None:
        class Source:
            def fetch(self, url: str) -> FetchedDocument:

                return _document(url, b"x" * 100)

            def close(self) -> None:
                return None

        catalogue.execute(
            "INSERT INTO catalogue VALUES (2,'a-2','8-K','2024-03-15',NULL,NULL,'d.htm','u2')"
        )
        catalogue.commit()
        targets = [
            ManifestTarget(1, "0000000001-24-000001", "8-K", "2024-03-15", 1, 1.0, 100),
            ManifestTarget(2, "a-2", "8-K", "2024-03-15", 1, 1.0, 100),
        ]
        resolved, _ = resolve_documents(targets, catalogue)

        report = harvest(resolved, root, Source(), budget=Budget(max_requests=0, max_bytes=150))

        assert report.bytes_written <= 150
        assert sum(report.by_outcome.values()) == len(resolved)


class TestTargetAccountingBalances:
    def test_every_target_reaches_exactly_one_terminal_state(
        self, root: EvidenceRoot, catalogue: sqlite3.Connection
    ) -> None:
        """No target may silently disappear between manifest and report."""
        catalogue.execute(
            "INSERT INTO catalogue VALUES (3,'a-3','10-Q','2024-05-01',NULL,NULL,'q.htm','u3')"
        )
        catalogue.commit()

        class Source:
            def fetch(self, url: str) -> FetchedDocument:

                return _document(url, b"ok")

            def close(self) -> None:
                return None

        targets = [
            ManifestTarget(1, "0000000001-24-000001", "8-K", "2024-03-15", 1, 1.0, 100),
            ManifestTarget(3, "a-3", "10-Q", "2024-05-01", 1, 1.0, 100),
            ManifestTarget(9, "missing", "8-K", "2024-05-01", 1, 1.0, 100),
        ]
        resolved, unresolved = resolve_documents(targets, catalogue)
        report = harvest(resolved, root, Source(), budget=Budget(max_requests=10, max_bytes=10**6))

        accounted = sum(report.by_outcome.values())
        assert accounted + len(unresolved) == len(targets)
        assert len(unresolved) == 1


class TestPreflightRejectsAChangedPopulation:
    def test_a_target_already_trusted_is_detected_before_network(
        self, catalogue: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Silently dropping it would change the frozen validation population."""
        catalogue.execute(
            "INSERT INTO filings (url, cik, accession) VALUES ('u', 1, '0000000001-24-000001')"
        )
        catalogue.commit()
        path = tmp_path / "m.jsonl"
        write_manifest(path, [row()])
        targets, _ = load_manifest(path)

        overlap = {t.key for t in targets} & held_keys(catalogue)

        assert overlap == {(1, "0000000001-24-000001")}


class TestHeldSetComesFromTheManifest:
    """Which source answers "do we already have this filing".

    Phase 9L.4 planned 800 filings that were already on disk, because the check
    read the derived index — a snapshot that can lag the corpus. The manifest is
    the authority on what has been acquired; the index is an analysis artifact
    rebuilt on demand. Reading the wrong one wastes requests and, worse, makes a
    "disjoint from the corpus" claim that is not true.
    """

    def test_the_manifest_reports_what_was_actually_harvested(self, root: EvidenceRoot) -> None:
        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)
        manifest.append(
            Entry(
                url="https://sec.gov/a.htm",
                outcome=Outcome.DOWNLOADED,
                status=200,
                sha256="a" * 64,
                bytes=10,
                path="filings/a.htm",
                request_kind="FILING",
                cik=7323,
                accession="acc-1",
            )
        )

        assert held_from_manifest(manifest) == {(7323, "acc-1")}

    def test_a_stale_index_and_the_manifest_can_disagree(
        self, root: EvidenceRoot, catalogue: sqlite3.Connection
    ) -> None:
        """The exact 9L.4 shape: harvested, but the index has not been rebuilt."""
        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)
        manifest.append(
            Entry(
                url="https://sec.gov/a.htm",
                outcome=Outcome.DOWNLOADED,
                status=200,
                sha256="a" * 64,
                bytes=10,
                path="filings/a.htm",
                request_kind="FILING",
                cik=1,
                accession="0000000001-24-000001",
            )
        )

        assert held_keys(catalogue) == frozenset(), "index has not seen the harvest"
        assert held_from_manifest(manifest) == {(1, "0000000001-24-000001")}

    def test_a_failed_record_is_not_counted_as_held(self, root: EvidenceRoot) -> None:
        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)
        manifest.append(
            Entry(
                url="https://sec.gov/a.htm",
                outcome=Outcome.FAILED,
                status=None,
                sha256="",
                request_kind="FILING",
                cik=1,
                accession="acc-1",
                error="ConnectError",
            )
        )

        assert held_from_manifest(manifest) == frozenset()

    def test_a_later_success_supersedes_an_earlier_failure(self, root: EvidenceRoot) -> None:
        """The 9L.3 recovery shape: pass 1 failed, pass 2 stored it."""
        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)
        manifest.append(
            Entry(
                url="https://sec.gov/a.htm",
                outcome=Outcome.FAILED,
                status=None,
                sha256="",
                request_kind="FILING",
                cik=1,
                accession="acc-1",
            )
        )
        manifest.append(
            Entry(
                url="https://sec.gov/a.htm",
                outcome=Outcome.DOWNLOADED,
                status=200,
                sha256="a" * 64,
                bytes=10,
                path="filings/a.htm",
                request_kind="FILING",
                cik=1,
                accession="acc-1",
            )
        )

        assert held_from_manifest(manifest) == {(1, "acc-1")}

    def test_a_submissions_record_is_not_a_filing(self, root: EvidenceRoot) -> None:
        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)
        manifest.append(
            Entry(
                url="https://data.sec.gov/submissions/CIK0000000001.json",
                outcome=Outcome.DOWNLOADED,
                status=200,
                sha256="a" * 64,
                bytes=10,
                path="submissions/CIK0000000001.json",
                request_kind="SUBMISSIONS",
                cik=1,
            )
        )

        assert held_from_manifest(manifest) == frozenset()
