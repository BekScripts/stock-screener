"""The four prospective source adapters, each bounded to a decision instant.

Each adapter answers one question the decision packet asks, and each is
responsible for proving its own answer was knowable at the instant rather than
merely true now. That proof differs by source, which is why these are not one
generic fetch:

- the **universe** and **tradability** come from the broker's current asset
  list, which carries no history. For a live prospective decision that is
  admissible — §10 permits current metadata — but only with provenance recorded
  and a freshness bound applied, never backdated to an earlier instant.
- **SEC evidence** is genuinely point-in-time, because the submissions index
  publishes an acceptance timestamp per accession. That is the one source that
  can prove availability rather than assert it, so it is bounded strictly:
  `accepted_at <= T`, and `companyfacts` is refused outright because it cannot
  say which filing supplied a value.
- **prices** are bounded by session. Bars for the decision session and earlier
  are admissible however late they are fetched; a bar for a later session never
  is.

Nothing here writes to either frozen database, and nothing falls back to one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog

from .envelope import (
    Completeness,
    SnapshotEnvelope,
    StaleSourceError,
    content_digest,
    request_fingerprint,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

log = structlog.get_logger(__name__)

__all__ = [
    "NORMALIZER_VERSION",
    "SESSION_TIMEZONE",
    "SourceResult",
    "fetch_market",
    "fetch_sec_availability",
    "fetch_universe",
    "session_close",
]

#: Bump this when normalisation changes the records a source yields.
NORMALIZER_VERSION = "PROSPECTIVE_NORMALIZER_V1"

SESSION_TIMEZONE = "America/New_York"

#: `companyfacts` and friends: current aggregates that cannot attribute a value
#: to an accession, so they cannot prove decision-time availability.
NON_PIT_SAFE_OPERATIONS: frozenset[str] = frozenset(
    {"companyfacts", "get_financial_statements", "get_statement_profile_by_cik"}
)


def session_close(session: date) -> datetime:
    """The regular close of a session, as an aware moment.

    Args:
        session: The trading session.

    Returns:
        16:00 in the session timezone.
    """
    return datetime.combine(session, time(16, 0), tzinfo=ZoneInfo(SESSION_TIMEZONE))


@dataclass(frozen=True)
class SourceResult:
    """One adapter's output.

    Attributes:
        envelope: The snapshot envelope.
        payload: The raw payload, as retrieved.
        records: The normalised records.
    """

    envelope: SnapshotEnvelope
    payload: Any
    records: list[dict[str, Any]]

    @property
    def fingerprint(self) -> str:
        """The envelope's digest, for the decision packet."""
        return self.envelope.fingerprint()


def fetch_universe(provider: Any, *, decision_session: date) -> SourceResult:
    """Snapshot the tradable universe and its tradability attributes.

    The broker's asset list describes the present and carries no history, so its
    `semantic_as_of` is the retrieval moment — not the decision instant. That is
    the honest reading, and it is what makes the freshness policy do real work:
    a universe snapshot taken weeks before a decision is stale, and a snapshot
    taken after it describes a world the decision could not have seen.

    Args:
        provider: A market-data provider exposing `get_stock_universe`.
        decision_session: The session this snapshot is for.

    Returns:
        The snapshot, one record per listing.

    Raises:
        StaleSourceError: If the provider returned nothing usable.
    """
    retrieved = datetime.now(UTC)
    profiles = provider.get_stock_universe()
    if not profiles:
        raise StaleSourceError("the universe source returned no listings")
    records: list[dict[str, Any]] = []
    ambiguity: list[str] = []
    for profile in profiles:
        ticker = getattr(profile, "ticker", None)
        if not ticker:
            ambiguity.append("a listing arrived without a ticker and was dropped")
            continue
        records.append(
            {
                "ticker": ticker,
                "name": getattr(profile, "name", None),
                "exchange": getattr(profile, "exchange", None),
                "is_active": getattr(profile, "is_active", None),
                "is_fund": getattr(profile, "is_fund", None),
                "quote_currency": getattr(profile, "quote_currency", None),
                "tradability_source": "broker asset list",
                "tradability_semantics": "CURRENT_AT_RETRIEVAL",
            }
        )
    payload = {"listings": records}
    envelope = SnapshotEnvelope(
        source_type="universe",
        source_name=type(provider).__name__,
        source_version="broker asset list",
        operation="get_stock_universe",
        retrieved_at=retrieved,
        semantic_as_of=retrieved,
        content_sha256=content_digest(payload),
        request=request_fingerprint(type(provider).__name__, "get_stock_universe", {}),
        normalizer_version=NORMALIZER_VERSION,
        record_count=len(records),
        completeness=Completeness.COMPLETE if records else Completeness.UNAVAILABLE,
        decision_session=decision_session.isoformat(),
        ambiguity=ambiguity,
        notes=[
            "the broker asset list has no history, so semantic_as_of is the "
            "retrieval moment. It is never backdated to the decision instant.",
            "tradability and exchange come from this list; absence is never read as tradable.",
        ],
    )
    log.info("prospective.universe", listings=len(records))
    return SourceResult(envelope=envelope, payload=payload, records=records)


def fetch_sec_availability(
    source: Any, tickers: Sequence[str], *, decision_instant: datetime
) -> SourceResult:
    """Snapshot which SEC filings were public at the decision instant.

    This is the only source that can prove availability rather than assert it.
    The submissions index publishes `acceptanceDateTime` per accession, so a
    filing is admissible exactly when it was accepted at or before the instant,
    and every admitted value traces to an accession.

    Args:
        source: An observation source exposing `get_filing_acceptance`.
        tickers: The filers to ask about.
        decision_instant: The cutoff. Must be aware.

    Returns:
        The snapshot, one record per admitted filing, with those excluded for
        arriving too late counted separately.

    Raises:
        StaleSourceError: If the instant is naive.
    """
    if decision_instant.tzinfo is None:
        raise StaleSourceError("the decision instant must be timezone-aware")
    retrieved = datetime.now(UTC)
    admitted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for ticker in tickers:
        try:
            filings = source.get_filing_acceptance(ticker)
        except Exception as exc:
            unavailable.append(f"{ticker}: {type(exc).__name__}")
            continue
        for filing in filings:
            accepted = filing.accepted_at
            if accepted.tzinfo is None:
                accepted = accepted.replace(tzinfo=UTC)
            row = {
                "ticker": ticker,
                "accession": filing.accession,
                "form": filing.form,
                "accepted_at": accepted.isoformat(),
                "filed_date": filing.filed_date.isoformat(),
                "source": filing.source,
            }
            (admitted if accepted <= decision_instant else excluded).append(row)
    stamps = [row["accepted_at"] for row in admitted]
    payload = {"admitted": admitted, "excluded_after_cutoff": excluded}
    envelope = SnapshotEnvelope(
        source_type="sec_availability",
        source_name=type(source).__name__,
        source_version="EDGAR submissions index",
        operation="get_filing_acceptance",
        retrieved_at=retrieved,
        semantic_as_of=decision_instant,
        content_sha256=content_digest(payload),
        request=request_fingerprint(
            type(source).__name__,
            "get_filing_acceptance",
            {"tickers": sorted(tickers), "cutoff": decision_instant.isoformat()},
        ),
        normalizer_version=NORMALIZER_VERSION,
        record_count=len(admitted),
        completeness=(Completeness.INCOMPLETE if unavailable else Completeness.COMPLETE),
        decision_session=decision_instant.date().isoformat(),
        min_source_timestamp=min(stamps) if stamps else None,
        max_source_timestamp=max(stamps) if stamps else None,
        ambiguity=unavailable,
        notes=[
            f"{len(excluded)} filings were accepted after the instant and excluded",
            "admissibility is accepted_at <= decision instant, per accession",
            "companyfacts and other current aggregates are refused: they cannot "
            "attribute a value to an accession, so they cannot prove availability",
        ],
    )
    log.info(
        "prospective.sec",
        admitted=len(admitted),
        excluded=len(excluded),
        unavailable=len(unavailable),
    )
    return SourceResult(envelope=envelope, payload=payload, records=admitted)


def fetch_market(
    provider: Any,
    tickers: Sequence[str],
    *,
    decision_session: date,
    lookback_start: date,
) -> SourceResult:
    """Snapshot daily bars through the decision session, and no further.

    A lookback is ordinary: the frozen score needs history for volume and
    volatility features. What is not ordinary is a bar for a later session, so
    any that arrive are dropped and counted rather than quietly kept.

    Args:
        provider: A market-data provider exposing `get_daily_prices_batch`.
        tickers: The securities to price.
        decision_session: The last admissible session.
        lookback_start: The first session to retrieve.

    Returns:
        The snapshot, one record per admitted bar.

    Raises:
        StaleSourceError: If no bar for any ticker reaches the decision session.
    """
    retrieved = datetime.now(UTC)
    bars = provider.get_daily_prices_batch(list(tickers), lookback_start, decision_session)
    admitted: list[dict[str, Any]] = []
    future: list[dict[str, Any]] = []
    for ticker, series in sorted(bars.items()):
        for bar in series:
            row = {
                "ticker": ticker,
                "session": bar.date.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            (admitted if bar.date <= decision_session else future).append(row)
    reached = {row["ticker"] for row in admitted if row["session"] == decision_session.isoformat()}
    if not reached:
        raise StaleSourceError(
            f"no bar reaches the decision session {decision_session}; the price "
            "source cannot describe the decision instant"
        )
    sessions = [row["session"] for row in admitted]
    payload = {"bars": admitted, "dropped_future_bars": future}
    envelope = SnapshotEnvelope(
        source_type="market",
        source_name=type(provider).__name__,
        source_version="daily bars",
        operation="get_daily_prices_batch",
        retrieved_at=retrieved,
        semantic_as_of=session_close(decision_session),
        content_sha256=content_digest(payload),
        request=request_fingerprint(
            type(provider).__name__,
            "get_daily_prices_batch",
            {
                "tickers": sorted(tickers),
                "start": lookback_start.isoformat(),
                "end": decision_session.isoformat(),
            },
        ),
        normalizer_version=NORMALIZER_VERSION,
        record_count=len(admitted),
        completeness=Completeness.COMPLETE,
        decision_session=decision_session.isoformat(),
        min_source_timestamp=min(sessions) if sessions else None,
        max_source_timestamp=max(sessions) if sessions else None,
        ambiguity=[],
        notes=[
            f"{len(future)} bars postdated the decision session and were dropped",
            f"{len(reached)} of {len(tickers)} tickers have a bar for the session",
            "retrieval after the close is expected; semantic_as_of is the session's "
            "close, not the retrieval moment",
        ],
    )
    log.info(
        "prospective.market",
        bars=len(admitted),
        dropped_future=len(future),
        at_session=len(reached),
    )
    return SourceResult(envelope=envelope, payload=payload, records=admitted)
