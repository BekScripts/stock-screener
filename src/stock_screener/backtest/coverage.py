"""How much of the record is actually usable, date by date.

`security_readiness` answers the seven questions for *one* security on one date.
This answers them for the whole roster across many dates, and reports the answer
as a funnel: how many securities survive each stage between "exists in the
record" and "could be studied on this date without inventing anything".

The funnel is the deliverable, not the final number. A dataset that ends with
two thousand usable securities per date is a different dataset depending on
whether the other three thousand were lost to missing fundamentals, to an
unsupported sector, or to a universe the record cannot reconstruct — and only
the first of those improves by waiting.

Two rules govern what is counted.

**Universe membership is reported, never assumed.** The count of members comes
from `reconstruct_universe` and nothing else. Where that returns nothing because
every company's membership record begins after the date, the report says so and
counts the members as zero — it does not substitute a proxy and call it
membership. `listed_on_date` is offered beside it as an explicitly separate
diagnostic: securities that *traded* on the date according to the stored price
record. A traded security was certainly listed; that is a necessary condition
for membership and not a sufficient one, and the two are never added together.

**Absence is attributed, never averaged away.** Every security that fails a
stage is counted at the first stage it fails, so the stage totals sum to the
roster and the attrition percentages describe where the record actually runs
out.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import distinct, func, select

from data_access import (
    BenchmarkPrice,
    CompanyRepository,
    Delisting,
    ForwardReturnRecord,
    FundamentalObservation,
    HistoricalSecurity,
    PitSignalSnapshot,
    PriceHistory,
)
from domain import CURRENT_SIGNAL_VERSION, OutcomeStatus, ScoringStatus
from stock_screener.backtest.scores import benchmark_returns_as_of, reconstruct_score
from stock_screener.backtest.universe import reconstruct_universe

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from stock_screener.config import Settings

log = structlog.get_logger(__name__)

#: How many sessions of stored history a security needs before the year-long
#: feature windows can be filled. One trading year, the same figure
#: `domain.features` uses for its `_252` windows.
YEAR_SESSIONS = 252

#: How many sessions a security may be missing from the date and still count as
#: trading on it. Mirrors `domain.outcomes.DEFAULT_STALE_SESSIONS`: a run that
#: finished on Tuesday leaves every security short by two days.
LISTED_TOLERANCE_SESSIONS = 5


@dataclass(frozen=True)
class DateCoverage:
    """What one date can and cannot support, as a funnel over the roster.

    Attributes:
        as_of: The date described.
        in_record: Companies the database holds at all — the funnel's top.
        universe_members: Securities `reconstruct_universe` places in the
            investable set on the date. Zero where the membership record does
            not reach back this far, which is a stated absence and not an
            empty market.
        universe_observed: Members whose presence rests on a witnessed
            transition.
        universe_baseline: Members present on a baseline's authority only.
        universe_undetermined: Companies whose state on the date the record
            cannot decide.
        listed_on_date: Securities with a stored bar within a few sessions of
            the date. A separate diagnostic, never a substitute for membership.
        sufficient_price_history: Securities with a full trading year of bars
            by the date.
        fundamentals_available: Securities whose filings by the date produce
            point-in-time metrics.
        market_cap_available: Of those, the ones with a point-in-time market
            capitalisation.
        score_reconstructable: Securities the frozen formula scores on the date.
        signals_available: Securities with a derived market signal for the date.
        forward_returns_available: Securities with at least one realised forward
            return measured from the date.
        forward_returns_by_horizon: Realised measurements per horizon.
        recorded_exits: Securities the registry records as having left the
            market by the date, over the whole registry rather than over today's
            roster.
        terminal_outcomes_available: Of those, the ones a study could actually
            take an outcome from — a stated cash terminal value, or a
            reconstructed historical security with a price path.
        interrupted: Forward measurements refused because the security stopped
            trading inside the horizon.
        fully_usable: Securities that clear every *data* stage — listed, priced
            a year deep, filed by the date, valued, scored, signalled and
            measured forward. Universe membership is deliberately **not** one of
            them: it is reported beside this number rather than multiplied into
            it, because a universe the record cannot reconstruct would drive
            this to zero on every date and hide which data gaps are real. A
            study must read both, and `research_window().survivorship` says
            which kind of universe the count sits on.
        by_status: How many securities carried each scoring status.
    """

    as_of: date
    in_record: int
    universe_members: int
    universe_observed: int
    universe_baseline: int
    universe_undetermined: int
    listed_on_date: int
    sufficient_price_history: int
    fundamentals_available: int
    market_cap_available: int
    score_reconstructable: int
    signals_available: int
    forward_returns_available: int
    recorded_exits: int
    terminal_outcomes_available: int
    interrupted: int
    fully_usable: int
    by_status: Mapping[str, int] = field(default_factory=dict)
    forward_returns_by_horizon: Mapping[int, int] = field(default_factory=dict)

    def attrition(self) -> tuple[tuple[str, int, float], ...]:
        """Return each stage with its survivors and the share lost reaching it.

        The share is measured against the previous stage, not against the top,
        so a stage that loses nothing reads as zero however small the population
        has already become.
        """
        stages = (
            ("in_record", self.in_record),
            ("listed_on_date", self.listed_on_date),
            ("sufficient_price_history", self.sufficient_price_history),
            ("fundamentals_available", self.fundamentals_available),
            ("market_cap_available", self.market_cap_available),
            ("score_reconstructable", self.score_reconstructable),
            ("signals_available", self.signals_available),
            ("forward_returns_available", self.forward_returns_available),
            ("fully_usable", self.fully_usable),
        )
        out: list[tuple[str, int, float]] = []
        previous = self.in_record
        for name, count in stages:
            lost = 0.0 if previous == 0 else (previous - count) / previous * 100
            out.append((name, count, lost))
            previous = count
        return tuple(out)


def _listed_on(session: Session, as_of: date, benchmark: str, *, tolerance: int) -> set[int]:
    """Return companies with a stored bar in the window ending at the date.

    A bar is evidence the security traded. The window absorbs a security that
    simply did not print on the day itself without absorbing one that stopped
    printing months ago.

    **The window is measured on the benchmark's calendar, never on the price
    table's own dates.** Deriving it from `price_history` would let the last
    securities still trading define what "recent" means, so a security that
    stopped six months ago would look current the moment its peers stopped too —
    the same inversion `pit.returns` refuses when it counts horizons.
    """
    window_start = _sessions_before(session, as_of, benchmark, sessions=tolerance)
    rows = session.execute(
        select(PriceHistory.company_id)
        .where(PriceHistory.date <= as_of, PriceHistory.date >= window_start)
        .group_by(PriceHistory.company_id)
    )
    return {company_id for (company_id,) in rows}


def _sessions_before(session: Session, as_of: date, benchmark: str, *, sessions: int) -> date:
    """Return the date `sessions` market sessions before a date."""
    rows = session.execute(
        select(BenchmarkPrice.date)
        .where(BenchmarkPrice.symbol == benchmark, BenchmarkPrice.date <= as_of)
        .order_by(BenchmarkPrice.date.desc())
        .limit(sessions + 1)
    ).all()
    return rows[-1][0] if rows else as_of


def _bar_counts(session: Session, as_of: date) -> dict[int, int]:
    """Return how many bars each company has on or before a date."""
    rows = session.execute(
        select(PriceHistory.company_id, func.count())
        .where(PriceHistory.date <= as_of)
        .group_by(PriceHistory.company_id)
    )
    return dict(rows.tuples().all())


def _filed_by(session: Session, as_of: date) -> set[int]:
    """Return companies with at least one observation filed by a date.

    The cheap gate before the expensive one: a company with nothing filed
    cannot produce point-in-time metrics, and asking the metric engine to prove
    that one company at a time is what makes a full sweep slow.
    """
    rows = session.execute(
        select(FundamentalObservation.company_id)
        .where(
            FundamentalObservation.filed_date.is_not(None),
            FundamentalObservation.filed_date <= as_of,
        )
        .group_by(FundamentalObservation.company_id)
    )
    return {company_id for (company_id,) in rows}


def _signalled(session: Session, as_of: date) -> set[int]:
    """Return companies with a derived market signal for a date."""
    rows = session.execute(
        select(PitSignalSnapshot.company_id).where(
            PitSignalSnapshot.snapshot_date == as_of,
            PitSignalSnapshot.signal_version == CURRENT_SIGNAL_VERSION,
        )
    )
    return {company_id for (company_id,) in rows}


def _recorded_exits(session: Session, as_of: date) -> tuple[int, int]:
    """Return exits recorded by a date, and how many can contribute an outcome.

    Counted over the whole delisting registry, not over today's roster. Joining
    to `companies` first would count only the exits whose ticker a *current*
    company happens to hold — which is almost entirely reused symbols, and would
    report the survivorship gap as if it were small.

    An exit contributes an outcome when a source states what a holder received
    (a cash terminal value) or when the security was reconstructed into
    `historical_securities` with a price path. An exit with neither is recorded
    and still UNKNOWN, which is the honest distinction between knowing a company
    left and knowing what its holders got.
    """
    exits = session.scalar(
        select(func.count(distinct(Delisting.ticker))).where(Delisting.effective_date <= as_of)
    )
    with_cash = session.scalar(
        select(func.count(distinct(Delisting.ticker))).where(
            Delisting.effective_date <= as_of, Delisting.cash_per_share.is_not(None)
        )
    )
    reconstructed = session.scalar(
        select(func.count(distinct(HistoricalSecurity.cik))).where(
            HistoricalSecurity.last_trading_date.is_not(None),
            HistoricalSecurity.last_trading_date <= as_of,
        )
    )
    return int(exits or 0), int(with_cash or 0) + int(reconstructed or 0)


def _realised(session: Session, as_of: date) -> tuple[set[int], dict[int, int], int]:
    """Return companies with realised returns, the per-horizon counts, and interruptions."""
    rows = session.execute(
        select(
            ForwardReturnRecord.company_id,
            ForwardReturnRecord.horizon_sessions,
            ForwardReturnRecord.status,
        ).where(ForwardReturnRecord.observation_date == as_of)
    ).all()
    companies: set[int] = set()
    per_horizon: Counter[int] = Counter()
    interrupted = 0
    for company_id, horizon, status in rows:
        if status == OutcomeStatus.REALISED.value:
            companies.add(company_id)
            per_horizon[horizon] += 1
        elif status == OutcomeStatus.INTERRUPTED.value:
            interrupted += 1
    return companies, dict(per_horizon), interrupted


def date_coverage(session: Session, settings: Settings, as_of: date) -> DateCoverage:
    """Measure what one date can support, across the whole roster.

    Args:
        session: Open database session.
        settings: Supplies thresholds and the benchmark symbol.
        as_of: The date to measure.

    Returns:
        The funnel for the date, every stage counted independently and the
        universe reported rather than assumed.
    """
    companies = CompanyRepository(session).list_all()
    universe = reconstruct_universe(session, as_of)
    listed = _listed_on(
        session, as_of, settings.benchmark_symbol, tolerance=LISTED_TOLERANCE_SESSIONS
    )
    bars = _bar_counts(session, as_of)
    filed = _filed_by(session, as_of)
    signalled = _signalled(session, as_of)
    realised, per_horizon, interrupted = _realised(session, as_of)

    exits, terminal = _recorded_exits(session, as_of)

    # Measured once for the date, not once per company: it is the same figure
    # for every security on a date, and re-reading the benchmark series five
    # thousand times is the difference between a sweep that finishes and one
    # that does not.
    market = benchmark_returns_as_of(session, settings.benchmark_symbol, as_of)

    deep_enough = {cid for cid, count in bars.items() if count >= YEAR_SESSIONS}
    statuses: Counter[str] = Counter()
    with_metrics: set[int] = set()
    with_market_cap: set[int] = set()
    scored: set[int] = set()

    for company in companies:
        if company.id not in filed:
            # Nothing had been filed by the date. The metric engine would say
            # the same thing after a query per company; it is counted here
            # instead so a full sweep stays affordable.
            statuses[ScoringStatus.INSUFFICIENT_DATA.value] += 1
            continue
        reconstructed = reconstruct_score(session, settings, company.id, as_of, benchmark=market)
        if reconstructed is None:
            statuses[ScoringStatus.ERROR.value] += 1
            continue
        statuses[reconstructed.score.status.value] += 1
        if reconstructed.metrics is None:
            continue
        with_metrics.add(company.id)
        if reconstructed.metrics.market_cap is not None:
            with_market_cap.add(company.id)
        if reconstructed.score.status is ScoringStatus.SCORED:
            scored.add(company.id)

    # Membership is not intersected in. See `fully_usable` on `DateCoverage`:
    # a universe of zero members would zero this on every historical date and
    # hide whether the data behind those dates is there or not. The two facts
    # are reported side by side and a study has to read both.
    usable = scored & deep_enough & listed & signalled & realised & with_market_cap

    coverage = DateCoverage(
        as_of=as_of,
        in_record=len(companies),
        universe_members=len(universe.members),
        universe_observed=len(universe.observed),
        universe_baseline=len(universe.members) - len(universe.observed),
        universe_undetermined=len(universe.undetermined),
        listed_on_date=len(listed),
        sufficient_price_history=len(deep_enough),
        fundamentals_available=len(with_metrics),
        market_cap_available=len(with_market_cap),
        score_reconstructable=len(scored),
        signals_available=len(signalled),
        forward_returns_available=len(realised),
        recorded_exits=exits,
        terminal_outcomes_available=terminal,
        interrupted=interrupted,
        fully_usable=len(usable),
        by_status=dict(statuses),
        forward_returns_by_horizon=per_horizon,
    )
    log.info(
        "date coverage measured",
        as_of=str(as_of),
        members=coverage.universe_members,
        scored=coverage.score_reconstructable,
        usable=coverage.fully_usable,
    )
    return coverage


@dataclass(frozen=True)
class ResearchWindow:
    """The span of dates the record can actually support, and its shape.

    Attributes:
        first_usable: Earliest measured date with any fully usable security.
        last_usable: Latest measured date with any fully usable security.
        longest_run: The longest unbroken run of measured dates that each hold
            at least `minimum` usable securities.
        median_usable: Median usable securities across the measured dates.
        minimum_usable: Fewest usable securities on any measured date.
        survivorship: Whether the universe was reconstructed from witnessed
            history, from baselines only, or not at all.
    """

    first_usable: date | None
    last_usable: date | None
    longest_run: tuple[date, date] | None
    median_usable: float
    minimum_usable: int
    survivorship: str


#: Membership is not reconstructable for the dates measured — every company's
#: record begins after them, so the roster is today's survivors and nothing else.
SURVIVORSHIP_UNRECONSTRUCTED = "UNRECONSTRUCTED"

#: Members exist but every one rests on a baseline, so the roster is known to be
#: present and not known to have been present for any length of time.
SURVIVORSHIP_BASELINE_ONLY = "BASELINE_ONLY"

#: At least one member's presence rests on a transition the system witnessed.
SURVIVORSHIP_OBSERVED = "OBSERVED"


def research_window(coverages: Sequence[DateCoverage], *, minimum: int = 1) -> ResearchWindow:
    """Reduce a series of date coverages to the window a study could use.

    Args:
        coverages: Measured dates, any order.
        minimum: How many usable securities a date needs to count as usable.

    Returns:
        The window's bounds, its longest unbroken run, and what the universe
        underneath it is actually made of.
    """
    ordered = sorted(coverages, key=lambda c: c.as_of)
    usable_dates = [c for c in ordered if c.fully_usable >= minimum]

    # The longest unbroken run of measured dates that each clear the minimum.
    # Measured in dates on the audit grid, not in calendar days: a monthly grid
    # with no gap is continuous even though its dates are a month apart.
    longest: tuple[date, date] | None = None
    best = 0
    run: list[date] = []
    for coverage in [*ordered, None]:
        if coverage is not None and coverage.fully_usable >= minimum:
            run.append(coverage.as_of)
            continue
        if len(run) > best:
            best, longest = len(run), (run[0], run[-1])
        run = []

    counts = sorted(c.fully_usable for c in ordered)
    median = 0.0
    if counts:
        middle = len(counts) // 2
        median = (
            float(counts[middle]) if len(counts) % 2 else (counts[middle - 1] + counts[middle]) / 2
        )

    if any(c.universe_observed > 0 for c in ordered):
        survivorship = SURVIVORSHIP_OBSERVED
    elif any(c.universe_members > 0 for c in ordered):
        survivorship = SURVIVORSHIP_BASELINE_ONLY
    else:
        survivorship = SURVIVORSHIP_UNRECONSTRUCTED

    return ResearchWindow(
        first_usable=usable_dates[0].as_of if usable_dates else None,
        last_usable=usable_dates[-1].as_of if usable_dates else None,
        longest_run=longest,
        median_usable=median,
        minimum_usable=min(counts) if counts else 0,
        survivorship=survivorship,
    )
