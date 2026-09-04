"""Reconstructing a historical security that no longer exists in `companies`.

The company reconstruction in `stock_screener.backtest.scores` reads a live
`companies` row and its point-in-time tables. This does the same for a security
that has left the market and therefore has no such row — an acquired, bankrupt or
wound-up filer whose record survives only under its CIK.

Every calculation is the frozen one. Fundamentals come from the append-only
`historical_observations` record, selected on `filed_date`; the market
capitalisation is the filed share count times the price recovered from the stored
close with the split factors since; eligibility is `evaluate_eligibility`; the
score is `score_company`. Nothing here reimplements a metric or a rule — it reads
a different set of tables and hands the result to the same domain functions the
live path uses, which is what keeps a delisted security's score identical to the
one it would have earned while listed.

Two properties make this survivorship-honest:

- **It never reads a `companies` row.** The identity is the CIK, the storage is
  the parallel historical tables, and today's company state cannot reach the
  result because it is never consulted.
- **Activity is decided by the date, not by today.** A security is eligible only
  on a date at or before its last trading session; asked about a later date it is
  inactive, exactly as it was. The live path's use of a current `is_active` flag
  — a look-ahead for a delisted name — is closed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from api_clients import periods_from_observations
from data_access import (
    HistoricalCorporateActionRepository,
    HistoricalObservationRepository,
    HistoricalPriceRepository,
    HistoricalSecurityRepository,
    to_corporate_action,
    to_filing_observation,
    to_price_bar,
)
from domain import (
    DEFAULT_LIQUIDITY_WINDOW,
    CompanyProfile,
    CompanyScore,
    EligibilityWarning,
    Freshness,
    ScoringStatus,
    StatementProfile,
    VolumeBasis,
    as_traded_price,
    build_company_metrics,
    evaluate_eligibility,
    latest_close,
    score_company,
)
from stock_screener.backtest.scores import benchmark_returns_as_of
from stock_screener.pit.metrics import PointInTimeMetrics

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from data_access.models import HistoricalObservation, HistoricalSecurity
    from domain import CompanyMetrics, FxConversion
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)

#: The cover-page share count's XBRL concept — the figure a point-in-time market
#: capitalisation is calculated from, dated by its own "as of" instant.
_COVER_PAGE_SHARES = "EntityCommonStockSharesOutstanding"


@dataclass(frozen=True)
class HistoricalReconstruction:
    """A delisted security's CompounderScore rebuilt from the free record.

    Attributes:
        as_of: The decision date reconstructed. Nothing filed or traded after it
            contributed.
        historical_security_id: The stored security's primary key — the identity.
        cik: Its SEC filer number, where the record establishes one. None for a
            security recovered from identifier evidence alone, which is the
            majority of delisted symbols; see `domain.identity`.
        ticker: The trading symbol as of the record.
        score: The score, carrying the status that explains an absent number.
        metrics: The point-in-time metrics behind it, or None when nothing had
            been filed by the date.
        eligible: Whether it passed the Phase 1 screen on the date.
        freshness: Whether the fundamentals were current on the date.
        status: Why the security left the market — a `DelistingReason` value.
        terminal_value_per_share: What a holder received per share, when a source
            states it. None is UNKNOWN, never a guessed zero.
        observations_available: How many observations were public by the date.
        periods_used: How many reported periods the metrics rested on.
    """

    as_of: date
    historical_security_id: int
    cik: int | None
    ticker: str
    score: CompanyScore
    metrics: CompanyMetrics | None
    eligible: bool
    freshness: Freshness
    status: str
    terminal_value_per_share: float | None
    observations_available: int
    periods_used: int


def _historical_profile(security: HistoricalSecurity, *, active: bool) -> CompanyProfile:
    """Build a domain profile from a stored historical security.

    The market capitalisation is suppressed — a delisted name has no live quote,
    and the point-in-time figure is rebuilt from shares and price instead. The
    statement profile is carried through so a delisted bank is declined the score
    exactly as a live one is.
    """
    return CompanyProfile(
        ticker=security.primary_ticker,
        name=security.name,
        exchange=security.exchange,
        sector=security.sector,
        industry=security.industry,
        market_cap=None,
        reporting_currency=security.reporting_currency,
        quote_currency=security.quote_currency,
        statement_profile=(
            StatementProfile(security.statement_profile) if security.statement_profile else None
        ),
        is_active=active,
    )


def _share_count_date(observations: Sequence[HistoricalObservation]) -> date | None:
    """Return the instant the newest available cover-page share count describes.

    Its own `period_end`, not the filing's date: a count stated as of 17 July and
    filed on 31 July is a statement about 17 July, and a split between the two is
    not in it.
    """
    dated = [
        obs
        for obs in observations
        if obs.concept == _COVER_PAGE_SHARES and obs.period_end is not None
    ]
    if not dated:
        return None
    return max(dated, key=lambda obs: (obs.period_end, obs.filed_date or obs.period_end)).period_end


def build_historical_metrics_as_of(
    session: Session,
    historical_security_id: int,
    as_of: date,
    *,
    liquidity_window: int = DEFAULT_LIQUIDITY_WINDOW,
    bar_volume_basis: VolumeBasis = VolumeBasis.UNKNOWN,
    fx: FxConversion | None = None,
) -> PointInTimeMetrics:
    """Build a historical security's metrics from what was public on a date.

    Fundamentals come from observations filed on or before `as_of`; price bars are
    read with the same cutoff. The market capitalisation is point-in-time on both
    sides — the filed share count times the price the security actually traded at,
    recovered from the stored close with the split factors since.

    Args:
        session: Open database session.
        historical_security_id: The stored security's primary key.
        as_of: The decision date.
        liquidity_window: Sessions to average dollar volume over.
        bar_volume_basis: What the bars' volume represents.
        fx: Rate converting quote to reporting currency, dated no later than
            `as_of`. None leaves currency-sensitive ratios unavailable.

    Returns:
        The metrics and the evidence behind them. `metrics` is None when nothing
        had been filed by `as_of`.
    """
    security = HistoricalSecurityRepository(session).get(historical_security_id)
    if security is None:
        return PointInTimeMetrics(as_of=as_of, metrics=None)

    active = security.last_trading_date is None or as_of <= security.last_trading_date
    profile = _historical_profile(security, active=active)

    observations = HistoricalObservationRepository(session).available_as_of(
        historical_security_id, as_of
    )
    periods = periods_from_observations(to_filing_observation(row) for row in observations)
    if not periods:
        return PointInTimeMetrics(
            as_of=as_of, metrics=None, observations_available=len(observations)
        )

    bars = [
        to_price_bar(row)
        for row in HistoricalPriceRepository(session).list_for_security(
            historical_security_id, until=as_of
        )
    ]
    # Every action, including those after `as_of`: a stored close is adjusted for
    # a later split, and undoing that needs the later factor. A split is applied
    # to a price, never read as knowledge held on the date.
    actions = [
        to_corporate_action(row)
        for row in HistoricalCorporateActionRepository(session).for_security(historical_security_id)
    ]
    share_basis_close = as_traded_price(
        latest_close(bars), actions, _share_count_date(observations) or as_of
    )

    metrics = build_company_metrics(
        profile,
        periods,
        bars,
        liquidity_window=liquidity_window,
        bar_volume_basis=bar_volume_basis,
        fx=fx,
        share_basis_close=share_basis_close,
    )
    return PointInTimeMetrics(
        as_of=as_of,
        metrics=metrics,
        periods=tuple(periods),
        observations_available=len(observations),
    )


def reconstruct_historical_score(
    session: Session,
    settings: Settings,
    historical_security_id: int,
    as_of: date,
    *,
    fx: FxConversion | None = None,
) -> HistoricalReconstruction | None:
    """Rebuild a delisted security's CompounderScore as of a past date.

    Every input is bounded to the date and every calculation is the frozen one,
    read from the historical tables rather than from `companies`. The result is
    the score the frozen formula would have produced for the security on the date
    — computed without any reference to today's company state, because a delisted
    security has none.

    Args:
        session: Open database session.
        settings: Supplies the eligibility thresholds, the benchmark symbol and
            the bars' volume basis.
        historical_security_id: The security to reconstruct.
        as_of: The decision date.
        fx: Rate converting quote to reporting currency, dated no later than
            `as_of`.

    Returns:
        The reconstruction, or None when the security does not exist.
    """
    security = HistoricalSecurityRepository(session).get(historical_security_id)
    if security is None:
        log.warning("no such historical security", historical_security_id=historical_security_id)
        return None

    active = security.last_trading_date is None or as_of <= security.last_trading_date
    profile = _historical_profile(security, active=active)

    pit = build_historical_metrics_as_of(
        session,
        historical_security_id,
        as_of,
        bar_volume_basis=settings.bar_volume_basis,
        fx=fx,
    )
    if pit.metrics is None:
        return HistoricalReconstruction(
            as_of=as_of,
            historical_security_id=historical_security_id,
            cik=security.cik,
            ticker=security.primary_ticker,
            score=CompanyScore(
                ticker=security.primary_ticker, status=ScoringStatus.INSUFFICIENT_DATA
            ),
            metrics=None,
            eligible=False,
            freshness=Freshness.CURRENT,
            status=security.status,
            terminal_value_per_share=security.terminal_value_per_share,
            observations_available=pit.observations_available,
            periods_used=0,
        )

    eligibility = evaluate_eligibility(
        profile, pit.metrics, settings.eligibility_thresholds, today=as_of
    )
    benchmark = benchmark_returns_as_of(session, settings.benchmark_symbol, as_of)
    score = score_company(profile, pit.metrics, benchmark, eligible=eligibility.eligible)
    freshness = (
        Freshness.STALE
        if EligibilityWarning.STALE_FUNDAMENTALS in eligibility.warnings
        else Freshness.CURRENT
    )
    return HistoricalReconstruction(
        as_of=as_of,
        historical_security_id=historical_security_id,
        cik=security.cik,
        ticker=security.primary_ticker,
        score=score,
        metrics=pit.metrics,
        eligible=eligibility.eligible,
        freshness=freshness,
        status=security.status,
        terminal_value_per_share=security.terminal_value_per_share,
        observations_available=pit.observations_available,
        periods_used=len(pit.periods),
    )
