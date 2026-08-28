"""The Tavily adapter: translating a vendor payload, and refusing to invent dates."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from api_clients import MockExternalResearch, TavilySearch
from api_clients.errors import ProviderAuthError, ProviderDataError
from domain import ExternalSearchResult


def _client(handler: object) -> httpx.Client:
    """An httpx client wired to a mock transport."""
    return httpx.Client(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        base_url="https://api.tavily.com",
    )


def _payload(*results: object) -> dict[str, object]:
    """A Tavily body. Deliberately accepts junk rows, which the adapter must survive."""
    return {"results": list(results)}


@pytest.mark.unit
def test_translates_a_vendor_result_into_a_search_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload(
                {
                    "title": "Micron raises guidance",
                    "url": "https://www.reuters.com/tech/micron",
                    "content": "Micron Technology raised its outlook.",
                    "published_date": "2026-08-12T09:30:00Z",
                    "source": "Reuters",
                }
            ),
        )

    provider = TavilySearch("key", client=_client(handler))

    results = provider.search("Micron", limit=5)

    assert results == [
        ExternalSearchResult(
            title="Micron raises guidance",
            url="https://www.reuters.com/tech/micron",
            snippet="Micron Technology raised its outlook.",
            published_at=date(2026, 8, 12),
            publisher="Reuters",
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("2026-08-12T09:30:00Z", date(2026, 8, 12)),
        ("2026-08-12", date(2026, 8, 12)),
        ("2026-08-12 09:30:00", date(2026, 8, 12)),
        ("", None),
        ("not a date", None),
        (None, None),
        (12345, None),
    ],
)
def test_a_date_is_read_or_left_absent_never_guessed(
    supplied: object, expected: date | None
) -> None:
    """A fabricated date on evidence that exists to be current is the worst error here."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload(
                {"title": "T", "url": "https://reuters.com/x", "published_date": supplied}
            ),
        )

    provider = TavilySearch("key", client=_client(handler))

    assert provider.search("q")[0].published_at == expected


@pytest.mark.unit
def test_a_result_without_a_url_or_title_is_skipped_not_fatal() -> None:
    """One bad row in twenty must not cost the other nineteen."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload(
                {"title": "no url"},
                {"url": "https://reuters.com/x"},
                "not an object",
                {"title": "good", "url": "https://reuters.com/y"},
            ),
        )

    provider = TavilySearch("key", client=_client(handler))

    results = provider.search("q")

    assert [result.title for result in results] == ["good"]


@pytest.mark.unit
def test_a_body_without_results_yields_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answer": "no results"})

    assert TavilySearch("key", client=_client(handler)).search("q") == []


@pytest.mark.unit
def test_the_window_is_sent_as_a_day_count() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_payload())

    provider = TavilySearch("key", client=_client(handler))
    provider.search("q", since=date(2020, 1, 1))

    assert isinstance(seen["days"], int)
    assert seen["days"] > 0


@pytest.mark.unit
def test_denied_domains_are_sent_to_the_vendor() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_payload())

    provider = TavilySearch("key", client=_client(handler), exclude_domains=["reddit.com"])
    provider.search("q")

    assert seen["exclude_domains"] == ["reddit.com"]


@pytest.mark.unit
def test_rejected_credentials_raise_rather_than_returning_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad key"})

    provider = TavilySearch("key", client=_client(handler))

    with pytest.raises(ProviderAuthError, match="tavily"):
        provider.search("q")


@pytest.mark.unit
def test_the_api_key_never_appears_in_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    provider = TavilySearch("sk-secret-value", client=_client(handler))

    with pytest.raises(Exception, match="tavily") as caught:
        provider.search("q")

    assert "sk-secret-value" not in str(caught.value)


@pytest.mark.unit
def test_the_mock_provider_returns_what_it_was_given_in_order() -> None:
    one = ExternalSearchResult(title="one", url="https://reuters.com/1")
    other = ExternalSearchResult(title="other", url="https://reuters.com/2")
    provider = MockExternalResearch({"": [one, other]})

    assert provider.search("anything") == [one, other]


@pytest.mark.unit
def test_the_mock_provider_matches_a_query_substring() -> None:
    hit = ExternalSearchResult(title="mu", url="https://reuters.com/mu")
    fallback = ExternalSearchResult(title="other", url="https://reuters.com/x")
    provider = MockExternalResearch({"micron": [hit], "": [fallback]})

    assert provider.search("Micron Technology (MU) earnings") == [hit]
    assert provider.search("Acme Corp") == [fallback]


@pytest.mark.unit
def test_the_mock_provider_can_be_made_to_fail() -> None:
    provider = MockExternalResearch(failing="provider unavailable")

    with pytest.raises(ProviderDataError, match="provider unavailable"):
        provider.search("q")
