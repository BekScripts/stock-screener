"""Harvesting SEC evidence: provenance, budgets, resumability and cache trust.

Nothing here touches the network. A fake client returns canned documents, so the
tests assert what the harvester does with a response rather than what the SEC
happens to serve today.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from api_clients.errors import ProviderRequestError
from api_clients.sec_documents import FetchedDocument
from stock_screener.config import Settings
from stock_screener.sec_evidence import (
    EVIDENCE_MANIFEST,
    Budget,
    Entry,
    Manifest,
    Outcome,
    RequestKind,
    Target,
    harvest,
    open_evidence_root,
)

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence import EvidenceRoot

pytestmark = pytest.mark.integration

BODY = b'{"cik":"0000320193","filings":{}}'
DIGEST = hashlib.sha256(BODY).hexdigest()


@dataclass
class FakeSec:
    """A SEC client that serves canned bytes and counts what was asked of it."""

    body: bytes = BODY
    status: int = 200
    fail_with: Exception | None = None
    calls: list[str] | None = None

    def fetch(self, url: str) -> FetchedDocument:
        if self.calls is None:
            self.calls = []
        self.calls.append(url)
        if self.fail_with is not None:
            raise self.fail_with
        return FetchedDocument(
            url=url,
            status=self.status,
            body=self.body,
            sha256=hashlib.sha256(self.body).hexdigest(),
            content_type="application/json",
            etag='W/"abc"',
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
            retrieved_at=datetime.now(UTC),
        )

    @property
    def requests(self) -> int:
        return len(self.calls or [])


@pytest.fixture
def root(tmp_path: Path) -> EvidenceRoot:
    """A verified evidence root on a directory that exists."""
    base = tmp_path / "sec"
    base.mkdir()
    return open_evidence_root(Settings(sec_evidence_dir=str(base)))


def target(cik: int = 320193) -> Target:
    """One submissions target."""
    return Target(
        url=f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
        kind=RequestKind.SUBMISSIONS,
        cik=cik,
    )


def manifest_of(root: EvidenceRoot) -> Manifest:
    """The root's manifest."""
    return Manifest(root.manifests / EVIDENCE_MANIFEST)


class TestProvenanceIsRecordedForEveryArtifact:
    def test_a_download_records_the_five_required_fields(self, root: EvidenceRoot) -> None:
        harvest([target()], root, FakeSec())

        entry = next(iter(manifest_of(root).entries()))

        assert entry.url.endswith("CIK0000320193.json")
        assert entry.status == 200
        assert entry.sha256 == DIGEST
        assert entry.bytes == len(BODY)
        assert entry.retrieved_at is not None

    def test_optional_response_facts_are_kept_when_the_server_sends_them(
        self, root: EvidenceRoot
    ) -> None:
        harvest([target()], root, FakeSec())

        entry = next(iter(manifest_of(root).entries()))

        assert entry.content_type == "application/json"
        assert entry.etag == 'W/"abc"'
        assert entry.last_modified == "Wed, 01 Jan 2025 00:00:00 GMT"
        assert entry.cik == 320193

    def test_the_recorded_path_locates_the_stored_bytes(self, root: EvidenceRoot) -> None:
        harvest([target()], root, FakeSec())

        entry = next(iter(manifest_of(root).entries()))

        assert entry.path is not None
        assert (root.path / entry.path).read_bytes() == BODY

    def test_a_failure_is_recorded_rather_than_raised(self, root: EvidenceRoot) -> None:
        client = FakeSec(fail_with=ProviderRequestError("sec unreachable"))

        report = harvest([target()], root, client)

        entry = next(iter(manifest_of(root).entries()))
        assert report.by_outcome == {str(Outcome.FAILED): 1}
        assert entry.outcome is Outcome.FAILED
        assert entry.error is not None and "unreachable" in entry.error


class TestAnArtifactIsNeverFetchedTwice:
    def test_a_verified_artifact_is_a_cache_hit(self, root: EvidenceRoot) -> None:
        client = FakeSec()
        harvest([target()], root, client)

        report = harvest([target()], root, client)

        assert report.by_outcome == {str(Outcome.CACHE_HIT): 1}
        assert client.requests == 1

    def test_a_corrupted_artifact_is_refetched(self, root: EvidenceRoot) -> None:
        """Bytes that no longer match their digest are not evidence."""
        client = FakeSec()
        harvest([target()], root, client)
        stored = root.submissions_path(320193)
        stored.write_bytes(b"corrupted")

        report = harvest([target()], root, client)

        assert report.by_outcome == {str(Outcome.INVALID_CACHE): 1}
        assert client.requests == 2
        assert stored.read_bytes() == BODY

    def test_a_deleted_artifact_the_manifest_still_claims_is_refetched(
        self, root: EvidenceRoot
    ) -> None:
        client = FakeSec()
        harvest([target()], root, client)
        root.submissions_path(320193).unlink()

        report = harvest([target()], root, client)

        assert report.by_outcome == {str(Outcome.INVALID_CACHE): 1}

    def test_a_file_with_no_manifest_entry_is_refetched_conservatively(
        self, root: EvidenceRoot
    ) -> None:
        """Adopting it would invent provenance that was never observed."""
        client = FakeSec()
        stray = root.submissions_path(320193)
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(BODY)

        report = harvest([target()], root, client)

        assert report.by_outcome == {str(Outcome.DOWNLOADED): 1}
        assert client.requests == 1


class TestBudgetsStopTheRunCleanly:
    def test_the_request_limit_stops_further_requests(self, root: EvidenceRoot) -> None:
        client = FakeSec()
        targets = [target(cik) for cik in (1, 2, 3, 4, 5)]

        report = harvest(targets, root, client, budget=Budget(max_requests=2))

        assert client.requests == 2
        assert report.by_outcome[str(Outcome.DOWNLOADED)] == 2
        assert report.by_outcome[str(Outcome.SKIPPED)] == 3
        assert report.stopped_on_budget

    def test_the_byte_limit_stops_before_storing_too_much(self, root: EvidenceRoot) -> None:
        client = FakeSec()
        targets = [target(cik) for cik in (1, 2, 3)]

        report = harvest(targets, root, client, budget=Budget(max_bytes=len(BODY)))

        assert report.bytes_written == len(BODY)
        assert report.by_outcome[str(Outcome.DOWNLOADED)] == 1
        assert report.stopped_on_budget

    def test_a_response_that_would_cross_the_budget_is_not_stored(self, root: EvidenceRoot) -> None:
        """The bytes arrived but keeping them would break the promise."""
        client = FakeSec()

        report = harvest([target()], root, client, budget=Budget(max_bytes=len(BODY) - 1))

        assert report.by_outcome == {str(Outcome.SKIPPED): 1}
        assert not root.submissions_path(320193).exists()

    def test_a_run_resumes_where_the_budget_stopped_it(self, root: EvidenceRoot) -> None:
        client = FakeSec()
        targets = [target(cik) for cik in (1, 2, 3, 4)]
        harvest(targets, root, client, budget=Budget(max_requests=2))

        report = harvest(targets, root, client, budget=Budget(max_requests=2))

        assert report.by_outcome[str(Outcome.CACHE_HIT)] == 2
        assert report.by_outcome[str(Outcome.DOWNLOADED)] == 2
        assert client.requests == 4

    def test_an_exact_boundary_spends_the_whole_budget_and_no_more(
        self, root: EvidenceRoot
    ) -> None:
        client = FakeSec()
        targets = [target(cik) for cik in (1, 2)]

        report = harvest(targets, root, client, budget=Budget(max_bytes=2 * len(BODY)))

        assert report.by_outcome == {str(Outcome.DOWNLOADED): 2}
        assert report.bytes_written == 2 * len(BODY)


class TestADryRunTouchesNothing:
    def test_a_dry_run_issues_no_request_and_writes_nothing(self, root: EvidenceRoot) -> None:
        report = harvest([target()], root, None, dry_run=True)

        assert report.by_outcome == {str(Outcome.SKIPPED): 1}
        assert not (root.manifests / EVIDENCE_MANIFEST).exists()
        assert not root.submissions_path(320193).exists()

    def test_a_dry_run_still_reports_what_is_already_held(self, root: EvidenceRoot) -> None:
        harvest([target()], root, FakeSec())

        report = harvest([target()], root, None, dry_run=True)

        assert report.by_outcome == {str(Outcome.CACHE_HIT): 1}

    def test_a_real_run_without_a_client_is_refused(self, root: EvidenceRoot) -> None:
        with pytest.raises(ValueError, match="needs a SEC client"):
            harvest([target()], root, None)


class TestTheManifestSurvivesACrash:
    def test_a_truncated_final_line_does_not_destroy_the_log(self, root: EvidenceRoot) -> None:
        """The one shape a crash mid-append can leave."""
        harvest([target(1), target(2)], root, FakeSec())
        path = root.manifests / EVIDENCE_MANIFEST
        with path.open("a", encoding="utf-8") as stream:
            stream.write('{"url":"https://example.com","outc')

        entries = list(Manifest(path).entries())

        assert len(entries) == 2

    def test_a_retry_appends_rather_than_edits(self, root: EvidenceRoot) -> None:
        harvest([target()], root, FakeSec(fail_with=ProviderRequestError("down")))

        harvest([target()], root, FakeSec())

        entries = list(manifest_of(root).entries())
        assert [e.outcome for e in entries] == [Outcome.FAILED, Outcome.DOWNLOADED]
        assert manifest_of(root).latest_by_url()[entries[0].url].outcome is Outcome.DOWNLOADED

    def test_each_entry_is_one_json_object_per_line(self, root: EvidenceRoot) -> None:
        harvest([target(1), target(2)], root, FakeSec())

        lines = (root.manifests / EVIDENCE_MANIFEST).read_text().strip().split("\n")

        assert len(lines) == 2
        assert all(json.loads(line)["url"] for line in lines)

    def test_an_unknown_field_from_a_later_revision_still_reads(self, root: EvidenceRoot) -> None:
        path = root.manifests / EVIDENCE_MANIFEST
        payload = {"url": "https://x", "outcome": "DOWNLOADED", "invented_later": 1}
        path.write_text(json.dumps(payload) + "\n")

        entries = list(Manifest(path).entries())

        assert len(entries) == 1
        assert entries[0].url == "https://x"

    def test_attempts_are_counted_across_runs(self, root: EvidenceRoot) -> None:
        harvest([target()], root, FakeSec(fail_with=ProviderRequestError("down")))
        harvest([target()], root, FakeSec(fail_with=ProviderRequestError("down")))

        assert manifest_of(root).attempts_for(target().url) == 2

    def test_an_entry_append_is_durable(self, root: EvidenceRoot) -> None:
        path = root.manifests / EVIDENCE_MANIFEST
        manifest = Manifest(path)

        manifest.append(Entry(url="https://x", outcome=Outcome.DOWNLOADED, sha256="a", bytes=1))

        assert path.read_text().count("\n") == 1
