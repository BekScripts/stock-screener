"""The portfolio simulator: cash, shares, execution prices and terminal legs.

A backtest that multiplies a selection by a stored forward return is not a
portfolio; it is a weighted average of labels wearing a portfolio's clothes. It
cannot run out of cash, cannot hold a name whose tape stopped, cannot show
turnover and cannot be reconciled. So nothing here reads a forward return. The
path is built the long way — open prices on the execution session, closes every
session after it, explicit cash, explicit shares — and every invariant that
ought to hold is checked rather than assumed.

## Execution

`CLOSE_SIGNAL_NEXT_SESSION_OPEN`. A target set computed at the close of the
decision session is executed at the open of the next session, sells first and
buys after, in identity order so two runs trade identically. The portfolio is
long-only, unlevered and never spends cash it does not hold.

## Terminal legs — `TERMINAL_LEG_POLICY_V1`

A held security whose tape stops is the one place a simulator is tempted to
invent a number. This one refuses to:

1. **Settled or extinguished** — a source states what a holder received, found
   by identity through dated identifier intervals, never by a shared symbol.
   The position is liquidated at that value on the effective date and the cash
   is real.
2. **Terminal, value unknown** — the series ends, nothing states a value. The
   position is carried at its last observed close and flagged. The result is
   reported with a bound: every unknown leg is also valued at zero, and both
   numbers are shown. A falling price is never read as a settlement.
3. **A gap that later resumes** — carried at the last close, no terminal claim.

## Corporate actions

The stored bars are the vendor's fully adjusted series, so splits and reverse
splits are exact and distributions are inside the price rather than modelled as
cash. Results are therefore an **adjusted-basis total return**: dividend
reinvestment is implicit, spinoffs are unsupported, and a symbol change is
invisible because the identity, not the symbol, is the key.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "GROSS",
    "SHARE_POLICY",
    "SIMPLE_10BPS_PER_SIDE",
    "STARTING_CAPITAL",
    "TERMINAL_LEG_POLICY",
    "CostModel",
    "Holding",
    "PriceBook",
    "Rebalance",
    "SimulationResult",
    "TerminalLeg",
    "TerminalState",
    "Trade",
    "simulate",
]

#: The frozen terminal-leg policy this phase runs under.
TERMINAL_LEG_POLICY = "TERMINAL_LEG_POLICY_V1"

#: Fractional shares, so an equal-weight target is exactly equal-weight and no
#: residual cash accumulates from rounding. The same policy for every baseline.
SHARE_POLICY = "FRACTIONAL_SHARES_RESEARCH_V1"

#: One nominal research amount, never tuned. Comparisons are normalised.
STARTING_CAPITAL = 100_000.0

#: Below this, a rebalance holds cash rather than inventing a portfolio.
MIN_CROSS_SECTION = 30


@dataclass(frozen=True)
class CostModel:
    """A transaction-cost assumption, applied to traded notional per side.

    Attributes:
        name: Its identifier, recorded on every run manifest.
        bps_per_side: Basis points charged on the notional of each side.
    """

    name: str
    bps_per_side: float

    def charge(self, notional: float) -> float:
        """The cost of trading `notional` on one side."""
        return abs(notional) * self.bps_per_side / 10_000.0


#: No costs. The primary result, because the cost model is not a market model.
GROSS = CostModel("GROSS", 0.0)

#: The single preregistered sensitivity. Not a claim about real execution.
SIMPLE_10BPS_PER_SIDE = CostModel("SIMPLE_10BPS_PER_SIDE_V1", 10.0)


class TerminalState(StrEnum):
    """What the record establishes about a security that stopped trading."""

    SETTLED = "SETTLED"
    EXTINGUISHED = "EXTINGUISHED"
    UNKNOWN = "UNKNOWN"
    ONGOING = "ONGOING"


@dataclass(frozen=True)
class TerminalLeg:
    """What a holder of one identity was finally left with, or was not.

    Attributes:
        key: The identity.
        state: What the record establishes.
        effective_date: The date it left, where one is recorded.
        value_per_share: What a holder received. None unless settled or
            extinguished — never a last traded price.
        reason: The classified reason it left.
        source: What established the value.
    """

    key: tuple[str, int]
    state: TerminalState
    effective_date: date | None
    value_per_share: float | None
    reason: str
    source: str | None


class PriceBook:
    """Adjusted daily opens and closes for the identities a study holds.

    Args:
        sessions: The benchmark sessions of the study window, in order.
        opens: `{identity: [open or None per session]}`.
        closes: `{identity: [close or None per session]}`.
    """

    def __init__(
        self,
        sessions: Sequence[date],
        opens: Mapping[tuple[str, int], list[float | None]],
        closes: Mapping[tuple[str, int], list[float | None]],
    ) -> None:
        self.sessions = list(sessions)
        self.index = {session: position for position, session in enumerate(self.sessions)}
        self._opens = dict(opens)
        self._closes = dict(closes)
        # The last session each identity has a close on, so asking whether a
        # security trades again after a date is a comparison rather than a scan
        # over the remaining calendar on every session it is carried.
        self._last_bar: dict[tuple[str, int], int] = {}
        for key, series in self._closes.items():
            last = -1
            for position, value in enumerate(series):
                if value is not None:
                    last = position
            self._last_bar[key] = last

    @classmethod
    def load(
        cls,
        db: sqlite3.Connection,
        sessions: Sequence[date],
        keys: Iterable[tuple[str, int]],
    ) -> PriceBook:
        """Read every bar the study can reach for these identities, once.

        Args:
            db: The research database, opened read-only.
            sessions: The study window's sessions, in order.
            keys: The identities to load.

        Returns:
            The price book.
        """
        wanted = set(keys)
        index = {session: position for position, session in enumerate(sessions)}
        first, last = sessions[0].isoformat(), sessions[-1].isoformat()
        opens: dict[tuple[str, int], list[float | None]] = {
            key: [None] * len(sessions) for key in wanted
        }
        closes: dict[tuple[str, int], list[float | None]] = {
            key: [None] * len(sessions) for key in wanted
        }
        queries = (
            (
                "COMPANY",
                "SELECT company_id, date, open, close FROM price_history"
                " WHERE date BETWEEN ? AND ?",
            ),
            (
                "HISTORICAL",
                "SELECT historical_security_id, date, open, close FROM historical_prices"
                " WHERE date BETWEEN ? AND ?",
            ),
        )
        for kind, query in queries:
            for identity_id, raw_date, opening, closing in db.execute(query, (first, last)):
                key = (kind, int(identity_id))
                if key not in wanted:
                    continue
                position = index.get(date.fromisoformat(str(raw_date)))
                if position is None:
                    continue
                if opening is not None:
                    opens[key][position] = float(opening)
                if closing is not None:
                    closes[key][position] = float(closing)
        return cls(sessions, opens, closes)

    def open_at(self, key: tuple[str, int], position: int) -> float | None:
        """The identity's opening price on a session, or None."""
        series = self._opens.get(key)
        return None if series is None else series[position]

    def close_at(self, key: tuple[str, int], position: int) -> float | None:
        """The identity's closing price on a session, or None."""
        series = self._closes.get(key)
        return None if series is None else series[position]

    def last_close_on_or_before(self, key: tuple[str, int], position: int) -> float | None:
        """The newest close at or before a session, for carrying a gap."""
        series = self._closes.get(key)
        if series is None:
            return None
        for walk in range(position, -1, -1):
            if series[walk] is not None:
                return series[walk]
        return None

    def has_later_bar(self, key: tuple[str, int], position: int) -> bool:
        """Whether the identity trades again after a session."""
        return self._last_bar.get(key, -1) > position


@dataclass(frozen=True)
class Rebalance:
    """One decision's target portfolio.

    Attributes:
        month: The decision month.
        decision: The decision session.
        execution: The session the targets are executed at.
        targets: `{identity: target weight}`, summing to at most one.
        split: The block the decision belongs to.
    """

    month: str
    decision: date
    execution: date
    targets: dict[tuple[str, int], float]
    split: str


@dataclass(frozen=True)
class Trade:
    """One executed order.

    Attributes:
        month: The decision month that produced it.
        decision: The decision session.
        execution: The session it executed on.
        kind: The identity class.
        identity_id: The identity.
        symbol: The symbol as a dated display attribute. Never a key.
        side: `BUY`, `SELL` or `TERMINAL`.
        shares: Shares traded, always positive.
        price: The execution price.
        notional: Shares times price.
        cost: The transaction cost charged.
        reason: Why the trade happened.
    """

    month: str
    decision: str
    execution: str
    kind: str
    identity_id: int
    symbol: str
    side: str
    shares: float
    price: float
    notional: float
    cost: float
    reason: str


@dataclass(frozen=True)
class Holding:
    """One position at one rebalance, after execution.

    Attributes:
        month: The decision month.
        execution: The execution session.
        kind: The identity class.
        identity_id: The identity.
        symbol: Display only.
        shares: Shares held.
        price: The valuation price.
        market_value: Shares times price.
        target_weight: What the strategy asked for.
        actual_weight: What was achieved.
        basis: `BAR`, `CARRY` or `SETTLEMENT`.
    """

    month: str
    execution: str
    kind: str
    identity_id: int
    symbol: str
    shares: float
    price: float
    market_value: float
    target_weight: float
    actual_weight: float
    basis: str


@dataclass
class SimulationResult:
    """One strategy's path, its ledgers and everything that went wrong.

    Attributes:
        strategy: The strategy version.
        cost_model: The cost assumption applied.
        sessions: Every valuation session, in order.
        nav: Net asset value at each session's close.
        cash: Cash at each session's close.
        floor_nav: NAV with every unknown terminal leg valued at zero.
        holdings_count: Positions held at each session.
        trades: Every executed order.
        holdings: Positions at each rebalance.
        turnover: One-way turnover fraction at each rebalance.
        rebalances: How many rebalances executed.
        diagnostics: Counts a reader needs to trust the path.
        violations: Invariants that failed. Empty is the only acceptable value.
    """

    strategy: str
    cost_model: str
    sessions: list[date] = field(default_factory=list)
    nav: list[float] = field(default_factory=list)
    cash: list[float] = field(default_factory=list)
    floor_nav: list[float] = field(default_factory=list)
    holdings_count: list[int] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)
    turnover: list[float] = field(default_factory=list)
    rebalances: int = 0
    diagnostics: dict[str, int] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)


def simulate(
    strategy: str,
    rebalances: Sequence[Rebalance],
    book: PriceBook,
    *,
    terminal: Mapping[tuple[str, int], TerminalLeg],
    symbols: Mapping[tuple[str, int], str],
    cost: CostModel = GROSS,
    capital: float = STARTING_CAPITAL,
) -> SimulationResult:
    """Walk the sessions, execute the targets, and account for every unit of cash.

    Args:
        strategy: The strategy version, recorded on the result.
        rebalances: The target portfolios, in decision order.
        book: The study window's prices.
        terminal: What the record establishes for identities that left.
        symbols: The dated display symbol per identity, for the ledgers.
        cost: The transaction-cost assumption.
        capital: The nominal starting amount.

    Returns:
        The path, the ledgers and the diagnostics.
    """
    result = SimulationResult(strategy=strategy, cost_model=cost.name)
    by_execution = {r.execution: r for r in rebalances}
    shares: dict[tuple[str, int], float] = {}
    carried: dict[tuple[str, int], float] = {}
    settled: set[tuple[str, int]] = set()
    unknown_terminal: set[tuple[str, int]] = set()
    counts: dict[str, int] = defaultdict(int)
    cash = capital
    started = False

    for position, session in enumerate(book.sessions):
        # 1. Terminal settlements, before anything else needs the cash.
        for key in sorted(shares):
            leg = terminal.get(key)
            if leg is None or key in settled:
                continue
            if leg.state not in (TerminalState.SETTLED, TerminalState.EXTINGUISHED):
                continue
            if leg.effective_date is None or leg.effective_date > session:
                continue
            held = shares.pop(key)
            value = float(leg.value_per_share or 0.0)
            proceeds = held * value
            charge = cost.charge(proceeds)
            cash += proceeds - charge
            settled.add(key)
            counts[f"terminal_{leg.state.lower()}"] += 1
            result.trades.append(
                Trade(
                    month="",
                    decision="",
                    execution=session.isoformat(),
                    kind=key[0],
                    identity_id=key[1],
                    symbol=symbols.get(key, ""),
                    side="TERMINAL",
                    shares=held,
                    price=value,
                    notional=proceeds,
                    cost=charge,
                    reason=f"TERMINAL_{leg.state}:{leg.reason}",
                )
            )

        # 2. Rebalance at the open.
        plan = by_execution.get(session)
        if plan is not None:
            started = True
            cash, traded = _rebalance(
                plan, book, position, shares, carried, cash, cost, symbols, counts, result
            )
            result.rebalances += 1
            result.turnover.append(traded)

        if not started:
            continue

        # 3. Value at the close.
        market = 0.0
        floor = 0.0
        for key, held in shares.items():
            price, basis = _valuation_price(book, key, position, carried, terminal)
            if price is None:
                counts["unvaluable_positions"] += 1
                continue
            market += held * price
            if basis == "CARRY" and _is_terminal_unknown(book, key, position, terminal):
                unknown_terminal.add(key)
                counts["unknown_terminal_sessions"] += 1
            else:
                floor += held * price
        result.sessions.append(session)
        result.nav.append(cash + market)
        result.floor_nav.append(cash + floor)
        result.cash.append(cash)
        result.holdings_count.append(len(shares))
        if cash < -1e-6:
            result.violations.append(f"cash went negative on {session.isoformat()}: {cash:.6f}")

    counts["unknown_terminal_identities"] = len(unknown_terminal)
    counts["settled_identities"] = len(settled)
    result.diagnostics = dict(sorted(counts.items()))
    _check_invariants(result)
    return result


def _is_terminal_unknown(
    book: PriceBook,
    key: tuple[str, int],
    position: int,
    terminal: Mapping[tuple[str, int], TerminalLeg],
) -> bool:
    if book.close_at(key, position) is not None:
        return False
    if book.has_later_bar(key, position):
        return False
    leg = terminal.get(key)
    return leg is None or leg.state is TerminalState.UNKNOWN


def _valuation_price(
    book: PriceBook,
    key: tuple[str, int],
    position: int,
    carried: dict[tuple[str, int], float],
    terminal: Mapping[tuple[str, int], TerminalLeg],
) -> tuple[float | None, str]:
    price = book.close_at(key, position)
    if price is not None:
        carried[key] = price
        return price, "BAR"
    last = book.last_close_on_or_before(key, position)
    if last is not None:
        return last, "CARRY"
    return carried.get(key), "CARRY"


def _rebalance(
    plan: Rebalance,
    book: PriceBook,
    position: int,
    shares: dict[tuple[str, int], float],
    carried: dict[tuple[str, int], float],
    cash: float,
    cost: CostModel,
    symbols: Mapping[tuple[str, int], str],
    counts: dict[str, int],
    result: SimulationResult,
) -> tuple[float, float]:
    """Execute one target portfolio at the open. Sells first, then buys."""
    prices: dict[tuple[str, int], float] = {}
    for key in set(shares) | set(plan.targets):
        opening = book.open_at(key, position)
        if opening is not None and opening > 0:
            prices[key] = opening

    nav = cash
    for key, held in shares.items():
        price = prices.get(key) or book.last_close_on_or_before(key, position) or carried.get(key)
        if price is None:
            counts["unvaluable_at_rebalance"] += 1
            continue
        nav += held * price
    if nav <= 0:
        result.violations.append(f"non-positive NAV at {plan.execution.isoformat()}")
        return cash, 0.0

    traded_notional = 0.0
    orders: list[tuple[tuple[str, int], float, float]] = []
    for key in sorted(set(shares) | set(plan.targets)):
        weight = plan.targets.get(key, 0.0)
        price = prices.get(key)
        held = shares.get(key, 0.0)
        if price is None:
            if held > 0:
                counts["unsaleable_carry"] += 1
            elif weight > 0:
                counts["unpriced_not_executed"] += 1
            continue
        desired_shares = (nav * weight) / price
        orders.append((key, desired_shares - held, price))

    for side in ("SELL", "BUY"):
        for key, delta, price in orders:
            if (delta >= 0) if side == "SELL" else (delta <= 0):
                continue
            quantity = abs(delta)
            notional = quantity * price
            if side == "BUY":
                charge = cost.charge(notional)
                if notional + charge > cash:
                    affordable = max(cash / (1.0 + cost.bps_per_side / 10_000.0), 0.0)
                    if affordable <= 0:
                        counts["cash_exhausted"] += 1
                        continue
                    quantity = affordable / price
                    notional = quantity * price
                    charge = cost.charge(notional)
                cash -= notional + charge
                shares[key] = shares.get(key, 0.0) + quantity
            else:
                charge = cost.charge(notional)
                cash += notional - charge
                remaining = shares.get(key, 0.0) - quantity
                if remaining <= 1e-12:
                    shares.pop(key, None)
                else:
                    shares[key] = remaining
            traded_notional += notional
            result.trades.append(
                Trade(
                    month=plan.month,
                    decision=plan.decision.isoformat(),
                    execution=plan.execution.isoformat(),
                    kind=key[0],
                    identity_id=key[1],
                    symbol=symbols.get(key, ""),
                    side=side,
                    shares=quantity,
                    price=price,
                    notional=notional,
                    cost=charge,
                    reason=plan.split,
                )
            )

    after = cash + sum(
        held * (prices.get(key) or book.last_close_on_or_before(key, position) or 0.0)
        for key, held in shares.items()
    )
    for key in sorted(shares):
        price = prices.get(key) or book.last_close_on_or_before(key, position) or 0.0
        value = shares[key] * price
        result.holdings.append(
            Holding(
                month=plan.month,
                execution=plan.execution.isoformat(),
                kind=key[0],
                identity_id=key[1],
                symbol=symbols.get(key, ""),
                shares=shares[key],
                price=price,
                market_value=value,
                target_weight=plan.targets.get(key, 0.0),
                actual_weight=value / after if after > 0 else 0.0,
                basis="BAR" if key in prices else "CARRY",
            )
        )
    return cash, traded_notional / (2.0 * nav)


def _check_invariants(result: SimulationResult) -> None:
    """Fail closed on anything a portfolio must never do."""
    for position, cash in enumerate(result.cash):
        if cash < -1e-6:
            result.violations.append(f"negative cash at {result.sessions[position].isoformat()}")
        if result.nav[position] <= 0:
            result.violations.append(f"non-positive NAV at {result.sessions[position].isoformat()}")
    for holding in result.holdings:
        if holding.target_weight < 0 or holding.target_weight > 1.0 + 1e-9:
            result.violations.append(
                f"target weight {holding.target_weight} out of bounds for "
                f"{holding.kind}:{holding.identity_id}"
            )
        if holding.shares < 0:
            result.violations.append(f"short position in {holding.kind}:{holding.identity_id}")
