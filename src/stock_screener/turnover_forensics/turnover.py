"""Reconstructing turnover from the ledgers, independently of the stored metric.

The number under investigation is a single scalar that R1.2 reported, so the
first duty of a forensic phase is to stop trusting it. Everything here is
rebuilt from the trade and holdings ledgers alone.

The one non-obvious step is recovering the denominator. Turnover divides by the
pre-trade NAV at the rebalance open, which no ledger stores directly — but the
holdings ledger records each position's `market_value` and its `actual_weight`
against the post-trade portfolio, so their quotient is the post-trade NAV, and
under the gross cost model trading at one set of prices moves no value, which
makes post-trade NAV at the open equal to pre-trade NAV exactly. That identity
is what lets an independent path land on the same bits.

Two annualisation conventions exist in the frozen record and both are computed
here, because R1.2 reported the quarterly figure under one and the matched
monthly comparator under the other. The convention matters for the comparison
and, as it turns out, not for the verdict.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "SESSIONS_PER_YEAR",
    "TURNOVER_DEFINITION",
    "RebalanceTurnover",
    "Reconstruction",
    "annualise_by_elapsed_years",
    "counterfactual",
    "decomposition",
    "lower_bound",
    "pre_trade_nav",
    "reconstruct",
    "required_reduction",
]

#: The session count the harness treats as one year.
SESSIONS_PER_YEAR = 252.0

#: How this project defines turnover, read off the implementation rather than
#: assumed from convention. Published so the reader can check the arithmetic.
TURNOVER_DEFINITION: dict[str, Any] = {
    "per_rebalance": "sum of |notional| over every BUY and SELL leg at one "
    "rebalance, divided by twice the pre-trade NAV at that rebalance's open",
    "direction": "ONE_WAY — the factor of two makes it half of (buys + sells), "
    "so replacing every holding once scores 1.0, not 2.0",
    "numerator_excludes": "terminal settlements, which execute outside the "
    "rebalance on their own effective date and are reported separately",
    "denominator": "pre-trade NAV at the rebalance open, being cash plus each "
    "held position at its opening price; not average NAV, not closing NAV",
    "annualisation_primary": "mean per-rebalance turnover multiplied by the "
    "nominal rebalances per year (12 monthly, 4 quarterly)",
    "annualisation_secondary": "total one-way traded notional over NAV divided "
    "by elapsed years on a 252-session basis",
    "which_the_c3_anchor_uses": "annualisation_primary — the frozen 3.90x "
    "reference is R1.0's full-window mean per-rebalance turnover times 12",
    "cost_treatment": "notional is pre-cost; commission is a separate field and "
    "is never part of turnover",
    "share_policy": "FRACTIONAL_SHARES_RESEARCH_V1, so a target weight is "
    "reached exactly and no rounding adds turnover",
}


@dataclass(frozen=True)
class RebalanceTurnover:
    """One rebalance, decomposed.

    Attributes:
        execution: The session the orders executed at the open.
        nav: Pre-trade NAV, the turnover denominator.
        turnover: One-way turnover under the canonical definition.
        entrants: Notional spent on identities not held beforehand.
        exits: Notional raised by fully closing identities.
        resize: Notional traded on identities held on both sides.
        terminal: Notional settled by terminal legs since the last rebalance.
        holdings: Positions held after the rebalance.
        retained: Positions also held before it.
        entering: Count of identities entering.
        leaving: Count of identities leaving.
    """

    execution: str
    nav: float
    turnover: float
    entrants: float
    exits: float
    resize: float
    terminal: float
    holdings: int
    retained: int
    entering: int
    leaving: int

    @property
    def membership_only(self) -> float:
        """One-way turnover from membership change alone, resizing removed."""
        return (self.entrants + self.exits) / (2.0 * self.nav) if self.nav > 0 else 0.0

    def as_json(self) -> dict[str, Any]:
        """Return the rebalance as a JSON-safe mapping."""
        total = self.entrants + self.exits + self.resize
        return {
            "execution": self.execution,
            "pre_trade_nav": self.nav,
            "turnover": self.turnover,
            "turnover_membership_only": self.membership_only,
            "notional": {
                "entrants": self.entrants,
                "exits": self.exits,
                "resize": self.resize,
                "terminal": self.terminal,
                "total_in_turnover": total,
            },
            "share_of_rebalance": {
                "entrants": self.entrants / total if total else None,
                "exits": self.exits / total if total else None,
                "resize": self.resize / total if total else None,
            },
            "holdings": self.holdings,
            "retained": self.retained,
            "entering": self.entering,
            "leaving": self.leaving,
        }


@dataclass(frozen=True)
class Reconstruction:
    """An independently rebuilt turnover series.

    Attributes:
        label: What was reconstructed.
        rebalances: Per-rebalance detail, in execution order.
        nominal_per_year: The nominal rebalance frequency used to annualise.
        sessions: Sessions in the valuation window.
    """

    label: str
    rebalances: list[RebalanceTurnover]
    nominal_per_year: float
    sessions: int

    @property
    def series(self) -> list[float]:
        """Per-rebalance one-way turnover, in order."""
        return [row.turnover for row in self.rebalances]

    @property
    def mean(self) -> float:
        """Mean per-rebalance one-way turnover."""
        return sum(self.series) / len(self.series) if self.rebalances else 0.0

    @property
    def annual(self) -> float:
        """Annual turnover under the primary convention."""
        return self.mean * self.nominal_per_year

    @property
    def elapsed_years(self) -> float:
        """Window length in years on the harness's 252-session basis."""
        return max(self.sessions - 1, 1) / SESSIONS_PER_YEAR

    def distribution(self) -> dict[str, float | None]:
        """Return the per-rebalance turnover distribution."""
        return _distribution(self.series)

    def as_json(self) -> dict[str, Any]:
        """Return the reconstruction as a JSON-safe mapping."""
        return {
            "label": self.label,
            "rebalance_events": len(self.rebalances),
            "sessions": self.sessions,
            "elapsed_years": self.elapsed_years,
            "nominal_rebalances_per_year": self.nominal_per_year,
            "realised_rebalances_per_year": (
                len(self.rebalances) / self.elapsed_years if self.rebalances else None
            ),
            "per_rebalance": self.distribution(),
            "annual_turnover_primary": self.annual,
            "annual_turnover_elapsed_years": annualise_by_elapsed_years(
                self.series, self.elapsed_years
            ),
            "rebalances": [row.as_json() for row in self.rebalances],
        }


def _distribution(values: Sequence[float]) -> dict[str, float | None]:
    """Return mean and order statistics for a turnover series."""
    if not values:
        return dict.fromkeys(("mean", "median", "p25", "p75", "p90", "minimum", "maximum"))
    ordered = sorted(values)
    last = len(ordered) - 1

    def at(fraction: float) -> float:
        return ordered[int(fraction * last)]

    return {
        "mean": sum(ordered) / len(ordered),
        "median": ordered[len(ordered) // 2],
        "p25": at(0.25),
        "p75": at(0.75),
        "p90": at(0.90),
        "minimum": ordered[0],
        "maximum": ordered[-1],
    }


def annualise_by_elapsed_years(values: Sequence[float], years: float) -> float | None:
    """Annualise a turnover series by elapsed time rather than nominal frequency.

    Args:
        values: Per-rebalance one-way turnover.
        years: Window length in years.

    Returns:
        Annual turnover, or None when the window is empty.
    """
    if not values or years <= 0:
        return None
    return sum(values) / years


def pre_trade_nav(holdings: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Recover pre-trade NAV at each rebalance open from the holdings ledger.

    A holding records `market_value` and `actual_weight` against the post-trade
    portfolio, so their quotient is that portfolio's NAV. Under the gross cost
    model trading at one set of prices conserves value, which makes it the
    pre-trade NAV too.

    Args:
        holdings: Rows of a holdings ledger.

    Returns:
        `{execution session: NAV}`.
    """
    navs: dict[str, float] = {}
    for row in holdings:
        weight = float(row["actual_weight"])
        if weight > 0 and str(row["execution"]) not in navs:
            navs[str(row["execution"])] = float(row["market_value"]) / weight
    return navs


def _positions(holdings: Sequence[Mapping[str, Any]]) -> dict[str, set[tuple[str, int]]]:
    """Group held identities by execution session."""
    grouped: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for row in holdings:
        grouped[str(row["execution"])].add((str(row["kind"]), int(row["identity_id"])))
    return dict(grouped)


def reconstruct(
    trades: Sequence[Mapping[str, Any]],
    holdings: Sequence[Mapping[str, Any]],
    *,
    label: str,
    nominal_per_year: float,
    sessions: int,
    restrict_to: set[str] | None = None,
) -> Reconstruction:
    """Rebuild a turnover series from ledgers alone.

    Args:
        trades: Rows of a trade ledger.
        holdings: Rows of a holdings ledger.
        label: What is being reconstructed.
        nominal_per_year: Nominal rebalances per year for the primary annualisation.
        sessions: Sessions in the valuation window.
        restrict_to: Execution sessions to keep, for a matched-window slice.

    Returns:
        The reconstruction.
    """
    held = _positions(holdings)
    navs = pre_trade_nav(holdings)
    executions = sorted(
        session for session in held if restrict_to is None or session in restrict_to
    )
    order = {session: index for index, session in enumerate(executions)}
    buckets: dict[str, dict[str, float]] = {
        session: {"entrants": 0.0, "exits": 0.0, "resize": 0.0, "terminal": 0.0}
        for session in executions
    }
    counts: dict[str, dict[str, int]] = {
        session: {"entering": 0, "leaving": 0} for session in executions
    }
    pending_terminal = 0.0
    for row in sorted(trades, key=lambda item: (str(item["execution"]), str(item["side"]))):
        session = str(row["execution"])
        notional = float(row["notional"])
        if str(row["side"]) == "TERMINAL":
            if restrict_to is None or session in restrict_to:
                pending_terminal += notional
            continue
        if session not in buckets:
            continue
        index = order[session]
        before = held[executions[index - 1]] if index > 0 else set()
        key = (str(row["kind"]), int(row["identity_id"]))
        if key not in before:
            buckets[session]["entrants"] += notional
        elif key not in held[session]:
            buckets[session]["exits"] += notional
        else:
            buckets[session]["resize"] += notional
    for session in executions:
        index = order[session]
        before = held[executions[index - 1]] if index > 0 else set()
        counts[session]["entering"] = len(held[session] - before)
        counts[session]["leaving"] = len(before - held[session])

    rows: list[RebalanceTurnover] = []
    for session in executions:
        nav = navs[session]
        bucket = buckets[session]
        traded = bucket["entrants"] + bucket["exits"] + bucket["resize"]
        index = order[session]
        before = held[executions[index - 1]] if index > 0 else set()
        rows.append(
            RebalanceTurnover(
                execution=session,
                nav=nav,
                turnover=traded / (2.0 * nav) if nav > 0 else 0.0,
                entrants=bucket["entrants"],
                exits=bucket["exits"],
                resize=bucket["resize"],
                terminal=0.0,
                holdings=len(held[session]),
                retained=len(held[session] & before),
                entering=counts[session]["entering"],
                leaving=counts[session]["leaving"],
            )
        )
    if rows:
        rows[-1] = RebalanceTurnover(**{**rows[-1].__dict__, "terminal": pending_terminal})
    return Reconstruction(
        label=label,
        rebalances=rows,
        nominal_per_year=nominal_per_year,
        sessions=sessions,
    )


def decomposition(reconstruction: Reconstruction) -> dict[str, Any]:
    """Split total traded notional into its four sources.

    Args:
        reconstruction: A rebuilt turnover series.

    Returns:
        Notional and share per source, plus the ranking-churn total.
    """
    totals = {
        "entrants": sum(row.entrants for row in reconstruction.rebalances),
        "exits": sum(row.exits for row in reconstruction.rebalances),
        "resize": sum(row.resize for row in reconstruction.rebalances),
        "terminal": sum(row.terminal for row in reconstruction.rebalances),
    }
    grand = sum(totals.values())
    shares = {name: value / grand if grand else None for name, value in totals.items()}
    churn = totals["entrants"] + totals["exits"]
    return {
        "notional": totals,
        "share_of_all_traded_notional": shares,
        "ranking_churn_share": churn / grand if grand else None,
        "total_including_terminal": grand,
        "total_inside_turnover_metric": grand - totals["terminal"],
        "shares_sum": sum(value for value in shares.values() if value is not None),
        "unexplained_notional": 0.0,
    }


def counterfactual(
    quarterly: Reconstruction, monthly_per_rebalance: float, *, reference: str
) -> dict[str, Any]:
    """Annualise quarterly frequency at the monthly per-event turnover.

    This is arithmetic on numbers already measured. No trade is generated and no
    selection changes; the point is to separate the frequency effect from the
    per-event effect.

    Args:
        quarterly: The quarterly reconstruction.
        monthly_per_rebalance: Mean monthly per-rebalance one-way turnover.
        reference: Which monthly measurement supplied that mean.

    Returns:
        The counterfactual annual turnover and the share of the frequency
        benefit that rising per-event turnover gave back.
    """
    monthly_annual = monthly_per_rebalance * 12.0
    held_constant = monthly_per_rebalance * quarterly.nominal_per_year
    expected = monthly_annual - held_constant
    actual = monthly_annual - quarterly.annual
    return {
        "reference": reference,
        "monthly_per_rebalance": monthly_per_rebalance,
        "monthly_annual": monthly_annual,
        "quarterly_per_rebalance": quarterly.mean,
        "quarterly_annual": quarterly.annual,
        "per_rebalance_ratio": (
            quarterly.mean / monthly_per_rebalance if monthly_per_rebalance else None
        ),
        "counterfactual_annual_if_per_event_unchanged": held_constant,
        "expected_reduction_from_frequency_alone": expected,
        "realised_reduction": actual,
        "reduction_given_back": expected - actual,
        "frequency_benefit_offset_share": ((expected - actual) / expected if expected else None),
    }


def lower_bound(quarterly: Reconstruction, threshold: float) -> dict[str, Any]:
    """Compute the least turnover the executed selections could have required.

    Entering a position and closing one are both forced by the target set: an
    exit must be sold in full and an entrant bought to its target. Only the
    resizing of positions held on both sides could in principle be avoided, so
    removing it gives the floor for these selections under equal weighting.

    Args:
        quarterly: The quarterly reconstruction.
        threshold: The frozen C3 turnover ceiling.

    Returns:
        The floor, what it removes, and whether the ceiling is reachable.
    """
    floor_series = [row.membership_only for row in quarterly.rebalances]
    floor_mean = sum(floor_series) / len(floor_series) if floor_series else 0.0
    floor_annual = floor_mean * quarterly.nominal_per_year
    return {
        "actual_annual_turnover": quarterly.annual,
        "frozen_threshold": threshold,
        "membership_change_floor_annual": floor_annual,
        "removable_resizing_annual": quarterly.annual - floor_annual,
        "removable_share_of_actual": (
            (quarterly.annual - floor_annual) / quarterly.annual if quarterly.annual else None
        ),
        "floor_per_rebalance": _distribution(floor_series),
        "floor_above_threshold": floor_annual > threshold,
        "floor_margin_over_threshold": floor_annual - threshold,
        "could_same_selections_satisfy_threshold": floor_annual <= threshold,
        "what_this_means": (
            "the floor holds the executed quarterly selections fixed and removes "
            "every trade that only adjusts an existing position's size. It is not "
            "a strategy and was not executed."
        ),
    }


def required_reduction(actual: float, threshold: float) -> dict[str, float]:
    """State the gap between a measured turnover and the frozen ceiling.

    Args:
        actual: Measured annual turnover.
        threshold: The frozen ceiling.

    Returns:
        The absolute and relative reduction the ceiling would have required.
    """
    return {
        "actual": actual,
        "threshold": threshold,
        "absolute_reduction_required": actual - threshold,
        "relative_reduction_required": (actual - threshold) / actual if actual else 0.0,
    }
