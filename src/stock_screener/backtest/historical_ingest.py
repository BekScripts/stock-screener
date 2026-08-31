"""Ingesting a historical security from free EDGAR + Alpaca data.

The one network path in the historical layer. It fetches a delisted filer's
record by its **CIK** — the only handle that still resolves once the ticker has
left the current map or been reused — and its prices and splits by ticker, and
stores them in the parallel historical tables. Nothing here reaches the
`companies` table; a delisted security is never made to look investable.

What it will not do is guess. A fundamental the free record does not carry stays
absent; a terminal value no source states stays NULL, which a reader must read as
UNKNOWN and never as zero. A receivership, which no corporate-action feed files,
takes its status only from an `asserted_status` a caller supplies from evidence
outside the feed — it is never inferred.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from api_clients.errors import ProviderError
from data_access import (
    HistoricalCorporateActionRepository,
    HistoricalObservationRepository,
    HistoricalPriceRepository,
    HistoricalSecurityRepository,
    session_scope,
)
from domain import (
    CorporateActionKind,
    DelistingReason,
    StatementProfile,
    classify_delisting,
    normalise_ticker,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from sqlalchemy.orm import Session, sessionmaker

    from domain import CorporateActionRecord, DelistingRecord, FilingObservation, PriceBar

log = structlog.get_logger(__name__)

#: The split kinds that change a share count and therefore a stored close.
_SPLIT_KINDS = frozenset({CorporateActionKind.FORWARD_SPLIT, CorporateActionKind.REVERSE_SPLIT})


@runtime_checkable
class HistoricalObservationSource(Protocol):
    """A provider that can report a filer's facts and profile by CIK."""

    def get_filing_observations_by_cik(self, cik: int) -> list[FilingObservation]:
        """Return every reported fact behind a filer's statements, by CIK."""
        ...

    def get_statement_profile_by_cik(self, cik: int) -> StatementProfile:
        """Classify the filer as a bank or a general company, by CIK."""
        ...


@runtime_checkable
class HistoricalMarketData(Protocol):
    """The market-data reads the historical ingestion needs."""

    def get_daily_prices(self, ticker: str, start: date, end: date) -> list[PriceBar]:
        """Return daily bars for a symbol over a window."""
        ...

    def get_corporate_actions(
        self, symbols: Sequence[str], start: date, end: date
    ) -> list[CorporateActionRecord]:
        """Return corporate actions for symbols over a window."""
        ...

    def list_delistings(self, start: date, end: date) -> list[DelistingRecord]:
        """Return every security that left the market in a window."""
        ...


@dataclass(frozen=True)
class HistoricalIngestResult:
    """What one historical-security ingestion recorded.

    Attributes:
        cik: The filer ingested.
        ticker: Its trading symbol.
        status: The fate recorded — a `DelistingReason` value.
        observations_stored: Observations newly appended.
        prices_stored: Price bars newly written.
        splits_stored: Split actions newly appended.
        last_trading_date: The last session a price was found for, or None.
        terminal_value_per_share: The terminal value, when a source stated one.
        gaps: Named things the free record could not establish, kept UNKNOWN.
    """

    cik: int
    ticker: str
    status: str
    observations_stored: int = 0
    prices_stored: int = 0
    splits_stored: int = 0
    last_trading_date: date | None = None
    terminal_value_per_share: float | None = None
    gaps: tuple[str, ...] = ()

    def summary(self) -> str:
        """Return a one-line description for a terminal."""
        terminal = (
            f"{self.terminal_value_per_share}/sh"
            if self.terminal_value_per_share is not None
            else "UNKNOWN"
        )
        return (
            f"{self.ticker} (CIK {self.cik}) {self.status}: "
            f"{self.observations_stored} obs, {self.prices_stored} bars, "
            f"{self.splits_stored} splits, last traded {self.last_trading_date or 'UNKNOWN'}, "
            f"terminal {terminal}, gaps [{', '.join(self.gaps) or 'none'}]"
        )


def _fingerprint(cik: int, observation: FilingObservation) -> str:
    """Return a stable digest of one observation's identity within a security."""
    parts = (
        cik,
        observation.taxonomy,
        observation.concept,
        observation.unit,
        observation.period_start,
        observation.period_end,
        observation.filed_date,
        observation.accession,
        observation.form,
        observation.value,
    )
    joined = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _observation_rows(cik: int, observations: Iterable[FilingObservation]) -> list[dict[str, Any]]:
    """Return column mappings for a filer's observations."""
    now = datetime.now(UTC)
    return [
        {
            "observation_fingerprint": _fingerprint(cik, observation),
            "taxonomy": observation.taxonomy,
            "concept": observation.concept,
            "value": observation.value,
            "unit": observation.unit,
            "period_start": observation.period_start,
            "period_end": observation.period_end,
            "filed_date": observation.filed_date,
            "accession": observation.accession,
            "form": observation.form,
            "fiscal_year": observation.fiscal_year,
            "fiscal_period": observation.fiscal_period,
            "frame": observation.frame,
            "source": observation.source,
            "observed_at": now,
        }
        for observation in observations
    ]


def _action_fingerprint(cik: int, action: CorporateActionRecord) -> str:
    """Return a stable digest of one action's identity within a security."""
    parts = (cik, action.kind.value, action.ex_date, action.split_factor, action.source_id)
    joined = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _reporting_currency(observations: Sequence[FilingObservation]) -> str | None:
    """Return the modal currency the statements were filed in, or None.

    Read from the XBRL unit keys, never assumed: a three-letter unit is a
    currency, `shares` and `USD/shares` are not. None where nothing qualifies,
    which downstream treats as USD.
    """
    codes = [
        obs.unit
        for obs in observations
        if len(obs.unit) == 3 and obs.unit.isalpha() and obs.unit.isupper()
    ]
    if not codes:
        return None
    return Counter(codes).most_common(1)[0][0]


def _fetch_fundamentals(
    source: HistoricalObservationSource, cik: int
) -> tuple[list[FilingObservation], str | None]:
    """Fetch a filer's observations and statement profile, tolerating absence.

    An EDGAR `404` is not an error here: an FDIC-only bank and a pre-2009 filer
    both have no `companyfacts`, and the honest outcome is empty fundamentals
    with the gap recorded, not a failed ingestion.
    """
    try:
        observations = source.get_filing_observations_by_cik(cik)
    except ProviderError:
        return [], None
    if not observations:
        return [], None
    try:
        profile = source.get_statement_profile_by_cik(cik).value
    except ProviderError:
        profile = None
    return observations, profile


def _filing_span(observations: Sequence[FilingObservation]) -> tuple[date | None, date | None]:
    """Return the earliest and latest filing dates in the record."""
    filed = sorted(obs.filed_date for obs in observations if obs.filed_date is not None)
    return (filed[0], filed[-1]) if filed else (None, None)


def _terminal_from_feed(
    market: HistoricalMarketData, ticker: str, end: date
) -> DelistingRecord | None:
    """Return the ticker's delisting near `end` from the action feed, or None."""
    window_start = end - timedelta(days=90)
    window_end = end + timedelta(days=15)
    for record in market.list_delistings(window_start, window_end):
        if record.symbol == ticker:
            return record
    return None


@dataclass(frozen=True)
class CohortEntry:
    """One delisted security a caller has established the identity of.

    A cohort is not derivable from the delisting registry alone: a delisting
    carries a ticker, and a ticker is not an identity once the symbol has been
    reissued. Establishing the CIK is the caller's job, and this is the shape it
    hands over — every field either verified from a free source or absent.

    Attributes:
        cik: The SEC filer identifier. The security's identity.
        ticker: The symbol it traded under, for the price-keyed reads.
        name: A human name for the record.
        start: Earliest session to fetch prices from.
        end: Latest session to fetch prices to — around the delisting.
        exchange: The venue, where known. NULL where not.
        asserted_status: A fate established outside the action feed, for a
            receivership no feed files.
        asserted_terminal_value: A terminal value from evidence outside the feed.
        asserted_terminal_source: Where that asserted value came from.
    """

    cik: int
    ticker: str
    name: str
    start: date
    end: date
    exchange: str | None = None
    asserted_status: DelistingReason | None = None
    asserted_terminal_value: float | None = None
    asserted_terminal_source: str | None = None


@dataclass(frozen=True)
class CohortIngestReport:
    """What ingesting a whole cohort recorded, and what it could not.

    Attributes:
        results: One result per security that was ingested.
        failures: Securities whose ingestion raised, with the reason.
    """

    results: tuple[HistoricalIngestResult, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()

    @property
    def with_fundamentals(self) -> int:
        """How many securities stored at least one filing observation."""
        return sum(1 for r in self.results if r.observations_stored > 0)

    @property
    def with_prices(self) -> int:
        """How many securities stored at least one price bar."""
        return sum(1 for r in self.results if r.prices_stored > 0)

    @property
    def with_actions(self) -> int:
        """How many securities stored at least one split."""
        return sum(1 for r in self.results if r.splits_stored > 0)

    @property
    def with_terminal_value(self) -> int:
        """How many securities a source stated a terminal value for."""
        return sum(1 for r in self.results if r.terminal_value_per_share is not None)

    def gap_counts(self) -> dict[str, int]:
        """Return how many securities carried each named gap."""
        counts: Counter[str] = Counter()
        for result in self.results:
            counts.update(result.gaps)
        return dict(counts)

    def summary(self) -> str:
        """Return a one-line description for a terminal."""
        return (
            f"cohort: {len(self.results)} ingested, {len(self.failures)} failed — "
            f"{self.with_fundamentals} with fundamentals, {self.with_prices} with prices, "
            f"{self.with_actions} with actions, {self.with_terminal_value} with a terminal value"
        )


def ingest_cohort(
    factory: sessionmaker[Session],
    *,
    observations_source: object,
    market: object,
    cohort: Iterable[CohortEntry],
    on_progress: Callable[[HistoricalIngestResult, int], None] | None = None,
) -> CohortIngestReport:
    """Ingest a whole cohort of delisted securities, one at a time.

    **One transaction per security**, so a cohort of several hundred names —
    each of them several network round trips — is resumable and does not hold a
    write lock for the hour it takes. A failure on one is recorded rather than
    allowed to end the run: a delisted name whose facts EDGAR does not serve is
    the expected case here, not an exceptional one.

    Args:
        factory: Session factory. One session is opened per security.
        observations_source: Fetches filers' facts by CIK (EDGAR). Must satisfy
            `HistoricalObservationSource`.
        market: Fetches prices, splits and delistings (Alpaca). Must satisfy
            `HistoricalMarketData`.
        cohort: The securities to ingest, each with its identity established.
        on_progress: Called after each security with its result and position.

    Returns:
        What was recorded across the cohort, and what could not be. A provider
        that cannot serve the historical reads gives an empty report rather than
        a run that half-ingests and then fails, which follows the convention the
        other provider-backed passes use.
    """
    if not isinstance(observations_source, HistoricalObservationSource) or not isinstance(
        market, HistoricalMarketData
    ):
        log.warning(
            "provider cannot serve the historical reads, skipping",
            observations=type(observations_source).__name__,
            market=type(market).__name__,
        )
        return CohortIngestReport()

    results: list[HistoricalIngestResult] = []
    failures: list[tuple[str, str]] = []
    for index, entry in enumerate(cohort, start=1):
        try:
            with session_scope(factory) as session:
                result = ingest_historical_security(
                    session,
                    observations_source=observations_source,
                    market=market,
                    cik=entry.cik,
                    ticker=entry.ticker,
                    name=entry.name,
                    start=entry.start,
                    end=entry.end,
                    exchange=entry.exchange,
                    asserted_status=entry.asserted_status,
                    asserted_terminal_value=entry.asserted_terminal_value,
                    asserted_terminal_source=entry.asserted_terminal_source,
                )
        except (ProviderError, ValueError) as error:
            failures.append((entry.ticker, f"{type(error).__name__}: {error}"))
            log.warning(
                "historical security could not be ingested",
                ticker=entry.ticker,
                cik=entry.cik,
                detail=str(error),
            )
            continue
        results.append(result)
        if on_progress is not None:
            on_progress(result, index)
    return CohortIngestReport(results=tuple(results), failures=tuple(failures))


def ingest_historical_security(
    session: Session,
    *,
    observations_source: HistoricalObservationSource,
    market: HistoricalMarketData,
    cik: int,
    ticker: str,
    name: str,
    start: date,
    end: date,
    exchange: str | None = None,
    asserted_status: DelistingReason | None = None,
    asserted_terminal_value: float | None = None,
    asserted_terminal_source: str | None = None,
) -> HistoricalIngestResult:
    """Ingest one delisted security from free EDGAR + Alpaca data.

    Args:
        session: Open database session. The caller commits.
        observations_source: Fetches the filer's facts by CIK (EDGAR).
        market: Fetches prices, splits and delistings (Alpaca).
        cik: The filer's SEC identifier — the security's identity.
        ticker: Its trading symbol, for the price-keyed reads.
        name: A human name for the record.
        start: Earliest session to fetch prices from.
        end: Latest session to fetch prices to — around the delisting.
        exchange: The venue the security traded on, a known fact for a named
            security. NULL where unknown, which the screen reads as unsupported.
        asserted_status: A fate established outside the action feed, for a
            receivership the feed does not file. Overrides the feed.
        asserted_terminal_value: A terminal value from evidence outside the feed.
        asserted_terminal_source: Where that asserted value came from.

    Returns:
        What was recorded, including the gaps the free record left UNKNOWN.
    """
    symbol = normalise_ticker(ticker)
    gaps: list[str] = []

    observations, statement_profile = _fetch_fundamentals(observations_source, cik)
    if not observations:
        # A pre-2009 filer, or one whose statements are filed with another
        # regulator (an FDIC-only bank). Recorded, not guessed at.
        gaps.append("NO_EDGAR_FUNDAMENTALS")

    bars = market.get_daily_prices(symbol, start, end)
    if not bars:
        gaps.append("NO_FREE_PRICE_HISTORY")
    last_trading_date = bars[-1].date if bars else None

    raw_actions = market.get_corporate_actions([symbol], start, end)
    splits = [action for action in raw_actions if action.kind in _SPLIT_KINDS]

    delisting = _terminal_from_feed(market, symbol, end)
    if asserted_status is not None:
        status = asserted_status
        terminal_value = asserted_terminal_value
        terminal_source = asserted_terminal_source
    elif delisting is not None:
        status = classify_delisting(delisting.kind)
        terminal_value = delisting.cash_per_share
        terminal_source = delisting.source or None
    else:
        status = DelistingReason.UNKNOWN
        terminal_value = None
        terminal_source = None
    if terminal_value is None:
        gaps.append("NO_TERMINAL_VALUE")

    first_filing, last_filing = _filing_span(observations)
    security = HistoricalSecurityRepository(session).upsert(
        cik,
        name=name,
        primary_ticker=symbol,
        exchange=exchange,
        status=status.value,
        sector=None,
        industry=None,
        statement_profile=statement_profile,
        first_filing_date=first_filing,
        last_filing_date=last_filing,
        last_trading_date=last_trading_date,
        terminal_value_per_share=terminal_value,
        terminal_value_source=terminal_source,
        reporting_currency=_reporting_currency(observations),
        quote_currency="USD",
    )

    stored_obs = HistoricalObservationRepository(session).add_all(
        security.id, _observation_rows(cik, observations)
    )
    stored_prices = HistoricalPriceRepository(session).upsert_bars(security.id, bars)
    stored_splits = HistoricalCorporateActionRepository(session).add_all(
        security.id,
        [
            {
                "action_fingerprint": _action_fingerprint(cik, action),
                "ticker": symbol,
                "action_type": action.kind.value,
                "ex_date": action.ex_date,
                "split_factor": action.split_factor,
                "cash_amount": action.cash_amount,
                "currency": action.currency,
                "record_date": action.record_date,
                "payable_date": action.payable_date,
                "source": action.source,
                "source_id": action.source_id,
                "observed_at": datetime.now(UTC),
            }
            for action in splits
        ],
    )

    result = HistoricalIngestResult(
        cik=cik,
        ticker=symbol,
        status=status.value,
        observations_stored=stored_obs,
        prices_stored=stored_prices,
        splits_stored=stored_splits,
        last_trading_date=last_trading_date,
        terminal_value_per_share=terminal_value,
        gaps=tuple(gaps),
    )
    log.info("historical security ingested", summary=result.summary())
    return result
