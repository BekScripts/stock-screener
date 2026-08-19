"""Refresh everything known about one company, then stop.

The network half of deep research, and deliberately the only half that touches a
provider. It runs the existing ingestion and scoring passes for a single symbol,
in the order their dependencies require, and records what each one did. Brief
assembly happens afterwards, against the database, with no network access at
all — the same split Phase 3 draws between `prepare_filing_evidence` and
`assemble_brief`, for the same reason: an assembler that could fetch is an
assembler whose output depends on when you ran it.

**Nothing here is new pipeline code.** Every stage is a call into the pass the
CLI already runs market-wide, narrowed to one ticker by the `tickers` argument
those passes have always taken. A second fundamentals engine or a second scoring
path would be a second thing to keep in agreement with the scanner, and the one
that drifted would be the one nobody was watching. In particular the score is
`score_market`, persisted exactly as a nightly run persists it: deep research
consumes the CompounderScore, it does not own one.

Two kinds of failure, and the difference decides whether a person gets an answer.

**Critical** — the ticker cannot be resolved to a company, or the score the brief
must explain does not exist afterwards. Preparation raises, because there is
nothing useful to hand back.

**Degraded** — an optional provider is unavailable, a pass fails for this one
company, or a stage finds nothing to do. Preparation records it on the stage,
carries on, and the brief says so through `DataFreshness`. A company whose
fundamentals are six months old and whose enrichment quota is exhausted is a
company worth reading about with those facts attached; refusing to produce
anything would be the less honest answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from api_clients import ProviderError
from data_access import (
    SINGLE_COVERAGE,
    BenchmarkPriceRepository,
    CompanyRepository,
    FilingExcerptRepository,
    FilingRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    ScoreSnapshotRepository,
)
from domain import CURRENT_SCORE_VERSION, normalise_ticker
from stock_screener.scanning import (
    update_benchmark,
    update_filing_text,
    update_filings,
    update_fundamentals,
    update_market_data,
)
from stock_screener.scoring import score_market

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import Session

    from api_clients import FundamentalsProvider, MarketDataProvider
    from data_access import Company
    from stock_screener.config import Settings
    from stock_screener.scanning import IngestionReport

log = structlog.get_logger(__name__)

BENCHMARK_STALENESS_DAYS = 5
"""How far behind the benchmark may fall before preparation refreshes it.

The benchmark is one shared series, not a property of the company being
prepared, so re-fetching it on every single-stock run would spend a request to
learn nothing most of the time. Five days clears a long weekend: a series
current to the last session is reused, one that has missed a week is not.
"""


class PreparationError(RuntimeError):
    """A stage failed in a way that leaves nothing worth returning.

    Raised only for the two critical cases: a ticker that cannot be resolved to a
    company, and a company with no score to explain once scoring has run. Every
    other failure is recorded on its stage and the run continues.
    """


class Stage(StrEnum):
    """The preparation stages, in the order they must run.

    The order is not a preference. Metrics are computed from stored prices and
    stored statements, so both have to land before scoring; scoring writes the
    snapshot the brief is bounded by, so it has to land before assembly; and the
    benchmark supplies relative strength, without which every company scores
    `INSUFFICIENT_DATA`.
    """

    PROFILE = "profile"
    """Resolve the ticker to a company row, adding it if the provider knows it."""

    MARKET_DATA = "market_data"
    """Daily price history — liquidity, momentum and the 52-week position."""

    FUNDAMENTALS = "fundamentals"
    """Company profile and quarterly statements."""

    BENCHMARK = "benchmark"
    """The broad-market series relative strength is measured against."""

    SCORE = "score"
    """CompounderScore under the current version, persisted as an ordinary snapshot."""

    FILINGS = "filings"
    """The SEC filing index — `D.` evidence."""

    FILING_TEXT = "filing_text"
    """Text extracted from those filings — `X.` evidence."""


class StageState(StrEnum):
    """What one stage did."""

    REFRESHED = "REFRESHED"
    """Fetched and stored something new."""

    REUSED = "REUSED"
    """Already current; nothing fetched and nothing written."""

    DEGRADED = "DEGRADED"
    """Could not complete. The stored data stands, and the brief says so."""

    SKIPPED = "SKIPPED"
    """Not attempted, because it was not needed on this run."""


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """What one preparation stage did, and what it cost.

    Attributes:
        stage: Which stage.
        state: What happened.
        detail: A human-readable account, shown in the CLI summary and carried
            into `DataFreshness.stale` when the state is `DEGRADED`.
    """

    stage: Stage
    state: StageState
    detail: str = ""

    @property
    def degraded(self) -> bool:
        """Whether this stage left the brief worse than it should be."""
        return self.state is StageState.DEGRADED

    def line(self) -> str:
        """Return the stage and its outcome as one readable line."""
        suffix = f" — {self.detail}" if self.detail else ""
        return f"{self.stage.value}: {self.state.value.lower()}{suffix}"


@dataclass(frozen=True, slots=True)
class PreparationResult:
    """What a single-stock preparation run did, stage by stage.

    The record of the network half. It carries no evidence itself — the evidence
    went into the database, and the brief is read back out of it — which is the
    same rule Phase 5 applies to jobs: a run record answers "what happened", and
    the thing it produced is read where that thing lives.

    Attributes:
        ticker: The symbol prepared, normalised.
        company_id: The resolved company row.
        started_at: When the run began, as an aware UTC datetime.
        finished_at: When it ended.
        outcomes: One entry per stage, in execution order.
    """

    ticker: str
    company_id: int
    started_at: datetime
    finished_at: datetime
    outcomes: tuple[StageOutcome, ...] = field(default_factory=tuple)

    def outcome(self, stage: Stage) -> StageOutcome | None:
        """Return the outcome for one stage, or None when it did not run."""
        return next((found for found in self.outcomes if found.stage is stage), None)

    def states(self, *states: StageState) -> tuple[str, ...]:
        """Return the names of stages that ended in any of these states."""
        return tuple(
            found.stage.value for found in self.outcomes if found.state in frozenset(states)
        )

    @property
    def degraded(self) -> tuple[StageOutcome, ...]:
        """Every stage that could not complete."""
        return tuple(found for found in self.outcomes if found.degraded)

    def summary(self) -> str:
        """Return a one-line human-readable summary."""
        counts: dict[str, int] = {}
        for found in self.outcomes:
            counts[found.state.value.lower()] = counts.get(found.state.value.lower(), 0) + 1
        rendered = " ".join(f"{state}={count}" for state, count in sorted(counts.items()))
        return f"ticker={self.ticker} {rendered}"


def _state_for(report: IngestionReport, *, wrote: bool) -> tuple[StageState, str]:
    """Read an ingestion report as a stage state.

    A pass that failed for this company is degraded; one the provider's plan
    would not serve is degraded too, and says so differently because the fix is
    a billing decision rather than a retry; one that succeeded without writing
    was already current; one that wrote fetched something new. All of it comes
    from the counts the existing passes already return.
    """
    if report.failures:
        return StageState.DEGRADED, f"provider failed for {', '.join(report.failures)}"
    if report.blocked_tickers:
        return (
            StageState.DEGRADED,
            f"the provider subscription does not cover {', '.join(report.blocked_tickers)}",
        )
    if wrote:
        return StageState.REFRESHED, report.summary()
    return StageState.REUSED, report.summary()


def prepare_company(
    session: Session,
    settings: Settings,
    market_data: MarketDataProvider,
    fundamentals: FundamentalsProvider,
    ticker: str,
    *,
    today: date | None = None,
    score_version: str = CURRENT_SCORE_VERSION,
) -> PreparationResult:
    """Refresh one company end to end, leaving it ready for brief assembly.

    Runs the existing passes for a single symbol in dependency order: prices and
    statements first, the benchmark if it has fallen behind, then the score they
    feed, then the SEC index and the text behind it. Every one of them is the
    pass the CLI already runs; none of them is reimplemented here.

    Args:
        session: Open database session. The caller commits.
        settings: Supplies history windows, thresholds and the benchmark symbol.
        market_data: Source of price bars, and of the profile for a company not
            yet stored.
        fundamentals: Source of statements, the filing index and filing text.
        ticker: The single symbol to prepare.
        today: Treat this as the current date. Injected so tests do not depend on
            when they run.
        score_version: The formula version whose snapshot the brief will explain.

    Returns:
        What each stage did, in execution order.

    Raises:
        PreparationError: If the ticker cannot be resolved to a company, or if no
            score snapshot exists once scoring has run. Both mean there is no
            brief to assemble, so returning a degraded result would be
            pretending otherwise.
    """
    symbol = normalise_ticker(ticker)
    started_at = datetime.now(UTC)
    outcomes: list[StageOutcome] = []

    company, resolution = _resolve(session, fundamentals, symbol)
    outcomes.append(resolution)
    company_id = company.id

    outcomes.append(_refresh_market_data(session, settings, market_data, symbol, today))
    outcomes.append(_refresh_fundamentals(session, settings, fundamentals, symbol))
    outcomes.append(_refresh_benchmark(session, settings, market_data, today))
    outcomes.append(_run_score(session, settings, symbol, today, score_version, company_id))
    outcomes.append(_refresh_filings(session, fundamentals, symbol))
    outcomes.append(_refresh_filing_text(session, fundamentals, symbol))

    result = PreparationResult(
        ticker=symbol,
        company_id=company_id,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        outcomes=tuple(outcomes),
    )
    log.info("deep research preparation complete", summary=result.summary())
    return result


def _resolve(
    session: Session, provider: FundamentalsProvider, symbol: str
) -> tuple[Company, StageOutcome]:
    """Return the company row for a symbol, adding it when the provider knows it.

    A ticker absent from the stored universe is the one case where a per-stock
    run has to reach past the passes the scanner uses: `update_fundamentals` and
    friends all iterate stored companies, so an unknown symbol would silently do
    nothing at every stage and produce an empty brief.

    Rather than inventing symbol discovery, this asks the fundamentals provider
    for the one profile — the same `get_company_profile` call
    `update_fundamentals` already makes per company — and stores it through the
    same repository. A market-wide `update-universe` is never triggered.
    """
    companies = CompanyRepository(session)
    stored = companies.get_by_ticker(symbol)
    if stored is not None:
        return stored, StageOutcome(Stage.PROFILE, StageState.REUSED, "already in the universe")

    try:
        profile = provider.get_company_profile(symbol)
    except ProviderError as error:
        raise PreparationError(
            f"{symbol} is not in the stored universe and the provider could not be "
            f"reached to resolve it: {error}"
        ) from error

    if profile is None:
        raise PreparationError(
            f"{symbol} is not in the stored universe and the fundamentals provider does "
            f"not recognise it. Run `update-universe` if it should be there."
        )

    companies.upsert_profile(profile)
    session.flush()
    added = companies.get_by_ticker(symbol)
    if added is None:  # pragma: no cover — the upsert either wrote or raised
        raise PreparationError(f"{symbol} could not be stored after the provider supplied it")

    return added, StageOutcome(Stage.PROFILE, StageState.REFRESHED, "added from the provider")


def _refresh_market_data(
    session: Session,
    settings: Settings,
    provider: MarketDataProvider,
    symbol: str,
    today: date | None,
) -> StageOutcome:
    """Refresh one company's price history, degrading rather than raising."""
    try:
        report = update_market_data(session, provider, settings, tickers=[symbol], today=today)
    except ProviderError as error:
        return StageOutcome(Stage.MARKET_DATA, StageState.DEGRADED, str(error))
    state, detail = _state_for(report, wrote=report.rows_written > 0)
    return StageOutcome(Stage.MARKET_DATA, state, detail)


def _refresh_fundamentals(
    session: Session, settings: Settings, provider: FundamentalsProvider, symbol: str
) -> StageOutcome:
    """Refresh one company's statements, degrading rather than raising.

    A provider failure here is explicitly not fatal. EDGAR is the baseline and
    the optional metered provider sits behind the composite adapter, so a
    quota-exhausted vendor must not stop a company that already has years of
    filed statements from being researched.
    """
    try:
        report = update_fundamentals(session, provider, settings, tickers=[symbol])
    except ProviderError as error:
        return StageOutcome(Stage.FUNDAMENTALS, StageState.DEGRADED, str(error))
    state, detail = _state_for(report, wrote=report.rows_written > 0)
    return StageOutcome(Stage.FUNDAMENTALS, state, detail)


def _refresh_benchmark(
    session: Session, settings: Settings, provider: MarketDataProvider, today: date | None
) -> StageOutcome:
    """Refresh the benchmark only when it has fallen behind.

    Relative strength is the company's return less this series', so a missing
    benchmark makes every company `INSUFFICIENT_DATA`. It is also one shared
    series rather than anything to do with the company being prepared, which is
    why a current one is reused instead of re-fetched on every single-stock run.
    """
    symbol = normalise_ticker(settings.benchmark_symbol)
    as_of = today or datetime.now(UTC).date()
    stored = BenchmarkPriceRepository(session).latest_date(symbol)

    if stored is not None and (as_of - stored).days <= BENCHMARK_STALENESS_DAYS:
        return StageOutcome(
            Stage.BENCHMARK, StageState.SKIPPED, f"{symbol} current to {stored.isoformat()}"
        )

    try:
        report = update_benchmark(session, provider, settings, today=today)
    except ProviderError as error:
        return StageOutcome(Stage.BENCHMARK, StageState.DEGRADED, str(error))
    state, detail = _state_for(report, wrote=report.rows_written > 0)
    return StageOutcome(Stage.BENCHMARK, state, detail)


def _run_score(
    session: Session,
    settings: Settings,
    symbol: str,
    today: date | None,
    score_version: str,
    company_id: int,
) -> StageOutcome:
    """Score the company with the existing engine and persist the snapshot.

    Dated by the most recent price the company has, because that is the day the
    score actually describes, and recorded as a single-company run so the
    market-wide ranking views never mistake it for the newest ranking.

    `score_market` narrowed to one ticker, with `persist=True`, so the row it
    writes is indistinguishable from one a nightly run would write. Deep research
    has no scoring code of its own and no second score version.

    A status other than `SCORED` is not a failure here. An ineligible company, a
    bank, or one with two quarters of history gets a snapshot carrying the status
    that says why, and the brief explains that instead of a number.
    """
    # Dated by the data, not the clock, and marked as a single-company run.
    #
    # Both matter, and for different reasons. A score dated today while resting
    # on Friday's prices claims a currency it does not have. And a per-ticker
    # snapshot that looked like a market run would become "the latest ranking" —
    # one company, over a universe of thousands — which is exactly what one
    # `deep-research run` did to the dashboard before this.
    as_of = today or PriceHistoryRepository(session).latest_date(company_id) or _today()
    run = score_market(
        session,
        settings,
        tickers=[symbol],
        score_date=as_of,
        persist=True,
        coverage=SINGLE_COVERAGE,
    )
    session.flush()

    snapshot = ScoreSnapshotRepository(session).latest_for_company(
        company_id, score_version=score_version
    )
    if snapshot is None:
        raise PreparationError(
            f"{symbol} has no {score_version} snapshot after scoring, so there is no "
            f"score for a brief to explain"
        )

    return StageOutcome(
        Stage.SCORE,
        StageState.REFRESHED,
        f"{snapshot.scoring_status} on {snapshot.score_date.isoformat()} ({run.summary()})",
    )


def _refresh_filings(session: Session, provider: FundamentalsProvider, symbol: str) -> StageOutcome:
    """Refresh the SEC filing index for one company."""
    try:
        report = update_filings(session, provider, tickers=[symbol])
    except ProviderError as error:
        return StageOutcome(Stage.FILINGS, StageState.DEGRADED, str(error))
    state, detail = _state_for(report, wrote=report.rows_written > 0)
    return StageOutcome(Stage.FILINGS, state, detail)


def _refresh_filing_text(
    session: Session, provider: FundamentalsProvider, symbol: str
) -> StageOutcome:
    """Extract text from the filings worth reading, for one company.

    The same pass `research run --prepare` uses, so `X.` grounding is produced by
    one extractor and one selection rule. A company with no readable filing is
    degraded rather than fatal: the sections resting on filing text answer
    `UNKNOWN`, which is the honest outcome.
    """
    try:
        report = update_filing_text(session, provider, tickers=[symbol])
    except ProviderError as error:
        return StageOutcome(Stage.FILING_TEXT, StageState.DEGRADED, str(error))
    state, detail = _state_for(report, wrote=report.rows_written > 0)
    return StageOutcome(Stage.FILING_TEXT, state, detail)


def stored_freshness_dates(
    session: Session, company_id: int
) -> tuple[date | None, date | None, date | None, date | None]:
    """Return the dates the stored evidence actually reaches, newest first per kind.

    Read after preparation so the brief reports what is stored rather than what
    was requested. A pass that degraded leaves the previous date standing, which
    is exactly the signal a reader needs.

    Args:
        session: Open database session.
        company_id: The company to read.

    Returns:
        `(price_as_of, fundamentals_through, filings_through, excerpts_through)`,
        each None when nothing of that kind is stored.
    """
    price_as_of = PriceHistoryRepository(session).latest_date(company_id)
    fundamentals_through = FinancialSnapshotRepository(session).latest_period_end(company_id)
    filings_through = FilingRepository(session).latest_filed(company_id)

    excerpts = FilingExcerptRepository(session).list_for_company(company_id)
    excerpts_through = max((row.filed for row in excerpts), default=None)

    return price_as_of, fundamentals_through, filings_through, excerpts_through


def _today() -> date:
    """Return today in UTC, for a company with no stored price at all."""
    return datetime.now(UTC).date()
