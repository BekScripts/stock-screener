"""One URL, one request — even when a run asks for it twice.

Two securities can share a filer. `AIZ` and `AIZN` are Assurant's common and
preferred; `ALF` and `ALFUU` a SPAC's share and unit; a live row and its delisted
twin resolve to one CIK. Each resolves independently, so one invocation's target
list can hold the same submissions URL — and the same filing URLs — more than
once.

Before this fix the trusted set was a snapshot taken before the run, so the
second occurrence could not see what the first had just stored and fetched the
identical bytes again: 46 wasted requests in chunk 1, 181 corpus-wide. The bytes
were always identical, so nothing was corrupt — it was simply paid for twice.

The invariant these tests defend is narrow and load-bearing: an entry becomes
visible to later targets **only after its provenance is durable**. Trusting bytes
the manifest does not vouch for is the one thing the evidence contract cannot
survive.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
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

BODY = b'{"cik":"0001267238","filings":{}}'
DOC = b"<html>a filing document</html>"


@dataclass
class FakeSec:
    """Counts every fetch, per URL."""

    body: bytes = BODY
    fail: bool = False
    calls: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> FetchedDocument:
        self.calls.append(url)
        if self.fail:
            raise ProviderRequestError("sec unreachable")
        return FetchedDocument(
            url=url,
            status=200,
            body=self.body,
            sha256=hashlib.sha256(self.body).hexdigest(),
            content_type="application/json",
            etag=None,
            last_modified=None,
            retrieved_at=datetime.now(UTC),
        )

    def count(self, url: str) -> int:
        return sum(1 for c in self.calls if c == url)


@pytest.fixture
def root(tmp_path: Path) -> EvidenceRoot:
    base = tmp_path / "sec"
    base.mkdir()
    return open_evidence_root(Settings(sec_evidence_dir=str(base)))


def submissions(cik: int) -> Target:
    return Target(
        url=f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
        kind=RequestKind.SUBMISSIONS,
        cik=cik,
    )


def filing(cik: int, accession: str, document: str) -> Target:
    plain = accession.replace("-", "")
    return Target(
        url=f"https://www.sec.gov/Archives/edgar/data/{cik}/{plain}/{document}",
        kind=RequestKind.FILING,
        cik=cik,
        accession=accession,
        document=document,
        form="10-K",
        filing_date="2026-01-01",
    )


def entries(root: EvidenceRoot) -> list[Entry]:
    return list(Manifest(root.manifests / EVIDENCE_MANIFEST).entries())


class TestTwoSecuritiesOneFilingUrl:
    """The AIZ / AIZN shape: one filer, two securities, identical filing URL."""

    def test_the_same_filing_url_twice_costs_one_request(self, root: EvidenceRoot) -> None:
        client = FakeSec(body=DOC)
        target = filing(1267238, "0001267238-26-000001", "aiz-10k.htm")

        harvest([target, target], root, client)

        assert client.count(target.url) == 1

    def test_only_one_artifact_file_exists(self, root: EvidenceRoot) -> None:
        target = filing(1267238, "0001267238-26-000001", "aiz-10k.htm")

        harvest([target, target], root, FakeSec(body=DOC))

        files = [p for p in root.filings.rglob("*") if p.is_file() and not p.name.startswith("._")]
        assert len(files) == 1

    def test_the_duplicate_is_reported_as_a_cache_hit(self, root: EvidenceRoot) -> None:
        target = filing(1267238, "0001267238-26-000001", "aiz-10k.htm")

        report = harvest([target, target], root, FakeSec(body=DOC))

        assert report.by_outcome == {str(Outcome.DOWNLOADED): 1, str(Outcome.CACHE_HIT): 1}

    def test_the_manifest_gains_exactly_one_successful_record(self, root: EvidenceRoot) -> None:
        """A cache hit appends nothing, so duplication adds no provenance noise."""
        target = filing(1267238, "0001267238-26-000001", "aiz-10k.htm")

        harvest([target, target], root, FakeSec(body=DOC))

        assert len(entries(root)) == 1
        assert entries(root)[0].outcome is Outcome.DOWNLOADED


class TestTwoSecuritiesOneSubmissionsUrl:
    """The defect is not filing-only: shared CIKs share a submissions URL too."""

    def test_the_same_submissions_url_twice_costs_one_request(self, root: EvidenceRoot) -> None:
        client = FakeSec()
        target = submissions(1267238)

        harvest([target, target], root, client)

        assert client.count(target.url) == 1

    def test_one_stored_submissions_artifact(self, root: EvidenceRoot) -> None:
        harvest([submissions(1267238), submissions(1267238)], root, FakeSec())

        assert root.submissions_path(1267238).read_bytes() == BODY
        stored = [p for p in root.submissions.iterdir() if not p.name.startswith("._")]
        assert len(stored) == 1

    def test_one_trusted_manifest_entry(self, root: EvidenceRoot) -> None:
        harvest([submissions(1267238), submissions(1267238)], root, FakeSec())

        assert len(entries(root)) == 1


class TestTrustFollowsDurableProvenance:
    """Nothing may be trusted in memory that the manifest has not recorded."""

    def test_a_failed_append_leaves_the_url_untrusted(
        self, root: EvidenceRoot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Artifact written, manifest append fails — the run must not trust it."""
        client = FakeSec(body=DOC)
        target = filing(1267238, "0001267238-26-000001", "aiz-10k.htm")

        def explode(self: Manifest, entry: object) -> None:
            raise OSError("manifest volume gone")

        monkeypatch.setattr(Manifest, "append", explode)

        with pytest.raises(OSError, match="manifest volume gone"):
            harvest([target, target], root, client)

        # The append raised before any state update, so nothing became trusted.
        assert client.count(target.url) == 1

    def test_a_failed_fetch_does_not_become_trusted(self, root: EvidenceRoot) -> None:
        client = FakeSec(fail=True)
        target = submissions(1267238)

        report = harvest([target, target], root, client)

        assert report.by_outcome[str(Outcome.FAILED)] == 2
        assert client.count(target.url) == 2

    def test_a_zero_byte_response_never_becomes_trusted(self, root: EvidenceRoot) -> None:
        client = FakeSec(body=b"")
        target = submissions(1267238)

        report = harvest([target, target], root, client)

        assert report.by_outcome[str(Outcome.FAILED)] == 2
        assert not root.submissions_path(1267238).exists()

    def test_a_budget_skip_never_becomes_trusted(self, root: EvidenceRoot) -> None:
        """SKIPPED bytes were never stored, so a later run must re-fetch."""
        client = FakeSec()
        report = harvest(
            [submissions(1267238), submissions(1267238)],
            root,
            client,
            budget=Budget(max_bytes=len(BODY) - 1),
        )

        assert str(Outcome.CACHE_HIT) not in report.by_outcome
        assert not root.submissions_path(1267238).exists()


class TestBudgetAccountingWithDuplicates:
    def test_a_duplicate_consumes_no_request_budget(self, root: EvidenceRoot) -> None:
        """U1, U1, U2 with a ceiling of 2 must fetch both distinct URLs."""
        client = FakeSec()
        budget = Budget(max_requests=2)
        u1, u2 = submissions(1), submissions(2)

        report = harvest([u1, u1, u2], root, client, budget=budget)

        assert len(client.calls) == 2
        assert budget.requests_made == 2
        assert report.by_outcome[str(Outcome.DOWNLOADED)] == 2
        assert report.by_outcome[str(Outcome.CACHE_HIT)] == 1

    def test_a_duplicate_consumes_no_byte_budget(self, root: EvidenceRoot) -> None:
        budget = Budget()
        target = submissions(1267238)

        harvest([target, target], root, FakeSec(), budget=budget)

        assert budget.bytes_written == len(BODY)

    def test_attempt_numbering_is_unchanged_by_a_duplicate(self, root: EvidenceRoot) -> None:
        target = submissions(1267238)

        harvest([target, target], root, FakeSec())

        assert [e.attempt for e in entries(root)] == [1]

    def test_a_retry_after_failure_still_increments(self, root: EvidenceRoot) -> None:
        target = submissions(1267238)
        harvest([target], root, FakeSec(fail=True))

        harvest([target], root, FakeSec())

        assert [e.attempt for e in entries(root)] == [1, 2]


class TestHeavyDuplicationFetchesEachUrlOnce:
    def test_ten_thousand_rows_over_one_thousand_urls_fetch_one_thousand_times(
        self, root: EvidenceRoot
    ) -> None:
        """The structural guarantee at scale: requests track URLs, not rows."""
        client = FakeSec()
        urls = [submissions(cik) for cik in range(1, 1001)]
        targets = [t for t in urls for _ in range(10)]

        report = harvest(targets, root, client)

        assert len(targets) == 10_000
        assert len(client.calls) == 1_000
        assert len(set(client.calls)) == 1_000
        assert report.by_outcome[str(Outcome.DOWNLOADED)] == 1_000
        assert report.by_outcome[str(Outcome.CACHE_HIT)] == 9_000

    def test_the_manifest_records_only_the_downloads(self, root: EvidenceRoot) -> None:
        urls = [submissions(cik) for cik in range(1, 101)]

        harvest([t for t in urls for _ in range(10)], root, FakeSec())

        assert len(entries(root)) == 100

    def test_the_manifest_is_still_read_once_per_harvest(
        self, root: EvidenceRoot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads = {"n": 0}
        original = Manifest.entries

        def counted(self: Manifest):  # type: ignore[no-untyped-def]
            reads["n"] += 1
            return original(self)

        monkeypatch.setattr(Manifest, "entries", counted)
        urls = [submissions(cik) for cik in range(1, 51)]

        harvest([t for t in urls for _ in range(4)], root, FakeSec())

        assert reads["n"] == 1
