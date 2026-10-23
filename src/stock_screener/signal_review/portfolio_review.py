"""Where the portfolio result actually came from.

A benchmark-relative total return of +49.58 pp is a summary, and a summary can
hide almost anything: one name, one quarter, one regime, a liquidity corner, a
cost assumption that happens to be generous. This module takes the R1.0 trade,
holdings and NAV ledgers apart and asks what the number is made of.

Nothing is removed. Outliers are measured, not trimmed; the cost break-even is
computed analytically from the realised trade ledger rather than by running
more backtests at more cost levels; and no rule is changed on the strength of
anything found here. The point is to know whether the result is the kind of
thing worth forming a hypothesis about.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from stock_screener.signal_review.statistics import quantile, spread, std

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "ROLLING_SESSIONS",
    "IdentityPnL",
    "concentration",
    "cost_break_even",
    "drawdown_episodes",
    "holding_concentration",
    "identity_contributions",
    "month_contributions",
    "relative_path",
    "return_distribution",
    "rolling_performance",
    "turnover_sources",
]

#: One trading year. Fixed, not searched.
ROLLING_SESSIONS = 252


@dataclass(frozen=True)
class IdentityPnL:
    """What one identity contributed to the portfolio over the whole path.

    Attributes:
        kind: The identity class.
        identity_id: The identity.
        symbol: Its dated display symbol, from the last trade that names it.
        bought: Notional bought.
        sold: Notional sold, excluding terminal settlements.
        terminal: Notional received from terminal settlements.
        costs: Transaction costs charged against it.
        residual_shares: Shares still held at the end of the path.
        residual_value: Those shares at the final session's price.
        pnl: The identity's contribution to the portfolio's profit.
        legs: How many times it was bought into the portfolio.
    """

    kind: str
    identity_id: int
    symbol: str
    bought: float
    sold: float
    terminal: float
    costs: float
    residual_shares: float
    residual_value: float
    pnl: float
    legs: int

    def as_json(self) -> dict[str, Any]:
        """A deterministic mapping."""
        return {
            "kind": self.kind,
            "identity_id": self.identity_id,
            "symbol": self.symbol,
            "bought": self.bought,
            "sold": self.sold,
            "terminal": self.terminal,
            "costs": self.costs,
            "residual_shares": self.residual_shares,
            "residual_value": self.residual_value,
            "pnl": self.pnl,
            "legs": self.legs,
        }


def identity_contributions(
    trades: Sequence[Mapping[str, Any]],
    final_prices: Mapping[tuple[str, int], float],
) -> list[IdentityPnL]:
    """Attribute the whole path's profit to the identities that produced it.

    Cash-flow accounting, identity by identity: a buy is money out, a sell or a
    terminal settlement is money in, and whatever is still held at the end is
    valued at the last session's close. The sum over identities is the
    portfolio's profit, which the caller is expected to check.

    Args:
        trades: The R1.0 trade ledger.
        final_prices: The last session's price per identity still held.

    Returns:
        One record per identity, ordered by contribution, largest first.
    """
    bought: dict[tuple[str, int], float] = defaultdict(float)
    sold: dict[tuple[str, int], float] = defaultdict(float)
    terminal: dict[tuple[str, int], float] = defaultdict(float)
    costs: dict[tuple[str, int], float] = defaultdict(float)
    shares: dict[tuple[str, int], float] = defaultdict(float)
    legs: dict[tuple[str, int], int] = defaultdict(int)
    symbols: dict[tuple[str, int], str] = {}
    for trade in trades:
        key = (str(trade["kind"]), int(trade["identity_id"]))
        notional = float(trade["notional"])
        cost = float(trade["cost"])
        costs[key] += cost
        symbols[key] = str(trade["symbol"]) or symbols.get(key, "")
        side = str(trade["side"])
        if side == "BUY":
            bought[key] += notional
            shares[key] += float(trade["shares"])
            legs[key] += 1
        elif side == "SELL":
            sold[key] += notional
            shares[key] -= float(trade["shares"])
        else:
            terminal[key] += notional
            shares[key] -= float(trade["shares"])

    out: list[IdentityPnL] = []
    for key in sorted(set(bought) | set(sold) | set(terminal)):
        residual = shares[key] if abs(shares[key]) > 1e-9 else 0.0
        value = residual * final_prices.get(key, 0.0)
        out.append(
            IdentityPnL(
                kind=key[0],
                identity_id=key[1],
                symbol=symbols.get(key, ""),
                bought=bought[key],
                sold=sold[key],
                terminal=terminal[key],
                costs=costs[key],
                residual_shares=residual,
                residual_value=value,
                pnl=sold[key] + terminal[key] + value - bought[key] - costs[key],
                legs=legs[key],
            )
        )
    out.sort(key=lambda record: (-record.pnl, record.kind, record.identity_id))
    return out


def concentration(contributions: Sequence[IdentityPnL], profit: float) -> dict[str, Any]:
    """How much of the profit the largest few identities account for.

    Args:
        contributions: Identity records, largest contribution first.
        profit: The portfolio's total profit, for the denominator.

    Returns:
        Cumulative shares at 1, 5, 10, 25 and 50 names, the same for the worst
        contributors, and the counts either side of zero.
    """
    winners = [record.pnl for record in contributions if record.pnl > 0]
    losers = sorted(record.pnl for record in contributions if record.pnl < 0)
    top = [record.pnl for record in contributions]
    cumulative = {}
    for count in (1, 5, 10, 25, 50):
        cumulative[f"top_{count}"] = sum(top[:count]) / profit if profit else None
    worst = {}
    for count in (1, 5, 10, 25, 50):
        worst[f"worst_{count}"] = sum(losers[:count]) / profit if profit else None
    return {
        "identities": len(contributions),
        "profit": profit,
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": len(winners) / len(contributions) if contributions else None,
        "gross_gains": sum(winners),
        "gross_losses": sum(losers),
        "share_of_profit": cumulative,
        "share_of_profit_from_worst": worst,
        "largest_contributor": (contributions[0].as_json() if contributions else None),
        "largest_detractor": (
            min(contributions, key=lambda r: r.pnl).as_json() if contributions else None
        ),
    }


def month_contributions(nav: Sequence[Mapping[str, Any]], capital: float) -> dict[str, Any]:
    """Arithmetic profit by calendar month, and how concentrated it is.

    Args:
        nav: The NAV ledger, one row per session.
        capital: The nominal starting amount, for the first month's opening.

    Returns:
        The monthly series and the share of total profit in the best and worst
        months. Nothing is removed.
    """
    by_month: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row in nav:
        by_month[str(row["session"])[:7]].append((str(row["session"]), float(row["nav"])))
    months = sorted(by_month)
    contributions: dict[str, float] = {}
    opening = capital
    for month in months:
        closing = by_month[month][-1][1]
        contributions[month] = closing - opening
        opening = closing
    profit = sum(contributions.values())
    ordered = sorted(contributions.items(), key=lambda item: -item[1])
    worst = sorted(contributions.items(), key=lambda item: item[1])
    return {
        "months": len(contributions),
        "profit": profit,
        "by_month": dict(sorted(contributions.items())),
        "best": [
            {"month": m, "profit": v, "share": v / profit if profit else None}
            for m, v in ordered[:5]
        ],
        "worst": [
            {"month": m, "profit": v, "share": v / profit if profit else None} for m, v in worst[:5]
        ],
        "share_of_profit": {
            "top_1": sum(v for _, v in ordered[:1]) / profit if profit else None,
            "top_3": sum(v for _, v in ordered[:3]) / profit if profit else None,
            "top_5": sum(v for _, v in ordered[:5]) / profit if profit else None,
        },
        "positive_months": sum(1 for v in contributions.values() if v > 0),
        "negative_months": sum(1 for v in contributions.values() if v < 0),
    }


def holding_concentration(holdings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Realised position concentration at every rebalance.

    Args:
        holdings: The R1.0 holdings ledger.

    Returns:
        Per-rebalance counts and weights, and their distributions. An
        equal-weight design should show a largest weight close to `1/n`.
    """
    by_month: dict[str, list[float]] = defaultdict(list)
    for row in holdings:
        by_month[str(row["month"])].append(float(row["actual_weight"]))
    rows: list[dict[str, Any]] = []
    for month in sorted(by_month):
        weights = sorted(by_month[month], reverse=True)
        total = sum(weights)
        effective = 1.0 / sum(w * w for w in weights) if any(weights) else None
        rows.append(
            {
                "month": month,
                "holdings": len(weights),
                "largest_weight": weights[0] if weights else None,
                "equal_weight_target": 1.0 / len(weights) if weights else None,
                "top_5_weight": sum(weights[:5]),
                "top_10_weight": sum(weights[:10]),
                "invested_weight": total,
                "effective_holdings": effective,
            }
        )
    return {
        "rebalances": len(rows),
        "by_rebalance": rows,
        "holdings": spread([float(r["holdings"]) for r in rows]),
        "largest_weight": spread([float(r["largest_weight"]) for r in rows if r["largest_weight"]]),
        "effective_holdings": spread(
            [float(r["effective_holdings"]) for r in rows if r["effective_holdings"]]
        ),
        "top_10_weight": spread([float(r["top_10_weight"]) for r in rows]),
    }


def turnover_sources(
    trades: Sequence[Mapping[str, Any]], holdings: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Split each rebalance's traded notional into why it was traded.

    Four causes, and the split answers whether the turnover is ranking churn or
    housekeeping: a name entering the selection, a name leaving it, a
    continuing holding being resized back to its equal weight, and a security
    that stopped trading.

    Args:
        trades: The R1.0 trade ledger.
        holdings: The R1.0 holdings ledger, to know what was held before.

    Returns:
        Notional and share by cause, overall and per rebalance.
    """
    after: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for row in holdings:
        after[str(row["month"])].add((str(row["kind"]), int(row["identity_id"])))
    held_before: dict[str, set[tuple[str, int]]] = {}
    running: set[tuple[str, int]] = set()
    for month in sorted(after):
        held_before[month] = set(running)
        running = after[month]

    causes: dict[str, float] = defaultdict(float)
    per_rebalance: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for trade in trades:
        month = str(trade["month"])
        key = (str(trade["kind"]), int(trade["identity_id"]))
        notional = float(trade["notional"])
        side = str(trade["side"])
        before = held_before.get(month)
        if side == "TERMINAL" or before is None:
            cause = "TERMINAL"
        elif side == "BUY" and key not in before:
            cause = "ENTRANT"
        elif side == "SELL" and key not in after.get(month, set()):
            cause = "EXIT"
        else:
            cause = "RESIZE"
        causes[cause] += notional
        per_rebalance[month][cause] += notional
    total = sum(causes.values())
    return {
        "total_traded_notional": total,
        "by_cause": dict(sorted(causes.items())),
        "share_by_cause": {
            cause: value / total if total else None for cause, value in sorted(causes.items())
        },
        "ranking_churn_share": (
            (causes.get("ENTRANT", 0.0) + causes.get("EXIT", 0.0)) / total if total else None
        ),
        "by_rebalance": {
            month: dict(sorted(values.items())) for month, values in sorted(per_rebalance.items())
        },
    }


def cost_break_even(
    trades: Sequence[Mapping[str, Any]],
    nav: Sequence[Mapping[str, Any]],
    *,
    strategy_total_return: float,
    benchmark_total_return: float,
    observed_cost_bps: float,
    observed_total_return: float,
    years: float,
) -> dict[str, Any]:
    """The uniform per-side cost that would erase the benchmark advantage.

    Analytic, from the realised ledger: a cost of `c` per side on a trade of
    notional `N` taken when the portfolio is worth `V` multiplies the path by
    `(1 - c·N/V)`, so the whole path is multiplied by roughly `exp(-c·S)` where
    `S` is the sum of `N/V` over every trade. Setting the costed total return
    equal to the benchmark's and solving for `c` gives the break-even.

    The model is validated against the one cost level R1.0 actually ran, and
    the residual is reported. **It is not a market model**: spread, market
    impact, borrow, latency and capacity are not in it, and a break-even of a
    few dozen basis points does not mean the strategy survives real execution
    at that level.

    Args:
        trades: The gross trade ledger.
        nav: The gross NAV ledger.
        strategy_total_return: The gross total return.
        benchmark_total_return: The benchmark's total return over the same window.
        observed_cost_bps: The cost level R1.0 actually ran.
        observed_total_return: The total return at that level.
        years: The window's length, for the annualised statement.

    Returns:
        The exposure sum, the break-even, and the model's residual.
    """
    value_at = {str(row["session"]): float(row["nav"]) for row in nav}
    exposure = 0.0
    unpriced = 0
    for trade in trades:
        portfolio = value_at.get(str(trade["execution"]))
        if portfolio is None or portfolio <= 0:
            unpriced += 1
            continue
        exposure += float(trade["notional"]) / portfolio
    predicted = (1.0 + strategy_total_return) * math.exp(
        -(observed_cost_bps / 10_000.0) * exposure
    ) - 1.0
    break_even = (
        math.log((1.0 + strategy_total_return) / (1.0 + benchmark_total_return)) / exposure
        if exposure > 0 and 1.0 + benchmark_total_return > 0
        else None
    )
    return {
        "traded_notional_over_nav": exposure,
        "annualised_traded_notional_over_nav": exposure / years if years else None,
        "trades_without_a_nav_row": unpriced,
        "model": "path multiplier exp(-c * sum(notional/NAV)); c is per side",
        "validation": {
            "cost_bps_per_side": observed_cost_bps,
            "observed_total_return": observed_total_return,
            "predicted_total_return": predicted,
            "residual": predicted - observed_total_return,
        },
        "break_even_cost_per_side_bps": break_even * 10_000.0 if break_even else None,
        "break_even_definition": (
            "the uniform per-side cost at which the gross total return would fall to the "
            "benchmark's over the same sessions"
        ),
        "not_modelled": [
            "bid-ask spread",
            "market impact",
            "capacity",
            "latency and partial fills",
            "borrow, financing and taxes",
        ],
    }


def relative_path(
    strategy: Sequence[Mapping[str, Any]], benchmark: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The strategy against the benchmark, session by session.

    Args:
        strategy: The strategy's NAV ledger.
        benchmark: The benchmark's NAV ledger over the same sessions.

    Returns:
        The relative path's drawdowns and the periods spent behind.
    """
    bench = {str(row["session"]): float(row["nav"]) for row in benchmark}
    sessions: list[str] = []
    ratio: list[float] = []
    for row in strategy:
        session = str(row["session"])
        other = bench.get(session)
        if other is None or other <= 0:
            continue
        sessions.append(session)
        ratio.append(float(row["nav"]) / other)
    if not ratio:
        return {"sessions": 0}
    base = ratio[0]
    ratio = [value / base for value in ratio]
    peak = ratio[0]
    worst = 0.0
    worst_at = sessions[0]
    longest = 0
    current = 0
    longest_start = sessions[0]
    start = sessions[0]
    for position, value in enumerate(ratio):
        if value >= peak:
            peak = value
            if current > longest:
                longest, longest_start = current, start
            current = 0
            start = sessions[position]
        else:
            current += 1
            drop = value / peak - 1.0
            if drop < worst:
                worst, worst_at = drop, sessions[position]
    if current > longest:
        longest, longest_start = current, start
    behind = [session for session, value in zip(sessions, ratio, strict=True) if value < 1.0]
    return {
        "sessions": len(sessions),
        "final_relative": ratio[-1],
        "worst_relative_drawdown": worst,
        "worst_relative_drawdown_at": worst_at,
        "longest_relative_drawdown_sessions": longest,
        "longest_relative_drawdown_from": longest_start,
        "sessions_behind_start": len(behind),
        "share_of_sessions_behind_start": len(behind) / len(sessions),
        "first_session_behind": behind[0] if behind else None,
        "last_session_behind": behind[-1] if behind else None,
    }


def rolling_performance(
    strategy: Sequence[Mapping[str, Any]],
    benchmark: Sequence[Mapping[str, Any]],
    *,
    window: int = ROLLING_SESSIONS,
) -> dict[str, Any]:
    """Twelve-month rolling return, relative return and volatility.

    One window, fixed in advance at a trading year. No other window is tried.

    Args:
        strategy: The strategy's NAV ledger.
        benchmark: The benchmark's NAV ledger.
        window: Sessions in the rolling window.

    Returns:
        The distributions of the three rolling series.
    """
    bench = {str(row["session"]): float(row["nav"]) for row in benchmark}
    sessions = [str(row["session"]) for row in strategy]
    values = [float(row["nav"]) for row in strategy]
    other = [bench.get(session) for session in sessions]
    returns = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]
    rolling: list[float] = []
    relative: list[float] = []
    volatility: list[float] = []
    for end in range(window, len(values)):
        start = end - window
        if values[start] <= 0:
            continue
        gain = values[end] / values[start] - 1.0
        rolling.append(gain)
        left, right = other[start], other[end]
        if left and right and left > 0:
            relative.append(gain - (right / left - 1.0))
        block = returns[start:end]
        deviation = std(block)
        if deviation is not None:
            volatility.append(deviation * math.sqrt(window))
    return {
        "window_sessions": window,
        "windows": len(rolling),
        "rolling_return": spread(rolling),
        "rolling_benchmark_relative_return": spread(relative),
        "rolling_volatility": spread(volatility),
        "windows_behind_benchmark": sum(1 for value in relative if value < 0),
    }


def drawdown_episodes(
    nav: Sequence[Mapping[str, Any]], *, limit: int = 5, threshold: float = 0.05
) -> list[dict[str, Any]]:
    """The deepest peak-to-trough falls, with their recovery dates.

    Args:
        nav: A NAV ledger.
        limit: How many episodes to report.
        threshold: The smallest fall worth reporting.

    Returns:
        Episodes, deepest first.
    """
    sessions = [str(row["session"]) for row in nav]
    values = [float(row["nav"]) for row in nav]
    episodes: list[dict[str, Any]] = []
    peak = values[0] if values else 0.0
    peak_at = sessions[0] if sessions else ""
    trough = peak
    trough_at = peak_at
    open_episode = False
    for position, value in enumerate(values):
        if value >= peak:
            if open_episode and peak > 0 and trough / peak - 1.0 <= -threshold:
                episodes.append(
                    {
                        "peak": peak_at,
                        "trough": trough_at,
                        "recovery": sessions[position],
                        "depth": trough / peak - 1.0,
                        "sessions": position - sessions.index(peak_at),
                        "recovered": True,
                    }
                )
            peak, peak_at = value, sessions[position]
            trough, trough_at = value, sessions[position]
            open_episode = False
        else:
            open_episode = True
            if value < trough:
                trough, trough_at = value, sessions[position]
    if open_episode and peak > 0 and trough / peak - 1.0 <= -threshold:
        episodes.append(
            {
                "peak": peak_at,
                "trough": trough_at,
                "recovery": None,
                "depth": trough / peak - 1.0,
                "sessions": len(values) - sessions.index(peak_at),
                "recovered": False,
            }
        )
    episodes.sort(key=lambda item: float(item["depth"]))
    return episodes[:limit]


def return_distribution(nav: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The monthly return distribution of one NAV path.

    Args:
        nav: A NAV ledger.

    Returns:
        Moments, quantiles and the positive-month rate.
    """
    by_month: dict[str, float] = {}
    for row in nav:
        by_month[str(row["session"])[:7]] = float(row["nav"])
    months = sorted(by_month)
    returns = [
        by_month[months[i]] / by_month[months[i - 1]] - 1.0
        for i in range(1, len(months))
        if by_month[months[i - 1]] > 0
    ]
    body = spread(returns)
    body["p5"] = quantile(returns, 0.05)
    body["p95"] = quantile(returns, 0.95)
    body["months"] = len(returns)
    return body
