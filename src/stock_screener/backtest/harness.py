"""The backtest-readiness harness: can this date be studied honestly?

This is the integration point. It does not backtest, rank, threshold or
recommend — it answers, for one security on one date, the seven questions a
survivorship-aware study has to have answered before it may run a single bar:

1. Was the security in the investable universe on the date?
2. What fundamentals had been filed by the date?
3. What market capitalisation was knowable on the date?
4. What CompounderScore existed — captured, or reconstructable — on the date?
5. What market signals had been derived for the date?
6. What did the security do over the next 1, 5, 20 and 60 sessions?
7. If it left the market, how — and is that fate recorded rather than guessed?

Every answer is assembled from parts that already exist and are already tested:
the universe reconstruction, the point-in-time score reconstruction, the frozen
forward-return measurement, the immutable signal record and the delisting
registry. The harness's own contribution is to gather them behind one call and,
crucially, to record the *gaps* — the places where the record cannot answer, so
a study reads a stated absence instead of a confident wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    DelistingRepository,
    PitScoreSnapshotRepository,
    PitSignalSnapshotRepository,
    PriceHistoryRepository,
    to_price_bar,
)
from domain import (
    CURRENT_SCORE_VERSION,
    CURRENT_SIGNAL_VERSION,
    FORWARD_HORIZONS,
    CorporateActionKind,
    DelistingReason,
    OutcomeStatus,
    ScoringStatus,
    classify_delisting,
    forward_returns,
)
from stock_screener.backtest.scores import ReconstructedScore, reconstruct_score
from stock_screener.backtest.universe import reconstruct_universe

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import Session

    from domain import FxConversion, UniverseAsOf
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ForwardOutcome:
    """What a security did over one horizon, or why there is no number."""

    horizon_sessions: int
    status: OutcomeStatus
    total_return: float | None


@dataclass(frozen=True)
class DelistingFate:
    """What became of a security that left the market.

    Attributes:
        ticker: The security that ceased.
        reason: The fate a study reasons about — acquired, bankrupt, delisted,
            or `UNKNOWN` where the source does not establish one.
        kind: The settlement mechanism the source reported, verbatim.
        effective_date: Its last day as a listed security.
        cash_per_share: What a holder received per share, where cash was paid.
    """

    ticker: str
    reason: DelistingReason
    kind: str
    effective_date: date
    cash_per_share: float | None


@dataclass(frozen=True)
class SecurityReadiness:
    """Everything the record can and cannot say about one security on one date.

    The fields answer the seven questions in order; `gaps` collects every place
    an answer is absent, so an overall verdict can be read off one list rather
    than inferred from scattered Nones.
    """

    as_of: date
    ticker: str
    company_id: int | None
    in_universe: bool
    membership_kind: str | None
    fundamentals_known: bool
    periods_used: int
    market_cap: float | None
    score_status: ScoringStatus | None
    final_score: float | None
    score_is_reconstructed: bool
    score_was_captured: bool
    freshness: str | None
    signals_present: bool
    forward_outcomes: tuple[ForwardOutcome, ...]
    delisting: DelistingFate | None
    gaps: tuple[str, ...] = field(default_factory=tuple)

    @property
    def answerable(self) -> bool:
        """True when the date can be studied for this security without a guess.

        A security that left the market is still answerable — its fate is a
        recorded fact, and a study that handles delistings wants exactly that
        row. What is not answerable is a security the record cannot place, price
        or score without inventing something.
        """
        return not self.gaps


def _delisting_fate(
    session: Session, ticker: str, *, still_trading_after: date | None = None
) -> DelistingFate | None:
    """Return the recorded fate of a ticker, or None if it never left.

    Args:
        session: Open database session.
        ticker: The symbol to look up.
        still_trading_after: The last session this ticker's *current* holder was
            priced on, when there is one. A fate dated before that belongs to an
            earlier holder of the symbol, not to the security being judged:
            `CZR` left the market in July 2020 as Caesars Entertainment
            Corporation, and the `CZR` priced in 2024 is the survivor that took
            the letters. Attaching the first one's fate to the second would tell
            a study a live company had been acquired four years ago.

    Returns:
        The fate, or None where the security never left or the recorded exit
        belongs to a different issuer.
    """
    registry = DelistingRepository(session).by_ticker([ticker])
    delisting = registry.get(ticker.upper())
    if delisting is None:
        return None
    if still_trading_after is not None and still_trading_after > delisting.effective_date:
        log.debug(
            "delisting belongs to an earlier holder of the symbol",
            ticker=ticker,
            effective_date=str(delisting.effective_date),
            last_bar=str(still_trading_after),
        )
        return None
    kind: CorporateActionKind | None
    try:
        kind = CorporateActionKind(delisting.kind)
    except ValueError:
        # A kind the domain does not model. The fate is then unknown rather than
        # forced onto one of the modelled reasons.
        kind = None
    return DelistingFate(
        ticker=delisting.ticker,
        reason=classify_delisting(kind),
        kind=delisting.kind,
        effective_date=delisting.effective_date,
        cash_per_share=delisting.cash_per_share,
    )


def _forward_outcomes(
    session: Session,
    settings: Settings,
    company_id: int,
    as_of: date,
) -> tuple[tuple[ForwardOutcome, ...], bool]:
    """Measure what a security did after the date, over the frozen horizons.

    Computed on the fly through the frozen `domain.forward_returns` rather than
    read from the store, so the harness answers whether the measure pass has run
    or not. The market's calendar is the benchmark's; a gap in the security's own
    bars against it is the security's, which is the whole point.

    Returns:
        The outcomes and whether a benchmark calendar existed to measure on.
    """
    benchmark_bars = BenchmarkPriceRepository(session).list_bars(settings.benchmark_symbol)
    calendar = [bar.date for bar in benchmark_bars]
    if not calendar:
        return (), False

    stored_bars = PriceHistoryRepository(session).list_for_company(company_id)
    bars = [to_price_bar(row) for row in stored_bars]
    opens = {bar.date: bar.open for bar in bars}
    closes = {bar.date: bar.close for bar in bars}
    measured = forward_returns(calendar, opens, closes, as_of, horizons=FORWARD_HORIZONS)
    return (
        tuple(
            ForwardOutcome(
                horizon_sessions=result.horizon_sessions,
                status=result.status,
                total_return=result.total_return,
            )
            for result in measured
        ),
        True,
    )


def _captured_score_exists(session: Session, company_id: int, as_of: date) -> bool:
    """Whether an immutable score snapshot was captured for the date."""
    captured = PitScoreSnapshotRepository(session).for_date(
        as_of, score_version=CURRENT_SCORE_VERSION
    )
    return any(row.company_id == company_id for row in captured)


def _signals_present(session: Session, company_id: int, as_of: date) -> bool:
    """Whether a market signal was derived for the security on the date."""
    derived = PitSignalSnapshotRepository(session).for_date(
        as_of, signal_version=CURRENT_SIGNAL_VERSION
    )
    return any(row.company_id == company_id for row in derived)


def security_readiness(
    session: Session,
    settings: Settings,
    as_of: date,
    ticker: str,
    *,
    universe: UniverseAsOf | None = None,
    fx: FxConversion | None = None,
) -> SecurityReadiness:
    """Answer the seven readiness questions for one security on one date.

    Args:
        session: Open database session.
        settings: Supplies thresholds, the benchmark symbol and the volume basis.
        as_of: The decision date to judge.
        ticker: The security to judge.
        universe: A universe already reconstructed for `as_of`, to avoid
            rebuilding it per security in a loop. Rebuilt when not given.
        fx: Rate for a foreign filer, dated no later than `as_of`.

    Returns:
        The gathered answer, with a `gaps` list naming every unanswerable part.
    """
    symbol = ticker.upper()
    resolved_universe = universe or reconstruct_universe(session, as_of)
    company = CompanyRepository(session).get_by_ticker(symbol)
    last_bar = (
        PriceHistoryRepository(session).latest_date(company.id) if company is not None else None
    )
    fate = _delisting_fate(session, symbol, still_trading_after=last_bar)
    gaps: list[str] = []

    if company is None:
        # No company row. The security is knowable only through the delisting
        # registry — which is exactly the survivorship case the harness exists
        # to keep visible rather than let vanish.
        if fate is None:
            gaps.append("NOT_IN_RECORD")
        return SecurityReadiness(
            as_of=as_of,
            ticker=symbol,
            company_id=None,
            in_universe=False,
            membership_kind=None,
            fundamentals_known=False,
            periods_used=0,
            market_cap=None,
            score_status=None,
            final_score=None,
            score_is_reconstructed=False,
            score_was_captured=False,
            freshness=None,
            signals_present=False,
            forward_outcomes=(),
            delisting=fate,
            gaps=tuple(gaps),
        )

    membership = next((m for m in resolved_universe.members if m.company_id == company.id), None)
    in_universe = membership is not None
    if company.id in resolved_universe.undetermined:
        gaps.append("UNIVERSE_MEMBERSHIP_UNDETERMINED")

    reconstructed: ReconstructedScore | None = reconstruct_score(
        session, settings, company.id, as_of, fx=fx
    )
    has_metrics = reconstructed is not None and reconstructed.metrics is not None
    if not has_metrics:
        gaps.append("NO_FUNDAMENTALS_FILED_BY_DATE")

    market_cap = (
        reconstructed.metrics.market_cap
        if reconstructed is not None and reconstructed.metrics is not None
        else None
    )
    if has_metrics and market_cap is None:
        gaps.append("NO_POINT_IN_TIME_MARKET_CAP")

    outcomes, had_calendar = _forward_outcomes(session, settings, company.id, as_of)
    if not had_calendar:
        gaps.append("NO_BENCHMARK_CALENDAR")

    signals = _signals_present(session, company.id, as_of)
    if not signals:
        gaps.append("NO_SIGNALS_DERIVED")

    score = reconstructed.score if reconstructed else None
    return SecurityReadiness(
        as_of=as_of,
        ticker=symbol,
        company_id=company.id,
        in_universe=in_universe,
        membership_kind=(membership.kind.value if membership else None),
        fundamentals_known=has_metrics,
        periods_used=reconstructed.periods_used if reconstructed else 0,
        market_cap=market_cap,
        score_status=score.status if score else None,
        final_score=score.final_score if score else None,
        score_is_reconstructed=reconstructed is not None,
        score_was_captured=_captured_score_exists(session, company.id, as_of),
        freshness=reconstructed.freshness.value if reconstructed else None,
        signals_present=signals,
        forward_outcomes=outcomes,
        delisting=fate,
        gaps=tuple(gaps),
    )
