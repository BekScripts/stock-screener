"""Backfilling the point-in-time record over a range of past sessions.

The daily passes accumulate history one day at a time going forward. This runs
the *same* passes over sessions that have already happened, so a record that
would otherwise take years to grow can be built from the price history already
stored. Nothing new is computed: `capture_day`, `derive_market_signals` and
`measure_forward_returns` are called unchanged, with the same arguments a daily
run would pass. This module is the loop around them, and the four properties
that loop has to have.

**Idempotent.** Every underlying pass already refuses to rewrite a day: a
capture with a completed run is a no-op, a derivation for a day and version that
exists writes nothing, and a measured horizon is skipped before its prices are
read. The runner adds nothing to that — it just does not treat "already done" as
an error.

**Resumable.** Each session is its own transaction. A run killed halfway leaves
every completed day intact and the day in flight absent, and re-running picks up
where it stopped. There is no cursor to corrupt, because the record itself is
the cursor.

**Observable.** Each date reports what it wrote as it finishes, through a
progress callback, so a run measured in hours is legible while it happens rather
than only at the end.

**Safe to rerun.** No pass here updates or deletes anything. A second run over
the same range writes zero rows and says so.

The one thing this module does differently from a daily run is *where the prices
come from*: a `CachedPriceSource` is loaded once and shared across every date,
because reading six million bars per session described is what makes the naive
loop take days. The bars it returns are the same bars under the same `as_of`
bound — see `stock_screener.pit.prices`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from data_access import (
    BenchmarkPriceRepository,
    ForwardReturnRepository,
    PitFeatureSnapshotRepository,
    PitSignalSnapshotRepository,
    session_scope,
)
from domain import CURRENT_SIGNAL_VERSION
from stock_screener.pit.capture import FEATURE_VERSION, capture_day
from stock_screener.pit.prices import CachedPriceSource
from stock_screener.pit.returns import measure_forward_returns
from stock_screener.signals.derivation import derive_market_signals

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session, sessionmaker

    from stock_screener.config import Settings

log = structlog.get_logger(__name__)


class Cadence(StrEnum):
    """How densely to sample the session calendar.

    A daily backfill is the complete record and the expensive one. The coarser
    cadences exist because a study rebalancing monthly does not need 1,150
    decision dates to be studied, and because a coarse pass finishing today is
    worth more than a dense one finishing next week.
    """

    DAILY = "daily"
    """Every session in the range."""

    WEEKLY = "weekly"
    """The last session of each calendar week."""

    MONTHLY = "monthly"
    """The last session of each calendar month."""


class DateStatus(StrEnum):
    """What a single date's pass did."""

    WRITTEN = "WRITTEN"
    """Rows were newly stored for this date."""

    SKIPPED = "SKIPPED"
    """The date was already complete; nothing was written."""

    EMPTY = "EMPTY"
    """The pass ran and had nothing to store — no security qualified."""

    FAILED = "FAILED"
    """The pass raised. The date's transaction was rolled back."""


@dataclass(frozen=True)
class DateResult:
    """What one session's backfill did.

    Attributes:
        session_date: The session described.
        status: Whether rows were written, skipped, empty or refused.
        rows: How many rows were newly stored.
        securities: How many distinct securities the date now covers.
        detail: The failure, when there was one.
        seconds: Wall-clock time the date took.
    """

    session_date: date
    status: DateStatus
    rows: int = 0
    securities: int = 0
    detail: str | None = None
    seconds: float = 0.0


@dataclass(frozen=True)
class BackfillReport:
    """What a whole backfill run did, and where the record still has holes.

    Attributes:
        kind: Which pass ran, for a caller printing one line.
        requested: The sessions the run was asked to cover, in order.
        results: One entry per requested session, in order.
        covered: The requested sessions that hold rows now, whether this run
            wrote them or found them already there. The difference between this
            and `requested` is `missing`, and that gap is the point of the
            report: a backfill that says it finished while leaving dates empty
            has not finished.
        elapsed_seconds: Wall-clock time for the whole run.
    """

    kind: str
    requested: tuple[date, ...]
    results: tuple[DateResult, ...]
    covered: tuple[date, ...]
    elapsed_seconds: float = 0.0

    @property
    def written(self) -> tuple[DateResult, ...]:
        """The dates this run newly stored rows for."""
        return tuple(r for r in self.results if r.status is DateStatus.WRITTEN)

    @property
    def skipped(self) -> tuple[DateResult, ...]:
        """The dates that were already complete."""
        return tuple(r for r in self.results if r.status is DateStatus.SKIPPED)

    @property
    def failed(self) -> tuple[DateResult, ...]:
        """The dates whose pass raised, each carrying its reason."""
        return tuple(r for r in self.results if r.status is DateStatus.FAILED)

    @property
    def rows(self) -> int:
        """How many rows the run newly stored."""
        return sum(r.rows for r in self.results)

    @property
    def missing(self) -> tuple[date, ...]:
        """Requested sessions that still hold no rows.

        A hole in the record, whether it was refused, empty, or never reached.
        Reported rather than smoothed over: a study reading a date with no rows
        must know it is absent and not merely uneventful.
        """
        covered = set(self.covered)
        return tuple(day for day in self.requested if day not in covered)

    @property
    def earliest_covered(self) -> date | None:
        """The oldest requested session that holds rows."""
        return self.covered[0] if self.covered else None

    @property
    def latest_covered(self) -> date | None:
        """The newest requested session that holds rows."""
        return self.covered[-1] if self.covered else None

    def summary(self) -> str:
        """Return a one-line description for a terminal."""
        span = (
            f"{self.earliest_covered}..{self.latest_covered}" if self.covered else "nothing covered"
        )
        return (
            f"{self.kind}: {len(self.requested)} sessions requested, "
            f"{len(self.written)} written, {len(self.skipped)} already present, "
            f"{len(self.failed)} failed, {len(self.missing)} missing — "
            f"{self.rows} rows over {span} "
            f"in {self.elapsed_seconds:.0f}s"
        )


#: Called after each session with the result and how far through the run it is.
ProgressCallback = Callable[[DateResult, int, int], None]


def market_sessions(
    session: Session, benchmark: str, *, start: date, end: date
) -> tuple[date, ...]:
    """Return every session the market was open in a range, ascending.

    The calendar is the benchmark's, for the reason `pit.returns` gives: the
    benchmark trades every session, so a gap in it is a market holiday and a gap
    in a security is the security's own.

    Args:
        session: Open database session.
        benchmark: The symbol whose bars define the calendar.
        start: Earliest session to include.
        end: Latest session to include.

    Returns:
        The sessions in the range, ascending. Empty when the benchmark has no
        stored history, which is a missing input rather than a closed market.
    """
    bars = BenchmarkPriceRepository(session).list_bars(benchmark)
    return tuple(bar.date for bar in bars if start <= bar.date <= end)


def sample(sessions: Sequence[date], cadence: Cadence) -> tuple[date, ...]:
    """Thin a session calendar to the requested cadence.

    The last session of each period is taken, never the first: a weekly decision
    date is the one a study acts on at the week's close, and the first session of
    a week that began on a holiday is a different day in different weeks.

    Args:
        sessions: The full calendar, ascending.
        cadence: How densely to sample it.

    Returns:
        The sampled sessions, ascending.
    """
    if cadence is Cadence.DAILY:
        return tuple(sessions)

    def key(day: date) -> tuple[int, ...]:
        if cadence is Cadence.WEEKLY:
            iso = day.isocalendar()
            return (iso.year, iso.week)
        return (day.year, day.month)

    last: dict[tuple[int, ...], date] = {}
    for day in sessions:
        last[key(day)] = day
    return tuple(sorted(last.values()))


def _run(
    kind: str,
    factory: sessionmaker[Session],
    sessions: Sequence[date],
    step: Callable[[Session, date, CachedPriceSource], tuple[DateStatus, int]],
    coverage: Callable[[Session, Sequence[date]], dict[date, int]],
    *,
    on_progress: ProgressCallback | None = None,
) -> BackfillReport:
    """Run one pass over a list of sessions, a transaction each.

    The shared shape of every backfill: load the price record once, walk the
    dates, isolate each one, and finish by asking the store which dates actually
    hold rows rather than trusting the loop's own bookkeeping.
    """
    started = time.perf_counter()
    ordered = sorted(set(sessions))
    if not ordered:
        return BackfillReport(kind=kind, requested=(), results=(), covered=())

    with session_scope(factory) as loading:
        prices = CachedPriceSource.load(loading)

    results: list[DateResult] = []
    for index, day in enumerate(ordered, start=1):
        began = time.perf_counter()
        try:
            with session_scope(factory) as session:
                status, rows = step(session, day, prices)
        except Exception as error:
            result = DateResult(
                session_date=day,
                status=DateStatus.FAILED,
                detail=f"{type(error).__name__}: {error}",
                seconds=time.perf_counter() - began,
            )
            log.warning("backfill date failed", kind=kind, date=str(day), detail=result.detail)
        else:
            result = DateResult(
                session_date=day,
                status=status,
                rows=rows,
                seconds=time.perf_counter() - began,
            )
        results.append(result)
        if on_progress is not None:
            on_progress(result, index, len(ordered))

    with session_scope(factory) as session:
        present = coverage(session, ordered)

    results = [
        DateResult(
            session_date=r.session_date,
            status=r.status,
            rows=r.rows,
            securities=present.get(r.session_date, 0),
            detail=r.detail,
            seconds=r.seconds,
        )
        for r in results
    ]
    covered = tuple(day for day in ordered if present.get(day, 0) > 0)
    report = BackfillReport(
        kind=kind,
        requested=tuple(ordered),
        results=tuple(results),
        covered=covered,
        elapsed_seconds=time.perf_counter() - started,
    )
    log.info("backfill complete", summary=report.summary())
    return report


def backfill_signals(
    factory: sessionmaker[Session],
    settings: Settings,
    sessions: Sequence[date],
    *,
    on_progress: ProgressCallback | None = None,
) -> BackfillReport:
    """Capture features and derive market signals for each of many past sessions.

    Runs the two existing passes in order for every date — `capture_day`, then
    `derive_market_signals` — because the second reads what the first wrote.
    Both run inside one transaction per date, so a date never ends up with
    features and no signals.

    For a genuinely past date `capture_day` writes features only; it refuses to
    copy a current operational score onto a past day, which is what keeps a
    backfill from smuggling today's score into yesterday.

    Args:
        factory: Session factory. One session is opened per date.
        settings: Supplies the benchmark symbol.
        sessions: The dates to describe, in any order. Deduplicated and sorted.
        on_progress: Called after each date with the result and the position.

    Returns:
        What was written, what was already there, and which dates still hold
        nothing.
    """

    def step(session: Session, day: date, prices: CachedPriceSource) -> tuple[DateStatus, int]:
        captured = capture_day(session, settings, snapshot_date=day, prices=prices)
        derived = derive_market_signals(session, snapshot_date=day)
        written = captured.features + derived.derived
        if captured.already_captured and derived.already_derived:
            return DateStatus.SKIPPED, 0
        if written == 0:
            return DateStatus.EMPTY, 0
        return DateStatus.WRITTEN, written

    def coverage(session: Session, days: Sequence[date]) -> dict[date, int]:
        return PitSignalSnapshotRepository(session).counts_by_date(
            days, signal_version=CURRENT_SIGNAL_VERSION
        )

    return _run("signals", factory, sessions, step, coverage, on_progress=on_progress)


def backfill_returns(
    factory: sessionmaker[Session],
    settings: Settings,
    sessions: Sequence[date],
    *,
    on_progress: ProgressCallback | None = None,
) -> BackfillReport:
    """Measure forward returns from each of many past decision dates.

    Calls `measure_forward_returns` unchanged, so every property it guarantees
    holds for a backfilled date exactly as for a live one: the entry is the open
    of the session strictly after the decision date, horizons are counted on the
    market's calendar, and a security that stopped trading is recorded as
    interrupted rather than filled in.

    Args:
        factory: Session factory. One session is opened per date.
        settings: Supplies the benchmark symbol.
        sessions: The decision dates to measure from.
        on_progress: Called after each date with the result and the position.

    Returns:
        What was measured, and which dates still hold nothing.
    """

    def step(session: Session, day: date, prices: CachedPriceSource) -> tuple[DateStatus, int]:
        measured = measure_forward_returns(
            session, day, benchmark=settings.benchmark_symbol, prices=prices
        )
        if measured.stored == 0:
            return DateStatus.SKIPPED, 0
        return DateStatus.WRITTEN, measured.stored

    def coverage(session: Session, days: Sequence[date]) -> dict[date, int]:
        return ForwardReturnRepository(session).counts_by_date(days)

    return _run("returns", factory, sessions, step, coverage, on_progress=on_progress)


def feature_coverage(session: Session, days: Sequence[date]) -> dict[date, int]:
    """Return how many feature rows each date holds, for an audit."""
    return PitFeatureSnapshotRepository(session).counts_by_date(
        days, feature_version=FEATURE_VERSION
    )
