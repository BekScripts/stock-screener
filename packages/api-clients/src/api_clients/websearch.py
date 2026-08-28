"""Tavily adapter for current external evidence, and a fixture-backed stand-in.

The narrowest possible search client: one POST, results back as
`ExternalSearchResult`. It assigns no tier, rejects nothing on quality grounds and
stores nothing — all of that is collection policy, and an adapter that decided any
of it would make the policy untestable without a transport.

Tavily was chosen over a general web search API for one reason that matters to
this contract: it reports `published_date` for most news results. A piece of
"current" evidence whose date nobody knows is close to useless — it cannot be aged
against a brief's `as_of`, and the alternative to a real date is not a guessed one
but `None`. Vendors with patchier date coverage push far more items into that
hole.

The vendor's ranking is passed through untouched. Nothing downstream reads the
order: the collector sorts, and the external fingerprint is order-insensitive, so
Tavily reshuffling its results tomorrow is not mistaken for new evidence.
"""

from __future__ import annotations

from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx

from api_clients._http import RateLimiter, RetryPolicy, request_json
from domain import ExternalSearchResult

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

PROVIDER_NAME = "tavily"

DEFAULT_BASE_URL = "https://api.tavily.com"

_SEARCH_PATH = "/search"

_MAX_SNIPPET_CHARS = 4000
"""How much of a vendor snippet to keep before the collector trims it further.

A generous ceiling, not the excerpt bound. `deep_research.MAX_EXCERPT_CHARS` is
the real limit and the collector applies it; this only stops a vendor returning a
whole page from occupying memory on the way there.
"""


def _as_date(value: Any) -> date | None:
    """Read a vendor date without ever inventing one.

    Tavily's news topic sends RFC 2822 — `Wed, 24 Jun 2026 16:32:00 GMT` — which
    is what its underlying feeds carry. An earlier version of this function tried
    ISO-8601 only, so every date silently became None, every item reached the
    brief undated, and the recency window stopped filtering anything at all. The
    failure was invisible because "no date" is a legitimate outcome, which is
    exactly why both formats are tried explicitly and a live payload is checked
    against them.

    Anything unparseable becomes None rather than today: a fabricated publication
    date on evidence that exists to be *current* is the one error this module must
    not make.

    Args:
        value: Whatever the vendor put in the date field.

    Returns:
        The publication date, or None when the vendor gave none or gave something
        that cannot be read as one.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()

    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _text(value: Any) -> str:
    """Return a vendor string field, or empty when it is missing or not a string."""
    return value.strip() if isinstance(value, str) else ""


class TavilySearch:
    """Current external material from Tavily's search API.

    Args:
        api_key: Tavily key. Sent as a bearer token, never logged and never
            placed in an exception message.
        base_url: API host.
        timeout_seconds: Per-request timeout.
        limiter: Minimum spacing between requests.
        retry: Retry policy. Defaults apply if omitted.
        exclude_domains: Domains the vendor should not return at all. Passed to
            the API so excluded material never crosses the wire, which is cheaper
            than filtering it here — but the collector applies the same policy
            again, because a vendor honouring a filter is not something to depend
            on for a correctness rule.
        client: HTTP client, injected by tests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        exclude_domains: Sequence[str] = (),
        client: httpx.Client | None = None,
    ) -> None:
        self._exclude = list(exclude_domains)
        self._limiter = limiter
        self._retry = retry
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def search(
        self, query: str, *, since: date | None = None, limit: int = 20
    ) -> list[ExternalSearchResult]:
        """Return current published material matching a query.

        Args:
            query: What to search for. Passed through unrewritten.
            since: Earliest publication date to include, translated into Tavily's
                `days` window. A vendor filter is a request rather than a
                guarantee, so the collector re-applies the window.
            limit: Most results to return.

        Returns:
            The results in the vendor's own ranking order.

        Raises:
            ProviderError: If the search could not be performed.
        """
        body: dict[str, Any] = {
            "query": query,
            "topic": "news",
            "search_depth": "basic",
            "max_results": limit,
            "include_answer": False,
            "include_raw_content": False,
        }
        if since is not None:
            days = (date.today() - since).days  # noqa: DTZ011 — a window width, not a timestamp
            body["days"] = max(1, days)
        if self._exclude:
            body["exclude_domains"] = self._exclude

        payload = request_json(
            self._client,
            "POST",
            _SEARCH_PATH,
            provider=PROVIDER_NAME,
            json=body,
            limiter=self._limiter,
            retry=self._retry,
        )
        return list(_parse(payload))

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()


def _parse(payload: Any) -> Iterable[ExternalSearchResult]:
    """Translate a Tavily response body into search results.

    A malformed entry is skipped rather than raising. One bad row in twenty must
    not cost the other nineteen, and a result with no URL is not a result — it is
    something the collector could never cite.
    """
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return

    for row in rows:
        if not isinstance(row, dict):
            continue
        url = _text(row.get("url"))
        title = _text(row.get("title"))
        if not url or not title:
            continue
        yield ExternalSearchResult(
            title=title,
            url=url,
            snippet=_text(row.get("content"))[:_MAX_SNIPPET_CHARS],
            published_at=_as_date(row.get("published_date")),
            publisher=_text(row.get("source")),
            author=_text(row.get("author")),
        )


class MockExternalResearch:
    """An `ExternalResearchProvider` serving pre-loaded results.

    What the fixture-driven configuration uses, and what every automated test
    uses. Returns exactly what it was given, in the order it was given, so a test
    asserting the collector's ordering, deduplication or tier policy is asserting
    the collector rather than a vendor's ranking.

    Args:
        results: Results by query substring. A query matching no key returns the
            entries under `""`, which is the catch-all.
        failing: When set, every search raises `ProviderDataError` with this
            message. Exercises the degradation path.
    """

    def __init__(
        self,
        results: Mapping[str, Sequence[ExternalSearchResult]] | None = None,
        *,
        failing: str = "",
    ) -> None:
        self._results = {key: list(value) for key, value in (results or {}).items()}
        self._failing = failing
        self.queries: list[str] = []

    def search(
        self, query: str, *, since: date | None = None, limit: int = 20
    ) -> list[ExternalSearchResult]:
        """Return the configured results for a query.

        Raises:
            ProviderDataError: If this mock was configured to fail.
        """
        from api_clients.errors import ProviderDataError

        self.queries.append(query)
        if self._failing:
            raise ProviderDataError(self._failing)

        for key, rows in self._results.items():
            if key and key.lower() in query.lower():
                return rows[:limit]
        return list(self._results.get("", []))[:limit]
