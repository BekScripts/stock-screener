"""Durable identity, a position, and the portfolio as it actually stands.

The identity rule is inherited rather than invented. The research layer keys
every security on `(kind, identity_id)` and treats a ticker as a dated display
attribute, because a symbol is reassigned when a company delists and whoever
inherits it is a different company. That mistake is cheap to make and expensive
to find, so `SecurityIdentity` has no `__eq__` on its symbol and the symbol never
reaches a dictionary key.

A `PortfolioSnapshot` is a statement about one moment: these positions, this
cash, these marks. It carries its own fingerprint so a decision can bind to the
portfolio it was computed against, and it refuses to exist in states a portfolio
cannot be in — a negative NAV, a mark that is not finite, two positions claiming
the same identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from stock_screener.backtest_harness.universe import COMPANY, HISTORICAL

from .arithmetic import EXPOSURE_TOLERANCE, quantise
from .fingerprint import digest

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "KINDS",
    "PortfolioSnapshot",
    "PortfolioStateError",
    "Position",
    "SecurityIdentity",
]

#: The kinds the research layer recognises. A kind outside this set is refused
#: rather than passed through, because the portfolio layer cannot price something
#: whose identity class it does not know.
KINDS: frozenset[str] = frozenset({COMPANY, HISTORICAL})


class PortfolioStateError(ValueError):
    """Raised when a portfolio or position could not be in the state described."""


@dataclass(frozen=True, order=True)
class SecurityIdentity:
    """A security, as the whole system addresses it.

    Ordered by `(kind, identity_id)` so every deterministic tie-break in this
    package can sort identities without restating the rule.

    Attributes:
        kind: `COMPANY` or `HISTORICAL`.
        identity_id: The durable primary key within that kind.
        symbol: The ticker, for display only. Never part of equality, ordering or
            any key — a reassigned symbol would silently merge two companies.
    """

    kind: str
    identity_id: int
    symbol: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        """Refuse an identity the research layer would not recognise.

        Raises:
            PortfolioStateError: If the kind is unknown or the id is not positive.
        """
        if self.kind not in KINDS:
            raise PortfolioStateError(
                f"unknown identity kind {self.kind!r}; expected one of {sorted(KINDS)}"
            )
        if self.identity_id <= 0:
            raise PortfolioStateError(
                f"identity_id must be a positive primary key, got {self.identity_id!r}"
            )

    @property
    def key(self) -> tuple[str, int]:
        """The durable key, with no symbol in it."""
        return (self.kind, self.identity_id)

    @property
    def label(self) -> str:
        """A stable string form for serialisation, `KIND:id`."""
        return f"{self.kind}:{self.identity_id}"

    def as_json(self) -> dict[str, Any]:
        """Return the identity as a JSON-safe mapping."""
        return {"kind": self.kind, "identity_id": self.identity_id, "symbol": self.symbol}


@dataclass(frozen=True)
class Position:
    """One held security, marked at a price.

    Tax lots are deliberately absent: a lot-level model changes what a position
    *is* and would have to be threaded through every weight in this layer, so it
    belongs to a later phase that needs it rather than to this one that does not.

    Attributes:
        identity: The security.
        quantity: Units held. Negative represents a short, which the baseline
            policy does not enable but the model can express.
        mark_price: The price the position is marked at.
        cost_basis: Average cost per unit, when known. Never used in a weight.
        strategy_id: Which strategy opened it, when known. Attribution only.
    """

    identity: SecurityIdentity
    quantity: float
    mark_price: float
    cost_basis: float | None = None
    strategy_id: str | None = None

    def __post_init__(self) -> None:
        """Refuse a position that could not be held as described.

        Raises:
            PortfolioStateError: If the quantity or mark is not finite, or the
                mark is not positive.
        """
        for name in ("quantity", "mark_price"):
            value = getattr(self, name)
            try:
                quantise(float(value))
            except ValueError as exc:
                raise PortfolioStateError(f"{self.identity.label}: {name} {exc}") from exc
        if self.mark_price <= 0:
            raise PortfolioStateError(
                f"{self.identity.label}: a position must be marked at a positive price, "
                f"got {self.mark_price!r}. An unpriced position is unpriced, never zero."
            )

    @property
    def market_value(self) -> float:
        """Quantity times mark. Signed, so a short is negative."""
        return quantise(self.quantity * self.mark_price)

    def as_json(self) -> dict[str, Any]:
        """Return the position as a JSON-safe mapping."""
        return {
            "identity": self.identity.as_json(),
            "quantity": quantise(self.quantity),
            "mark_price": self.mark_price,
            "market_value": self.market_value,
            "cost_basis": self.cost_basis,
            "strategy_id": self.strategy_id,
        }


@dataclass(frozen=True)
class PortfolioSnapshot:
    """The portfolio as it stands at one moment.

    Attributes:
        as_of: The moment this describes, ISO 8601 with offset.
        cash: Uninvested cash. May not be negative: this layer has no margin.
        positions: The holdings, in identity order.
        price_fingerprint: The digest of the price source the marks came from, so
            a decision can say what it was marked against.
    """

    as_of: str
    cash: float
    positions: tuple[Position, ...] = ()
    price_fingerprint: str | None = None

    def __post_init__(self) -> None:
        """Refuse a portfolio that could not exist.

        Raises:
            PortfolioStateError: On negative cash, a duplicated identity, or a
                non-positive NAV.
        """
        if self.cash < 0:
            raise PortfolioStateError(
                f"cash may not be negative ({self.cash!r}); this layer has no margin"
            )
        seen: set[tuple[str, int]] = set()
        for position in self.positions:
            if position.identity.key in seen:
                raise PortfolioStateError(
                    f"{position.identity.label} appears twice; a portfolio holds one "
                    "position per identity, and two rows for one security is a "
                    "reconciliation failure rather than a larger position"
                )
            seen.add(position.identity.key)
        if self.nav <= 0:
            raise PortfolioStateError(
                f"net asset value must be positive, got {self.nav!r}; weights are "
                "undefined against a zero or negative NAV"
            )

    @property
    def nav(self) -> float:
        """Net asset value: cash plus the signed market value of every position."""
        return quantise(self.cash + sum(position.market_value for position in self.positions))

    @property
    def weights(self) -> dict[tuple[str, int], float]:
        """Current weight per identity, as a fraction of NAV."""
        nav = self.nav
        return {
            position.identity.key: quantise(position.market_value / nav)
            for position in sorted(self.positions, key=lambda row: row.identity)
        }

    @property
    def cash_weight(self) -> float:
        """Cash as a fraction of NAV."""
        return quantise(self.cash / self.nav)

    @property
    def gross_exposure(self) -> float:
        """Sum of absolute position weights."""
        return quantise(sum(abs(weight) for weight in self.weights.values()))

    @property
    def net_exposure(self) -> float:
        """Sum of signed position weights."""
        return quantise(sum(self.weights.values()))

    def identity_for(self, key: tuple[str, int]) -> SecurityIdentity | None:
        """Look up a held identity by its durable key.

        Args:
            key: `(kind, identity_id)`.

        Returns:
            The identity, or None when it is not held.
        """
        for position in self.positions:
            if position.identity.key == key:
                return position.identity
        return None

    def body(self) -> dict[str, Any]:
        """The fingerprinted body. Positions are sorted by identity.

        Returns:
            A JSON-safe mapping that is identical for identical holdings,
            regardless of the order they were supplied in.
        """
        return {
            "as_of": self.as_of,
            "cash": quantise(self.cash),
            "nav": self.nav,
            "cash_weight": self.cash_weight,
            "positions": [
                position.as_json() | {"weight": self.weights[position.identity.key]}
                for position in sorted(self.positions, key=lambda row: row.identity)
            ],
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "price_fingerprint": self.price_fingerprint,
        }

    def fingerprint(self) -> str:
        """The snapshot's deterministic digest."""
        return digest(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the snapshot with its fingerprint."""
        return {**self.body(), "fingerprint": self.fingerprint()}

    @classmethod
    def empty(cls, *, as_of: str, cash: float) -> PortfolioSnapshot:
        """An all-cash portfolio, for a first decision with no holdings.

        Args:
            as_of: The moment.
            cash: The starting cash.

        Returns:
            The snapshot.
        """
        return cls(as_of=as_of, cash=cash, positions=())


def weights_sum_to_one(weights: Sequence[float], cash_weight: float) -> bool:
    """Whether position weights plus cash account for the whole portfolio.

    Args:
        weights: The position weights.
        cash_weight: The cash weight.

    Returns:
        True when the total is 1 within `EXPOSURE_TOLERANCE`.
    """
    return abs(sum(weights) + cash_weight - 1.0) <= EXPOSURE_TOLERANCE
