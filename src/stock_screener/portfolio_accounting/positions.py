"""Position state under weighted-average cost, with strategy attribution.

Cost basis is the number everything downstream depends on: realised P&L is the
difference between proceeds and basis, and unrealised P&L is the difference
between the mark and basis. Getting it wrong does not produce an error — it
produces a plausible, wrong performance figure, which is worse.

Two policy choices are made here explicitly rather than inherited from whatever
the arithmetic happened to do:

**Fees are expensed, never capitalised.** A commission paid to buy is a cost of
the period, not part of what the shares cost. Capitalising it would hide the fee
inside the basis, where it would re-emerge later as a smaller realised gain and
be invisible as a cost. `WEIGHTED_AVERAGE_COST_V1` names this so a future
policy that capitalises them is a new version rather than a silent edit.

**Attribution is tracked per strategy and never inferred.** When two strategies
hold the same security, a sale has to say whose shares were sold; splitting it
pro rata would be a guess that quietly moves P&L between strategies. A legacy
holding whose owner is genuinely unknown is attributed to `UNATTRIBUTED_EXISTING`
and stays there until something says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .errors import AccountingError, Code
from .events import UNATTRIBUTED, StrategySlice
from .money import ZERO, is_zero, quantise_cash, quantise_price, quantise_quantity

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from decimal import Decimal

    from stock_screener.portfolio_risk import SecurityIdentity

__all__ = [
    "COST_BASIS_POLICY",
    "PositionState",
    "SaleAllocation",
    "allocate_sale",
]

#: The cost-basis contract. Fees are expensed; a policy that capitalises them, or
#: one that uses FIFO or specific-lot identification, is a different version.
COST_BASIS_POLICY = "WEIGHTED_AVERAGE_COST_V1"


@dataclass(frozen=True)
class PositionState:
    """One holding, as the ledger has established it.

    Attributes:
        identity: The security.
        quantity: Shares held. Never negative: the long-only baseline refuses a
            short rather than representing one.
        total_cost: What the held shares cost, excluding fees.
        attribution: Shares owned per strategy. Sums to `quantity`.
        realised_pnl: Cumulative realised gain or loss on this security, gross
            of fees. Fees are a separate P&L term, so they are not netted here.
        fees_paid: Cumulative transaction costs on this security.
        dividends_received: Cumulative cash dividends credited for it.
    """

    identity: SecurityIdentity
    quantity: Decimal = ZERO
    total_cost: Decimal = ZERO
    attribution: Mapping[str, Decimal] = field(default_factory=dict)
    realised_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO
    dividends_received: Decimal = ZERO

    @property
    def average_cost(self) -> Decimal:
        """Cost per share held, or zero for a closed position.

        A closed position's average cost is undefined rather than zero in
        principle; zero is returned because nothing may multiply by it while the
        quantity is zero, and the alternative is a `None` that every caller would
        have to re-handle.
        """
        if is_zero(self.quantity):
            return ZERO
        return quantise_price(self.total_cost / self.quantity)

    @property
    def is_open(self) -> bool:
        """Whether any shares are held."""
        return not is_zero(self.quantity)

    @property
    def owners(self) -> tuple[str, ...]:
        """The strategies holding shares, in a deterministic order."""
        return tuple(sorted(key for key, value in self.attribution.items() if not is_zero(value)))

    def attribution_slices(self) -> tuple[StrategySlice, ...]:
        """The attribution as ordered slices."""
        return tuple(
            StrategySlice(strategy_id=key, quantity=self.attribution[key]) for key in self.owners
        )

    def check_attribution(self) -> None:
        """Verify attribution still sums to the quantity.

        Raises:
            AccountingError: If the slices disagree with the position. This is
                an internal invariant, and a mismatch means a bug rather than
                bad input, so it is checked after every mutation.
        """
        total = quantise_quantity(sum(self.attribution.values(), ZERO))
        if total != quantise_quantity(self.quantity):
            raise AccountingError(
                Code.ATTRIBUTION_RECONCILIATION_FAILED,
                f"{self.identity.label}: attribution sums to {total} but the position "
                f"holds {self.quantity}",
            )

    def buy(
        self,
        *,
        quantity: Decimal,
        price: Decimal,
        fees: Decimal,
        attribution: Sequence[StrategySlice],
    ) -> PositionState:
        """Apply a purchase.

        Args:
            quantity: Shares bought.
            price: Price per share.
            fees: Transaction costs, expensed rather than capitalised.
            attribution: Who the shares belong to. Empty attributes them to
                `UNATTRIBUTED_EXISTING`, which is honest about not knowing.

        Returns:
            The position after the purchase.
        """
        slices = attribution or (StrategySlice(strategy_id=UNATTRIBUTED, quantity=quantity),)
        owners = dict(self.attribution)
        for row in slices:
            owners[row.strategy_id] = quantise_quantity(
                owners.get(row.strategy_id, ZERO) + row.quantity
            )
        after = replace(
            self,
            quantity=quantise_quantity(self.quantity + quantity),
            total_cost=quantise_cash(self.total_cost + quantity * price),
            attribution=owners,
            fees_paid=quantise_cash(self.fees_paid + fees),
        )
        after.check_attribution()
        return after

    def sell(
        self,
        *,
        quantity: Decimal,
        price: Decimal,
        fees: Decimal,
        attribution: Sequence[StrategySlice] = (),
    ) -> tuple[PositionState, SaleAllocation]:
        """Apply a sale.

        Args:
            quantity: Shares sold.
            price: Price per share.
            fees: Transaction costs. Expensed as their own term, not netted
                into the realised result.
            attribution: Whose shares were sold. Required when more than one
                strategy holds the security.

        Returns:
            The position after the sale, and the realised result.

        Raises:
            AccountingError: `INSUFFICIENT_POSITION` if more shares were sold
                than held; `SELL_ATTRIBUTION_REQUIRED` if ownership is ambiguous;
                `ATTRIBUTION_EXCEEDS_SLICE` if a strategy sold more than it held.
        """
        held = quantise_quantity(self.quantity)
        sold = quantise_quantity(quantity)
        if sold > held:
            raise AccountingError(
                Code.INSUFFICIENT_POSITION,
                f"{self.identity.label}: the sale is for {sold} shares but the ledger "
                f"holds {held}. Allowing it would create a short position the "
                "long-only baseline does not support, and the shortfall is more "
                "likely a missing fill than a real short.",
            )
        slices = self._sale_slices(sold, attribution)
        average = self.average_cost
        basis = quantise_cash(sold * average)
        proceeds = quantise_cash(sold * price)
        # Gross of fees, deliberately. Fees are expensed as their own P&L term,
        # so netting them in here too would charge them twice on a sale and not
        # at all on a purchase — which is exactly the asymmetry that breaks the
        # NAV-change identity for a portfolio that has only ever bought.
        realised = quantise_cash(proceeds - basis)

        owners = dict(self.attribution)
        for row in slices:
            owners[row.strategy_id] = quantise_quantity(
                owners.get(row.strategy_id, ZERO) - row.quantity
            )
        owners = {key: value for key, value in owners.items() if not is_zero(value)}
        remaining = quantise_quantity(held - sold)
        after = replace(
            self,
            quantity=remaining,
            # Residual arithmetic is avoided deliberately: the remaining cost is
            # the remaining shares at the average that applied, so closing a
            # position leaves exactly zero rather than a rounding crumb that
            # would look like a free share.
            total_cost=quantise_cash(remaining * average),
            attribution=owners,
            realised_pnl=quantise_cash(self.realised_pnl + realised),
            fees_paid=quantise_cash(self.fees_paid + fees),
        )
        after.check_attribution()
        allocation = SaleAllocation(
            identity=self.identity,
            quantity=sold,
            proceeds=proceeds,
            cost_basis_released=basis,
            fees=quantise_cash(fees),
            realised_pnl=realised,
            per_strategy=tuple(
                StrategySlice(
                    strategy_id=row.strategy_id,
                    quantity=row.quantity,
                )
                for row in slices
            ),
            realised_per_strategy=_split_realised(slices, sold, realised),
        )
        return after, allocation

    def _sale_slices(
        self, sold: Decimal, attribution: Sequence[StrategySlice]
    ) -> tuple[StrategySlice, ...]:
        """Decide which strategies' shares a sale consumed.

        Args:
            sold: Shares sold.
            attribution: What the caller supplied.

        Returns:
            The slices to debit.

        Raises:
            AccountingError: If ownership is ambiguous and unstated, if the
                slices do not sum to the sale, or if a strategy sold more than
                it holds.
        """
        if not attribution:
            owners = self.owners
            if len(owners) > 1:
                raise AccountingError(
                    Code.SELL_ATTRIBUTION_REQUIRED,
                    f"{self.identity.label} is held by {len(owners)} strategies "
                    f"({', '.join(owners)}) and the sale does not say whose shares "
                    "were sold. A pro-rata split would be a guess that moves realised "
                    "P&L between strategies, so the sale is refused instead.",
                )
            if not owners:
                raise AccountingError(
                    Code.INSUFFICIENT_POSITION,
                    f"{self.identity.label}: nothing is held, so there is nothing to sell",
                )
            return (StrategySlice(strategy_id=owners[0], quantity=sold),)
        total = quantise_quantity(sum((row.quantity for row in attribution), ZERO))
        if total != sold:
            raise AccountingError(
                Code.ATTRIBUTION_DOES_NOT_SUM,
                f"{self.identity.label}: the sale's attribution sums to {total} but "
                f"{sold} shares were sold",
            )
        for row in attribution:
            available = quantise_quantity(self.attribution.get(row.strategy_id, ZERO))
            if row.quantity > available:
                raise AccountingError(
                    Code.ATTRIBUTION_EXCEEDS_SLICE,
                    f"{self.identity.label}: {row.strategy_id} is recorded as selling "
                    f"{row.quantity} shares but holds {available}. Borrowing another "
                    "strategy's shares to make the sale balance would misstate both.",
                )
        return tuple(sorted(attribution, key=lambda row: row.strategy_id))

    def credit_dividend(self, amount: Decimal) -> PositionState:
        """Record a cash dividend against this security.

        Args:
            amount: The credit.

        Returns:
            The position with the dividend recorded. Cost basis is untouched: a
            dividend is income, and reducing basis by it would be a return-of-
            capital treatment this engine does not claim to support.
        """
        return replace(self, dividends_received=quantise_cash(self.dividends_received + amount))

    def apply_split(self, *, numerator: Decimal, denominator: Decimal) -> PositionState:
        """Apply a split ratio.

        Quantity is multiplied and total cost is left alone, which is what makes
        the split economically neutral: the same money now buys a different
        number of shares, and the average cost falls by exactly the ratio.
        Attribution scales with the quantity, since a split does not change who
        owns what.

        Args:
            numerator: New shares per `denominator` old shares.
            denominator: The old share count the ratio is against.

        Returns:
            The position after the split.
        """
        ratio = numerator / denominator
        owners = {key: quantise_quantity(value * ratio) for key, value in self.attribution.items()}
        scaled = quantise_quantity(self.quantity * ratio)
        drift = quantise_quantity(scaled - sum(owners.values(), ZERO))
        if not is_zero(drift) and owners:
            # Quantising each slice can leave a sub-unit remainder. It is given
            # to the largest holder rather than dropped, because dropping it
            # would make attribution stop summing to the position.
            largest = max(sorted(owners), key=lambda key: owners[key])
            owners[largest] = quantise_quantity(owners[largest] + drift)
        after = replace(self, quantity=scaled, attribution=owners)
        after.check_attribution()
        return after

    def as_json(self) -> dict[str, Any]:
        """Return the position as a JSON-safe mapping."""
        return {
            "identity": self.identity.as_json(),
            "label": self.identity.label,
            "quantity": str(quantise_quantity(self.quantity)),
            "average_cost": str(self.average_cost),
            "total_cost": str(quantise_cash(self.total_cost)),
            "cost_basis_policy": COST_BASIS_POLICY,
            "attribution": [row.as_json() for row in self.attribution_slices()],
            "realised_pnl": str(quantise_cash(self.realised_pnl)),
            "fees_paid": str(quantise_cash(self.fees_paid)),
            "dividends_received": str(quantise_cash(self.dividends_received)),
        }


def _split_realised(
    slices: Sequence[StrategySlice], sold: Decimal, realised: Decimal
) -> tuple[dict[str, str], ...]:
    """Attribute a realised result across the slices that produced it.

    Under weighted-average cost every share carries the same basis, so realised
    P&L divides by quantity and nothing is being estimated. The last slice takes
    any rounding remainder so the parts sum to the whole.

    Args:
        slices: The debited slices.
        sold: Total shares sold.
        realised: The realised result.

    Returns:
        One entry per strategy.
    """
    if is_zero(sold) or not slices:
        return ()
    rows: list[dict[str, str]] = []
    running = ZERO
    for index, row in enumerate(slices):
        if index == len(slices) - 1:
            share = quantise_cash(realised - running)
        else:
            share = quantise_cash(realised * row.quantity / sold)
            running = quantise_cash(running + share)
        rows.append({"strategy_id": row.strategy_id, "realised_pnl": str(share)})
    return tuple(rows)


@dataclass(frozen=True)
class SaleAllocation:
    """What a sale realised, and whose it was.

    Attributes:
        identity: The security sold.
        quantity: Shares sold.
        proceeds: Gross proceeds, before fees.
        cost_basis_released: The basis the sold shares carried.
        fees: Transaction costs on the sale, expensed separately.
        realised_pnl: `proceeds - cost_basis_released`, gross of fees.
        per_strategy: The quantity debited from each strategy.
        realised_per_strategy: The realised result split across them.
    """

    identity: SecurityIdentity
    quantity: Decimal
    proceeds: Decimal
    cost_basis_released: Decimal
    fees: Decimal
    realised_pnl: Decimal
    per_strategy: tuple[StrategySlice, ...]
    realised_per_strategy: tuple[dict[str, str], ...]

    def as_json(self) -> dict[str, Any]:
        """Return the allocation as a JSON-safe mapping."""
        return {
            "identity": self.identity.label,
            "quantity": str(self.quantity),
            "proceeds": str(self.proceeds),
            "cost_basis_released": str(self.cost_basis_released),
            "fees": str(self.fees),
            "realised_pnl": str(self.realised_pnl),
            "per_strategy": [row.as_json() for row in self.per_strategy],
            "realised_per_strategy": [dict(row) for row in self.realised_per_strategy],
        }


def allocate_sale(
    position: PositionState,
    *,
    quantity: Decimal,
    price: Decimal,
    fees: Decimal,
    attribution: Sequence[StrategySlice] = (),
) -> tuple[PositionState, SaleAllocation]:
    """Apply a sale to a position.

    Args:
        position: The holding before the sale.
        quantity: Shares sold.
        price: Price per share.
        fees: Transaction costs.
        attribution: Whose shares were sold.

    Returns:
        The position after the sale, and the realised result.
    """
    return position.sell(quantity=quantity, price=price, fees=fees, attribution=attribution)
