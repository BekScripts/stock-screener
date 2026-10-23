"""Reconstructing the CompounderScore as it would have stood on a past date.

The score history in `pit_score_snapshots` begins the day capture was first run
and grows one day at a time. That is the right record of what the system *said*,
but it cannot answer a question about a date before capture existed. This module
answers the other version of the question — what the frozen formula *would* have
produced on a past date, given only what had been filed and priced by then.

It is possible at all because of a property established by reading the scorer:
`domain.score_company` is a pure function of one company's own inputs — its
profile, its metrics, and the broad-market return that is the same for every
company on a date. There is no cross-sectional term, no universe percentile, no
peer rank. So a single company's past score can be rebuilt from a single
company's past data, with no need to reconstruct the whole market first.

Nothing here is a new formula. The metrics come from `build_metrics_as_of`, which
reuses the provider's normalisation and the frozen arithmetic unchanged; the
eligibility verdict comes from the frozen `evaluate_eligibility`; the score comes
from the frozen `score_company`. This module only bounds every input to the date
and hands them to code that already exists — which is exactly why it changes no
number and mints no version.

The one honest gap: a company's sector, activity flag and statement profile are
read from its current row, because the project keeps no history of them. A
reclassification since the date would therefore be applied retroactively. It is
recorded in the result rather than hidden, and it is the reason a reconstructed
score is labelled a reconstruction and not a capture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    to_company_profile,
    to_price_bar,
)
from domain import (
    BenchmarkReturns,
    CompanyScore,
    EligibilityWarning,
    Freshness,
    ScoringStatus,
    evaluate_eligibility,
    return_6m,
    return_12m,
    score_company,
)
from stock_screener.pit.decision_time import DecisionPoint
from stock_screener.pit.metrics import build_metrics_as_of

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import Session

    from domain import CompanyMetrics, FxConversion, PriceBar
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ReconstructedScore:
    """A CompounderScore rebuilt from what was knowable on a past date.

    Attributes:
        as_of: The decision date the score describes. Nothing filed or traded
            after it contributed.
        company_id: The scored company's primary key.
        ticker: Its symbol as of the current row.
        score: The score itself, carrying the status that explains an absent
            number exactly as a live score would.
        metrics: The point-in-time metrics the score was built from, or None
            when nothing had been filed by the date.
        eligible: Whether the security passed the Phase 1 screen on the date.
        freshness: Whether the fundamentals behind it were current on the date,
            under the same cadence-aware bound the live screen applies.
        observations_available: How many filing observations were public by the
            date. Zero means the reconstruction had nothing to work from.
        periods_used: How many reported periods the metrics rested on.
        classification_is_current: True always, and named to keep the caller
            aware that sector, activity and statement profile were read from the
            company's present row rather than from a history the project lacks.
    """

    as_of: date
    company_id: int
    ticker: str
    score: CompanyScore
    metrics: CompanyMetrics | None
    eligible: bool
    freshness: Freshness
    observations_available: int
    periods_used: int
    classification_is_current: bool = True


def benchmark_returns_as_of(session: Session, symbol: str, as_of: date) -> BenchmarkReturns:
    """Return the benchmark's six- and twelve-month returns as of a date.

    The live path reads every stored bar; this one drops the bars after `as_of`
    first, so relative strength is measured against the market as it stood on the
    date rather than as it stands now. The frozen `return_6m` / `return_12m` then
    anchor on the newest remaining bar, which is the date's own session.

    Args:
        session: Open database session.
        symbol: The benchmark's ticker.
        as_of: The decision date.

    Returns:
        The returns, each None where the bounded history does not reach back far
        enough — which correctly leaves early dates without a benchmark rather
        than borrowing a later one's.
    """
    bars: list[PriceBar] = [
        to_price_bar(row)
        for row in BenchmarkPriceRepository(session).list_bars(symbol)
        if row.date <= as_of
    ]
    return BenchmarkReturns(
        symbol=symbol,
        return_6m=return_6m(bars),
        return_12m=return_12m(bars),
    )


def reconstruct_score(
    session: Session,
    settings: Settings,
    company_id: int,
    as_of: date,
    *,
    fx: FxConversion | None = None,
    decision: DecisionPoint = DecisionPoint.CLOSE,
    benchmark: BenchmarkReturns | None = None,
) -> ReconstructedScore | None:
    """Rebuild one company's CompounderScore as of a past date from PIT inputs.

    Every input is bounded to the date and every calculation is the frozen one.
    The result is the score the system would have produced on the date, not a
    record of what it did produce — the two agree when a capture also ran, and
    only this one exists for a date before capture began.

    Args:
        session: Open database session.
        settings: Supplies the eligibility thresholds, the benchmark symbol and
            the bars' volume basis.
        company_id: The company to reconstruct.
        as_of: The decision date.
        fx: Rate converting quote to reporting currency for a foreign filer,
            dated no later than `as_of`. None leaves currency-sensitive ratios
            unavailable, exactly as the live path degrades when no rate resolves.
        decision: Where in the day the decision sits. Defaults to the close.
        benchmark: The market's returns as of the date, when the caller has
            already measured them. The broad-market return is the one input
            every company on a date shares, so a sweep over the whole roster
            measures it once instead of re-reading the benchmark series per
            company. Computed here when not given, from the same function.

    Returns:
        The reconstruction, or None when the company does not exist.
    """
    company = CompanyRepository(session).get(company_id)
    if company is None:
        log.warning("no such company to reconstruct", company_id=company_id)
        return None

    # Classification is current; the market cap on the profile is suppressed so
    # nothing downstream can read a present valuation for a past date. The
    # point-in-time market cap is rebuilt inside `build_metrics_as_of`.
    profile = to_company_profile(company).model_copy(update={"market_cap": None})

    pit = build_metrics_as_of(
        session,
        company_id,
        as_of,
        bar_volume_basis=settings.bar_volume_basis,
        fx=fx,
        decision=decision,
    )
    if pit.metrics is None:
        return ReconstructedScore(
            as_of=as_of,
            company_id=company_id,
            ticker=company.ticker,
            score=CompanyScore(ticker=company.ticker, status=ScoringStatus.INSUFFICIENT_DATA),
            metrics=None,
            eligible=False,
            freshness=Freshness.CURRENT,
            observations_available=pit.observations_available,
            periods_used=0,
        )

    eligibility = evaluate_eligibility(
        profile, pit.metrics, settings.eligibility_thresholds, today=as_of
    )
    market = benchmark or benchmark_returns_as_of(session, settings.benchmark_symbol, as_of)
    score = score_company(profile, pit.metrics, market, eligible=eligibility.eligible)
    freshness = (
        Freshness.STALE
        if EligibilityWarning.STALE_FUNDAMENTALS in eligibility.warnings
        else Freshness.CURRENT
    )
    return ReconstructedScore(
        as_of=as_of,
        company_id=company_id,
        ticker=company.ticker,
        score=score,
        metrics=pit.metrics,
        eligible=eligibility.eligible,
        freshness=freshness,
        observations_available=pit.observations_available,
        periods_used=len(pit.periods),
    )
