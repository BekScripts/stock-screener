"""The request budget is a ceiling on actual HTTP attempts, not on intentions.

Phase 9K.15 measured a run given `--max-requests 15` issuing 16: the CIK
resolver's fetch of the SEC ticker file was a request the harvest caused but did
not itself make, and retries inside one target counted once between them. These
tests fix the unit of accounting — one attempt that reaches the network is one
unit of budget, wherever in the stack it originates.

Nothing here touches the network.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from api_clients._http import RetryPolicy, request_json, request_response
from api_clients.errors import ProviderBudgetError, ProviderError
from api_clients.sec_documents import FetchedDocument, SecDocuments
from stock_screener.config import Settings
from stock_screener.sec_evidence import (
    EVIDENCE_MANIFEST,
    Budget,
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

BODY = b'{"cik":"0000320193"}'


def transport(status: int = 200, body: bytes = BODY) -> httpx.MockTransport:
    """A transport that answers every request identically, counting nothing."""
    return httpx.MockTransport(lambda request: httpx.Response(status, content=body))


def counting_transport(statuses: list[int]) -> tuple[httpx.MockTransport, list[str]]:
    """A transport that records every attempt and replays the given statuses."""
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        status = statuses[min(len(seen) - 1, len(statuses) - 1)]
        return httpx.Response(status, content=BODY)

    return httpx.MockTransport(handle), seen


@pytest.fixture
def root(tmp_path: Path) -> EvidenceRoot:
    base = tmp_path / "sec"
    base.mkdir()
    return open_evidence_root(Settings(sec_evidence_dir=str(base)))


def target(cik: int) -> Target:
    return Target(
        url=f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
        kind=RequestKind.SUBMISSIONS,
        cik=cik,
    )


def documents(budget: Budget, tr: httpx.MockTransport) -> SecDocuments:
    return SecDocuments("tests test@example.com", meter=budget, client=httpx.Client(transport=tr))


class TestTheCeilingCountsActualAttempts:
    def test_a_budget_of_one_permits_exactly_one_http_attempt(self, root: EvidenceRoot) -> None:
        budget = Budget(max_requests=1)
        tr, seen = counting_transport([200])

        harvest([target(1), target(2), target(3)], root, documents(budget, tr))

        assert len(seen) == 1
        assert budget.requests_made == 1

    def test_a_budget_of_two_permits_exactly_two(self, root: EvidenceRoot) -> None:
        budget = Budget(max_requests=2)
        tr, seen = counting_transport([200])

        harvest([target(1), target(2), target(3)], root, documents(budget, tr))

        assert len(seen) == 2

    def test_no_attempt_is_made_once_the_ceiling_is_reached(self, root: EvidenceRoot) -> None:
        budget = Budget(max_requests=2)
        tr, seen = counting_transport([200])

        report = harvest([target(i) for i in range(1, 6)], root, documents(budget, tr))

        assert len(seen) == 2
        assert report.by_outcome[str(Outcome.SKIPPED)] == 3
        assert report.stopped_on_budget

    def test_every_retry_consumes_budget(self) -> None:
        """Three attempts at one URL is three requests, not one."""
        budget = Budget(max_requests=10)
        tr, seen = counting_transport([503])
        client = httpx.Client(transport=tr)

        with pytest.raises(ProviderError):
            request_response(
                client,
                "GET",
                "https://data.sec.gov/x.json",
                provider="sec",
                meter=budget,
                retry=RetryPolicy(attempts=3, backoff_seconds=0),
            )

        assert len(seen) == 3
        assert budget.requests_made == 3

    def test_a_retry_cannot_exceed_the_ceiling(self) -> None:
        budget = Budget(max_requests=2)
        tr, seen = counting_transport([503])
        client = httpx.Client(transport=tr)

        with pytest.raises(ProviderBudgetError):
            request_response(
                client,
                "GET",
                "https://data.sec.gov/x.json",
                provider="sec",
                meter=budget,
                retry=RetryPolicy(attempts=5, backoff_seconds=0),
            )

        assert len(seen) == 2

    def test_a_ticker_map_style_lookup_consumes_the_same_budget(self) -> None:
        """The resolver's fetch is a request the harvest causes; it must count."""
        budget = Budget(max_requests=1)
        client = httpx.Client(transport=transport(body=b"{}"))

        request_json(
            client,
            "GET",
            "https://www.sec.gov/files/company_tickers.json",
            provider="sec",
            meter=budget,
        )

        assert budget.requests_made == 1
        assert not budget.allow()

    def test_a_lookup_and_a_document_share_one_ceiling(self, root: EvidenceRoot) -> None:
        """The 9K.15 defect: 15 budgeted, 16 issued. One budget, one total."""
        budget = Budget(max_requests=1)
        client = httpx.Client(transport=transport(body=b"{}"))
        request_json(
            client,
            "GET",
            "https://www.sec.gov/files/company_tickers.json",
            provider="sec",
            meter=budget,
        )
        tr, seen = counting_transport([200])

        harvest([target(1)], root, documents(budget, tr))

        assert seen == []
        assert budget.requests_made == 1

    def test_an_unmetered_budget_is_unlimited(self, root: EvidenceRoot) -> None:
        budget = Budget()
        tr, seen = counting_transport([200])

        harvest([target(i) for i in range(1, 5)], root, documents(budget, tr))

        assert len(seen) == 4

    def test_accounting_survives_a_resume(self, root: EvidenceRoot) -> None:
        first = Budget(max_requests=2)
        tr, seen = counting_transport([200])
        harvest([target(i) for i in range(1, 5)], root, documents(first, tr))

        second = Budget(max_requests=2)
        report = harvest([target(i) for i in range(1, 5)], root, documents(second, tr))

        assert second.requests_made == 2
        assert len(seen) == 4
        assert report.by_outcome[str(Outcome.CACHE_HIT)] == 2


class TestAnEmptyBodyIsNotAnArtifact:
    def test_an_empty_two_hundred_is_recorded_failed(self, root: EvidenceRoot) -> None:
        budget = Budget()

        report = harvest([target(1)], root, documents(budget, transport(body=b"")))

        assert report.by_outcome == {str(Outcome.FAILED): 1}

    def test_no_zero_byte_file_is_written(self, root: EvidenceRoot) -> None:
        harvest([target(1)], root, documents(Budget(), transport(body=b"")))

        assert not root.submissions_path(1).exists()

    def test_an_empty_body_never_becomes_a_cache_hit(self, root: EvidenceRoot) -> None:
        """The failure this policy exists to prevent: permanent trust in nothing."""
        docs = documents(Budget(), transport(body=b""))
        harvest([target(1)], root, docs)

        report = harvest([target(1)], root, docs)

        assert str(Outcome.CACHE_HIT) not in report.by_outcome

    def test_the_failure_names_the_status_that_produced_it(self, root: EvidenceRoot) -> None:
        harvest([target(1)], root, documents(Budget(), transport(body=b"")))

        entry = next(iter(Manifest(root.manifests / EVIDENCE_MANIFEST).entries()))

        assert entry.status == 200
        assert entry.error is not None and "empty body" in entry.error

    def test_a_normal_artifact_is_unaffected(self, root: EvidenceRoot) -> None:
        report = harvest([target(1)], root, documents(Budget(), transport(body=BODY)))

        assert report.by_outcome == {str(Outcome.DOWNLOADED): 1}
        assert root.submissions_path(1).read_bytes() == BODY


class TestAnUnmeteredClientCannotOutrunTheCeiling:
    def test_a_source_that_does_not_meter_is_still_charged(self, root: EvidenceRoot) -> None:
        """The backstop. A fake — or a future client wired without a meter —
        must not run past the ceiling just because nothing counted for it."""

        class Unmetered:
            def __init__(self) -> None:
                self.calls = 0

            def fetch(self, url: str) -> FetchedDocument:
                self.calls += 1
                return FetchedDocument(
                    url=url,
                    status=200,
                    body=BODY,
                    sha256=hashlib.sha256(BODY).hexdigest(),
                    content_type=None,
                    etag=None,
                    last_modified=None,
                    retrieved_at=datetime.now(UTC),
                )

        client = Unmetered()
        budget = Budget(max_requests=2)

        harvest([target(i) for i in range(1, 6)], root, client, budget=budget)

        assert client.calls == 2
        assert budget.requests_made == 2

    def test_a_metered_client_is_not_double_charged(self, root: EvidenceRoot) -> None:
        budget = Budget(max_requests=10)
        tr, seen = counting_transport([200])

        harvest([target(1), target(2)], root, documents(budget, tr))

        assert len(seen) == 2
        assert budget.requests_made == 2


class TestFetchedDocumentAccounting:
    def test_a_fetched_document_reports_its_own_size(self) -> None:
        doc = FetchedDocument(
            url="https://x",
            status=200,
            body=BODY,
            sha256=hashlib.sha256(BODY).hexdigest(),
            content_type=None,
            etag=None,
            last_modified=None,
            retrieved_at=datetime.now(UTC),
        )

        assert doc.bytes_received == len(BODY)
