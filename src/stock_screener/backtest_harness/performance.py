"""Turning a NAV path into the metrics a research report has to carry.

Nothing here is clever and nothing here is optional. The metric set is fixed by
the contract's research policy before any strategy runs, so a weak result is
described with the same numbers as a strong one and there is no room to choose
a flattering summary afterwards.

Two statements of honesty are built in rather than left to a footnote:

- **The risk-free rate is zero**, because no risk-free series is part of the
  frozen dataset. A Sharpe ratio here is an excess-over-nothing ratio and says
  so in its own name.
- **Every window is the same window.** A benchmark is measured over exactly the
  sessions the strategy was measured over, so a partial year cannot be compared
  against a full one by accident.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date

__all__ = [
    "RISK_FREE_RATE",
    "SESSIONS_PER_YEAR",
    "PerformanceMetrics",
    "benchmark_path",
    "performance",
    "window",
]

#: Sessions in a trading year, matching `domain.features`.
SESSIONS_PER_YEAR = 252

#: Stated, not assumed: the frozen dataset holds no risk-free series.
RISK_FREE_RATE = 0.0


@dataclass(frozen=True)
class PerformanceMetrics:
    """One path's summary over one window.

    Attributes:
        sessions: Sessions in the window.
        start: First valuation session.
        end: Last valuation session.
        start_nav: NAV at the first session.
        end_nav: NAV at the last.
        total_return: End over start, less one.
        cagr: Annualised from the session count, never from calendar dates.
        volatility: Annualised standard deviation of session returns.
        sharpe: Return over volatility, at a stated risk-free rate of zero.
        sortino: Return over downside deviation, same rate.
        max_drawdown: The deepest peak-to-trough fall in the window.
        calmar: CAGR over the absolute drawdown.
        best_session: Largest single-session gain.
        worst_session: Largest single-session loss.
        positive_sessions: Share of sessions with a positive return.
    """

    sessions: int
    start: str
    end: str
    start_nav: float
    end_nav: float
    total_return: float | None
    cagr: float | None
    volatility: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float | None
    calmar: float | None
    best_session: float | None
    worst_session: float | None
    positive_sessions: float | None

    def as_json(self) -> dict[str, object]:
        """A deterministic mapping."""
        return dict(sorted(asdict(self).items()))


def _returns(nav: Sequence[float]) -> list[float]:
    return [
        nav[position] / nav[position - 1] - 1.0
        for position in range(1, len(nav))
        if nav[position - 1] > 0
    ]


def _std(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _max_drawdown(nav: Sequence[float]) -> float | None:
    if not nav:
        return None
    peak = nav[0]
    worst = 0.0
    for value in nav:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def performance(sessions: Sequence[date], nav: Sequence[float]) -> PerformanceMetrics:
    """Summarise one NAV path.

    Args:
        sessions: The valuation sessions, in order.
        nav: The NAV at each session's close.

    Returns:
        The metrics, each None where the window is too short to support it.
    """
    if not sessions or not nav or len(sessions) != len(nav):
        return PerformanceMetrics(
            sessions=0,
            start="",
            end="",
            start_nav=0.0,
            end_nav=0.0,
            total_return=None,
            cagr=None,
            volatility=None,
            sharpe=None,
            sortino=None,
            max_drawdown=None,
            calmar=None,
            best_session=None,
            worst_session=None,
            positive_sessions=None,
        )
    daily = _returns(nav)
    total = nav[-1] / nav[0] - 1.0 if nav[0] > 0 else None
    years = max(len(nav) - 1, 1) / SESSIONS_PER_YEAR
    cagr = ((nav[-1] / nav[0]) ** (1.0 / years) - 1.0) if nav[0] > 0 and years > 0 else None
    deviation = _std(daily)
    volatility = deviation * math.sqrt(SESSIONS_PER_YEAR) if deviation is not None else None
    mean = sum(daily) / len(daily) if daily else None
    sharpe = (
        (mean - RISK_FREE_RATE) / deviation * math.sqrt(SESSIONS_PER_YEAR)
        if mean is not None and deviation
        else None
    )
    downside = [value for value in daily if value < RISK_FREE_RATE]
    downside_deviation = (
        math.sqrt(sum(value**2 for value in downside) / len(downside)) if downside else None
    )
    sortino = (
        (mean - RISK_FREE_RATE) / downside_deviation * math.sqrt(SESSIONS_PER_YEAR)
        if mean is not None and downside_deviation
        else None
    )
    drawdown = _max_drawdown(nav)
    calmar = cagr / abs(drawdown) if cagr is not None and drawdown else None
    return PerformanceMetrics(
        sessions=len(sessions),
        start=sessions[0].isoformat(),
        end=sessions[-1].isoformat(),
        start_nav=nav[0],
        end_nav=nav[-1],
        total_return=total,
        cagr=cagr,
        volatility=volatility,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=drawdown,
        calmar=calmar,
        best_session=max(daily) if daily else None,
        worst_session=min(daily) if daily else None,
        positive_sessions=(sum(1 for value in daily if value > 0) / len(daily) if daily else None),
    )


def window(
    sessions: Sequence[date], nav: Sequence[float], first: date, last: date
) -> tuple[list[date], list[float]]:
    """The slice of a path between two dates, inclusive, rebased to its start.

    The session immediately before `first` is kept as the opening valuation
    where one exists, so the first return inside the window is a real return
    and not a zero.

    Args:
        sessions: The whole path's sessions.
        nav: The whole path's NAV.
        first: The window's first session.
        last: The window's last session.

    Returns:
        The sessions and NAV inside the window.
    """
    positions = [position for position, session in enumerate(sessions) if first <= session <= last]
    if not positions:
        return [], []
    start = positions[0]
    if start > 0:
        start -= 1
    end = positions[-1]
    return list(sessions[start : end + 1]), list(nav[start : end + 1])


def benchmark_path(
    db: sqlite3.Connection, symbol: str, sessions: Sequence[date], capital: float
) -> list[float]:
    """A buy-and-hold benchmark NAV over exactly the study's sessions.

    Args:
        db: The research database, opened read-only.
        symbol: The benchmark's ticker.
        sessions: The study window's sessions, in order.
        capital: The nominal starting amount.

    Returns:
        The NAV at each session, entered at the first session's open and
        carrying the previous close through a gap.
    """
    closes: dict[str, float] = {}
    opens: dict[str, float] = {}
    for raw_date, opening, closing in db.execute(
        "SELECT date, open, close FROM benchmark_prices WHERE symbol = ?",
        (symbol,),
    ):
        if closing is not None:
            closes[str(raw_date)] = float(closing)
        if opening is not None:
            opens[str(raw_date)] = float(opening)
    # Bought at the open of the first session, exactly as a strategy is filled,
    # and valued at every close after it. Entering at the first close instead
    # would hand the strategy one session's opening move that the benchmark
    # never had.
    entry = opens.get(sessions[0].isoformat()) or closes.get(sessions[0].isoformat())
    if entry is None or entry <= 0:
        return []
    shares = capital / entry
    path: list[float] = []
    last = entry
    for session in sessions:
        last = closes.get(session.isoformat(), last)
        path.append(shares * last)
    return path
