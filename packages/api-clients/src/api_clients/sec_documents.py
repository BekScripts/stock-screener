"""Fetching one SEC document, with everything needed to prove where it came from.

`edgar.py` reads the SEC's *structured* endpoints and returns parsed figures.
This reads the same host for *documents* — a filing's primary file, a company's
submissions index — and returns bytes plus the response facts that make a stored
artifact auditable: the status that produced it, and the `Content-Type`, `ETag`
and `Last-Modified` headers that describe it.

**This is not a second SEC client.** The URL conventions, the default hosts, the
User-Agent requirement, the rate limiter and the retry policy are all `edgar`'s
and `_http`'s, imported rather than restated. What is new here is only that the
response is returned whole instead of parsed, because an evidence corpus records
what it received rather than what it understood.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import structlog

from api_clients._http import RateLimiter, RequestMeter, RetryPolicy, request_response
from api_clients.edgar import (
    DEFAULT_DATA_URL,
    DEFAULT_MIN_INTERVAL_SECONDS,
    DEFAULT_WWW_URL,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FetchedDocument:
    """One document as received, with its provenance.

    Attributes:
        url: The absolute URL requested.
        status: The HTTP status that returned the body.
        body: The bytes exactly as received.
        sha256: Their digest, computed here so the caller cannot forget to.
        content_type: The declared media type, where the server sent one.
        etag: The entity tag, where the server sent one.
        last_modified: The `Last-Modified` header verbatim, where sent.
        retrieved_at: When the response arrived, in UTC.
    """

    url: str
    status: int
    body: bytes
    sha256: str
    content_type: str | None
    etag: str | None
    last_modified: str | None
    retrieved_at: datetime

    @property
    def bytes_received(self) -> int:
        """How many bytes the body holds."""
        return len(self.body)


def submissions_url(cik: int, *, data_base_url: str = DEFAULT_DATA_URL) -> str:
    """Return the submissions index URL for one filer.

    Args:
        cik: The filer number, unpadded.
        data_base_url: Host serving the structured endpoints.

    Returns:
        The absolute URL. The CIK is zero-padded to ten digits, which is the
        form this endpoint requires.
    """
    return f"{data_base_url.rstrip('/')}/submissions/CIK{cik:010d}.json"


def document_url(
    cik: int, accession: str, document: str, *, www_base_url: str = DEFAULT_WWW_URL
) -> str:
    """Return the archive URL of one document inside a filing.

    The archive path strips the dashes from the accession number while the
    printed form keeps them, which is why this is built rather than taken from
    a field — the same rule `edgar._document_url` follows.

    Args:
        cik: The filer number, unpadded.
        accession: The accession number, dashed or not.
        document: The file name within the filing.
        www_base_url: Host serving the archives.

    Returns:
        The absolute URL.
    """
    plain = accession.replace("-", "")
    return f"{www_base_url.rstrip('/')}/Archives/edgar/data/{cik}/{plain}/{document}"


class SecDocuments:
    """Fetches SEC documents one at a time, politely.

    Args:
        user_agent: Application name and a real contact address. The SEC blocks
            requests that do not identify the caller, so this is required.
        timeout_seconds: Per-request timeout.
        limiter: Rate limiter. Defaults to EDGAR's published interval.
        retry: Retry policy. Defaults to three bounded attempts.
        meter: Counts and caps actual HTTP attempts, retries included.
        client: HTTP client, injected by tests.

    Raises:
        ValueError: If no contact string is supplied.
    """

    def __init__(
        self,
        user_agent: str,
        *,
        timeout_seconds: float = 60.0,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        meter: RequestMeter | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("the SEC requires a User-Agent naming the app and a contact email")
        self._client = client or httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self._limiter = limiter or RateLimiter(DEFAULT_MIN_INTERVAL_SECONDS)
        self._retry = retry or RetryPolicy()
        self._meter = meter

    def fetch(self, url: str) -> FetchedDocument:
        """Fetch one document.

        Args:
            url: The absolute URL to read.

        Returns:
            The document and its provenance.

        Raises:
            ProviderAuthError: If the SEC rejected the caller.
            ProviderRateLimitError: If rate limited on the final attempt.
            ProviderRequestError: If the request failed on the final attempt.
        """
        response = request_response(
            self._client,
            "GET",
            url,
            provider="sec-documents",
            meter=self._meter,
            limiter=self._limiter,
            retry=self._retry,
        )
        body = response.content
        return FetchedDocument(
            url=url,
            status=response.status_code,
            body=body,
            sha256=hashlib.sha256(body).hexdigest(),
            content_type=_header(response.headers, "content-type"),
            etag=_header(response.headers, "etag"),
            last_modified=_header(response.headers, "last-modified"),
            retrieved_at=datetime.now(UTC),
        )

    def close(self) -> None:
        """Release the underlying HTTP connection."""
        self._client.close()

    def __enter__(self) -> SecDocuments:
        """Return self so the client can be used as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the client on exit."""
        self.close()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Return one response header, or None when the server did not send it.

    Absent is None rather than an empty string: the manifest records what the
    server said, and "" would claim it said nothing when it said nothing at all.
    """
    value = headers.get(name)
    return value if value else None
