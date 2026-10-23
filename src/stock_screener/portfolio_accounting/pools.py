"""Per-attribution cost pools: each owner carries its own weighted-average basis.

P1.3 kept one weighted-average basis per security and divided it among owners by
quantity. That is exact when co-owners entered at the same price and misleading
when they did not:

    A buys 1 @ 10, B buys 1 @ 20  ->  pooled average 15
    A sells its share @ 30        ->  pooled basis reports 15 realised
                                      A economically realised 20

The error is not confined to attribution. Because the pooled average also decides
what the *remaining* basis is, global realised P&L and global cost basis move too
— 15 against 20 in that example on both lines. That is why this is a new major
accounting version rather than an additive one: the portfolio's own figures
change, not merely their division.

The fix here is deliberately not tax lots. There is no FIFO, no LIFO, no specific
identification, no holding period and no wash-sale rule — those answer a tax
question this engine does not ask, and each would change what a position *is*.
Instead each `(identity, owner)` pair keeps its own weighted-average pool. Within
a pool the arithmetic is exactly P1.3's; what changed is that there is one pool
per owner rather than one per security.

**The pools are the single authority.** Global quantity, global cost basis and
global realised P&L are *derived* by summing them, never maintained alongside
them. Two independently mutating authorities would eventually disagree, and the
disagreement would surface as a reconciliation failure whose cause is this module.
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
    "PER_ATTRIBUTION_COST_POLICY",
    "AttributionCostPool",
    "PoolSaleAllocation",
    "PooledPosition",
]

#: The P1.4 cost-basis contract. Named so a change to it — and in particular any
#: move toward lot selection — is a new version rather than an edit.
PER_ATTRIBUTION_COST_POLICY = "PER_ATTRIBUTION_WEIGHTED_AVERAGE_COST_V1"


@dataclass(frozen=True)
class AttributionCostPool:
    """One owner's holding of one security, with its own basis.

    Attributes:
        identity: The security.
        owner: The attribution owner — a `strategy_id`, or
            `UNATTRIBUTED_EXISTING` where ownership is genuinely unknown. Never
            a ticker: a reassigned symbol would merge two owners' books.
        quantity: Shares this owner holds.
        total_cost_basis: What those shares cost this owner, excluding fees.
        realised_trading_pnl_gross: Cumulative realised result on this
            `(identity, owner)` pair, gross of fees. Retained after the pool
            closes, because a strategy that realised a loss here still realised
            it and relabelling that history would lose the fact.
        fees_paid: Transaction costs charged to this owner.
        dividends_received: Dividends credited to this owner.
        closed_quantity: Cumulative shares this owner has sold. Kept so a closed
            pool can still show it was once held rather than looking like a pool
            that never existed.
    """

    identity: SecurityIdentity
    owner: str
    quantity: Decimal = ZERO
    total_cost_basis: Decimal = ZERO
    realised_trading_pnl_gross: Decimal = ZERO
    fees_paid: Decimal = ZERO
    dividends_received: Decimal = ZERO
    closed_quantity: Decimal = ZERO

    def __post_init__(self) -> None:
        """Refuse a pool that could not exist under long-only semantics.

        Raises:
            AccountingError: On a missing owner, a negative quantity, or a
                negative cost basis.
        """
        if not self.owner:
            raise AccountingError(
                Code.EVENT_INVALID, f"{self.identity.label}: a cost pool must name its owner"
            )
        if quantise_quantity(self.quantity) < ZERO:
            raise AccountingError(
                Code.SHORT_POSITION_NOT_SUPPORTED,
                f"{self.identity.label}/{self.owner}: a cost pool holds "
                f"{quantise_quantity(self.quantity)}; the long-only baseline cannot "
                "represent a short, so a negative pool means an event was applied "
                "that should have been refused",
            )
        if quantise_cash(self.total_cost_basis) < ZERO:
            raise AccountingError(
                Code.EVENT_INVALID,
                f"{self.identity.label}/{self.owner}: cost basis may not be negative "
                f"({quantise_cash(self.total_cost_basis)})",
            )

    @property
    def average_cost(self) -> Decimal:
        """Cost per share for this owner, or zero when the pool is closed.

        A closed pool's average cost is undefined rather than zero in principle.
        Zero is returned because nothing may multiply by it while the quantity is
        zero, and a `None` here would have to be re-handled by every caller.
        """
        if is_zero(self.quantity):
            return ZERO
        return quantise_price(self.total_cost_basis / self.quantity)

    @property
    def is_open(self) -> bool:
        """Whether this owner holds any shares."""
        return not is_zero(self.quantity)

    @property
    def has_history(self) -> bool:
        """Whether this pool is worth keeping once closed.

        A pool that ever traded carries realised P&L, fees or dividends that
        belong to its owner. One that never did is an artefact of being asked
        about and is dropped.
        """
        return (
            self.is_open
            or self.realised_trading_pnl_gross != ZERO
            or self.fees_paid != ZERO
            or self.dividends_received != ZERO
            or self.closed_quantity != ZERO
        )

    def market_value(self, mark: Decimal) -> Decimal:
        """This owner's holding at a mark.

        Args:
            mark: The price per share.

        Returns:
            `quantity * mark`, quantised to cash.
        """
        return quantise_cash(self.quantity * mark)

    def unrealised_pnl(self, mark: Decimal) -> Decimal:
        """This owner's mark-to-market result.

        Computed as `market value - total_cost_basis` rather than
        `quantity * (mark - average_cost)`. The two agree whenever the average
        divides exactly and differ by a quantisation residue when it does not,
        because `average_cost` is derived at eight places while
        `total_cost_basis` is authoritative. Using the authoritative figure is
        what makes the portfolio-level identity close exactly instead of to
        within a rounding crumb — a crumb that would otherwise accumulate across
        every pool and surface as a false P&L decomposition failure.

        Args:
            mark: The price per share.

        Returns:
            The unrealised result.
        """
        return quantise_cash(self.market_value(mark) - self.total_cost_basis)

    def key(self) -> tuple[str, int, str]:
        """The pool's durable key: `(kind, identity_id, owner)`."""
        return (self.identity.kind, self.identity.identity_id, self.owner)

    def buy(self, *, quantity: Decimal, price: Decimal, fees: Decimal) -> AttributionCostPool:
        """Add shares to this owner's pool at a weighted average.

        Args:
            quantity: Shares bought for this owner.
            price: Execution price per share.
            fees: Transaction costs charged to this owner. Expensed, never
                capitalised into the basis — the P1.3 policy, preserved.

        Returns:
            The pool after the purchase.
        """
        return replace(
            self,
            quantity=quantise_quantity(self.quantity + quantity),
            total_cost_basis=quantise_cash(self.total_cost_basis + quantity * price),
            fees_paid=quantise_cash(self.fees_paid + fees),
        )

    def sell(
        self, *, quantity: Decimal, price: Decimal, fees: Decimal
    ) -> tuple[AttributionCostPool, Decimal, Decimal]:
        """Remove shares from this owner's pool at this owner's basis.

        Args:
            quantity: Shares sold from this pool.
            price: Execution price per share.
            fees: Transaction costs charged to this owner.

        Returns:
            The pool after the sale, the gross proceeds, and the basis released.

        Raises:
            AccountingError: `INSUFFICIENT_ATTRIBUTED_POSITION` if this owner
                does not hold the shares. The security's *global* quantity may
                well be sufficient; taking the shortfall from a co-owner would
                silently move one strategy's position and P&L into another's.
        """
        held = quantise_quantity(self.quantity)
        sold = quantise_quantity(quantity)
        if sold > held:
            raise AccountingError(
                Code.INSUFFICIENT_ATTRIBUTED_POSITION,
                f"{self.identity.label}: {self.owner} is recorded as selling {sold} "
                f"shares but holds {held}. The security's global quantity may be "
                "sufficient; taking the shortfall from a co-owner would move one "
                "strategy's position and realised P&L into another's on no evidence.",
            )
        average = self.average_cost
        remaining = quantise_quantity(held - sold)
        basis_released = quantise_cash(sold * average)
        proceeds = quantise_cash(sold * price)
        realised = quantise_cash(proceeds - basis_released)
        after = replace(
            self,
            quantity=remaining,
            # The remaining shares at the average that applied, rather than a
            # subtraction, so closing a pool leaves exactly zero instead of a
            # rounding crumb that would read as a free share.
            total_cost_basis=quantise_cash(remaining * average),
            realised_trading_pnl_gross=quantise_cash(self.realised_trading_pnl_gross + realised),
            fees_paid=quantise_cash(self.fees_paid + fees),
            closed_quantity=quantise_quantity(self.closed_quantity + sold),
        )
        return after, proceeds, basis_released

    def apply_split(self, *, numerator: Decimal, denominator: Decimal) -> AttributionCostPool:
        """Scale this pool by a split ratio.

        Quantity multiplies and total cost does not, so this owner's average cost
        moves by exactly the ratio and no gain or loss is created for it.

        Args:
            numerator: New shares per `denominator` old shares.
            denominator: The old share count the ratio is against.

        Returns:
            The pool after the split.
        """
        ratio = numerator / denominator
        return replace(self, quantity=quantise_quantity(self.quantity * ratio))

    def credit_dividend(self, amount: Decimal) -> AttributionCostPool:
        """Credit a dividend to this owner.

        Args:
            amount: The credit.

        Returns:
            The pool with the dividend recorded. Cost basis is untouched: a
            dividend is income, and reducing basis by it would be the
            return-of-capital treatment this engine refuses to guess at.
        """
        return replace(self, dividends_received=quantise_cash(self.dividends_received + amount))

    def as_json(self) -> dict[str, Any]:
        """Return the pool as a JSON-safe mapping."""
        return {
            "identity": self.identity.label,
            "owner": self.owner,
            "quantity": str(quantise_quantity(self.quantity)),
            "average_cost": str(self.average_cost),
            "total_cost_basis": str(quantise_cash(self.total_cost_basis)),
            "realised_trading_pnl_gross": str(quantise_cash(self.realised_trading_pnl_gross)),
            "fees_paid": str(quantise_cash(self.fees_paid)),
            "dividends_received": str(quantise_cash(self.dividends_received)),
            "closed_quantity": str(quantise_quantity(self.closed_quantity)),
            "is_open": self.is_open,
            "is_unattributed": self.owner == UNATTRIBUTED,
        }


@dataclass(frozen=True)
class PoolSaleAllocation:
    """What a sale realised, pool by pool.

    Attributes:
        identity: The security sold.
        quantity: Total shares sold.
        proceeds: Gross proceeds.
        cost_basis_released: Total basis released, summed across pools.
        fees: Transaction costs on the sale.
        realised_trading_pnl_gross: Total realised, gross of fees. Equal to the
            sum of the per-owner figures by construction, not by a second
            calculation.
        per_owner: One row per debited pool.
    """

    identity: SecurityIdentity
    quantity: Decimal
    proceeds: Decimal
    cost_basis_released: Decimal
    fees: Decimal
    realised_trading_pnl_gross: Decimal
    per_owner: tuple[dict[str, str], ...]

    @property
    def realised_pnl(self) -> Decimal:
        """The realised result, gross of fees.

        Named to match the V1 allocation so the event machine has one path
        through a sale rather than a branch on the accounting version.
        """
        return self.realised_trading_pnl_gross

    @property
    def per_strategy(self) -> tuple[StrategySlice, ...]:
        """The quantity debited from each owner."""
        return tuple(
            StrategySlice(
                strategy_id=str(row["owner"]),
                quantity=quantise_quantity(_decimal(row["quantity"])),
            )
            for row in self.per_owner
        )

    @property
    def realised_per_strategy(self) -> tuple[dict[str, str], ...]:
        """The realised result per owner, each from that owner's own basis.

        Unlike V1 this is not a division of one pooled figure: each entry was
        computed from its own pool and the total is their sum, so the parts and
        the whole agree by construction rather than by apportionment.
        """
        return tuple(
            {
                "strategy_id": str(row["owner"]),
                "realised_pnl": str(row["realised_trading_pnl_gross"]),
            }
            for row in self.per_owner
        )

    def as_json(self) -> dict[str, Any]:
        """Return the allocation as a JSON-safe mapping."""
        return {
            "identity": self.identity.label,
            "quantity": str(self.quantity),
            "proceeds": str(self.proceeds),
            "cost_basis_released": str(self.cost_basis_released),
            "fees": str(self.fees),
            "realised_trading_pnl_gross": str(self.realised_trading_pnl_gross),
            "realised_pnl": str(self.realised_pnl),
            "per_owner": [dict(row) for row in self.per_owner],
            "basis_policy": PER_ATTRIBUTION_COST_POLICY,
        }


@dataclass(frozen=True)
class PooledPosition:
    """One security's holding, as the sum of its owners' pools.

    Every global figure on this class is derived. There is no second cost basis
    kept beside the pools and no global realised total maintained in parallel —
    those would be a second authority, and the two would eventually disagree in a
    way that looks like a reconciliation failure rather than a bug here.

    Attributes:
        identity: The security.
        pools: One pool per owner, keyed by owner id. Closed pools with history
            are retained.
    """

    identity: SecurityIdentity
    pools: Mapping[str, AttributionCostPool] = field(default_factory=dict)

    @property
    def quantity(self) -> Decimal:
        """Global shares held: the sum of the pools."""
        return quantise_quantity(sum((pool.quantity for pool in self.pools.values()), ZERO))

    @property
    def total_cost(self) -> Decimal:
        """Global cost basis: the sum of the pools."""
        return quantise_cash(sum((pool.total_cost_basis for pool in self.pools.values()), ZERO))

    @property
    def realised_pnl(self) -> Decimal:
        """Global realised result, gross of fees: the sum of the pools."""
        return quantise_cash(
            sum((pool.realised_trading_pnl_gross for pool in self.pools.values()), ZERO)
        )

    @property
    def fees_paid(self) -> Decimal:
        """Global fees on this security."""
        return quantise_cash(sum((pool.fees_paid for pool in self.pools.values()), ZERO))

    @property
    def dividends_received(self) -> Decimal:
        """Global dividends on this security."""
        return quantise_cash(sum((pool.dividends_received for pool in self.pools.values()), ZERO))

    @property
    def average_cost(self) -> Decimal:
        """Global cost per share, derived from the summed pools."""
        held = self.quantity
        if is_zero(held):
            return ZERO
        return quantise_price(self.total_cost / held)

    @property
    def is_open(self) -> bool:
        """Whether any owner holds shares."""
        return not is_zero(self.quantity)

    @property
    def owners(self) -> tuple[str, ...]:
        """Owners currently holding shares, in a deterministic order."""
        return tuple(sorted(key for key, pool in self.pools.items() if pool.is_open))

    @property
    def attribution(self) -> dict[str, Decimal]:
        """Open quantity per owner, for the aggregate invariants."""
        return {
            key: quantise_quantity(pool.quantity)
            for key, pool in sorted(self.pools.items())
            if pool.is_open
        }

    def attribution_slices(self) -> tuple[StrategySlice, ...]:
        """The open attribution as ordered slices."""
        return tuple(
            StrategySlice(strategy_id=key, quantity=self.pools[key].quantity) for key in self.owners
        )

    def market_value(self, mark: Decimal) -> Decimal:
        """Global market value: the sum of the pools at this mark.

        Summed from the pools rather than computed as `quantity * mark`, for the
        same reason the basis is: two quantisations of the parts need not equal
        one quantisation of the whole, and the pools are this design's single
        authority. Deriving the global figure from them keeps every identity
        exact by construction.

        Args:
            mark: The price per share.

        Returns:
            The market value.
        """
        return quantise_cash(sum((pool.market_value(mark) for pool in self.pools.values()), ZERO))

    def unrealised_pnl(self, mark: Decimal) -> Decimal:
        """Global unrealised result: the sum of the pools at this mark.

        Args:
            mark: The price per share.

        Returns:
            The unrealised result.
        """
        return quantise_cash(sum((pool.unrealised_pnl(mark) for pool in self.pools.values()), ZERO))

    def check_attribution(self) -> None:
        """Verify the pools still sum to the position.

        Present so a pooled position satisfies the same contract as the V1
        `PositionState` and the aggregate invariants need no version branch.

        Raises:
            AccountingError: If a derived global disagrees with its pools.
        """
        self.check_invariants()

    def open_pools(self) -> tuple[AttributionCostPool, ...]:
        """The pools currently holding shares, in owner order."""
        return tuple(self.pools[key] for key in self.owners)

    def retained_pools(self) -> tuple[AttributionCostPool, ...]:
        """Every pool worth reporting, open or closed, in owner order."""
        return tuple(self.pools[key] for key in sorted(self.pools) if self.pools[key].has_history)

    def check_invariants(self) -> None:
        """Verify the derived globals agree with the pools.

        These are tautologies given the properties above, and are checked anyway:
        the whole point of the design is that the pools are the only authority, so
        a failure here means something has written a global figure directly.

        Raises:
            AccountingError: `ATTRIBUTION_RECONCILIATION_FAILED` if a derived
                global does not equal the sum of its pools.
        """
        for label, derived, summed in (
            (
                "quantity",
                self.quantity,
                quantise_quantity(sum((p.quantity for p in self.pools.values()), ZERO)),
            ),
            (
                "cost basis",
                self.total_cost,
                quantise_cash(sum((p.total_cost_basis for p in self.pools.values()), ZERO)),
            ),
            (
                "realised",
                self.realised_pnl,
                quantise_cash(
                    sum((p.realised_trading_pnl_gross for p in self.pools.values()), ZERO)
                ),
            ),
        ):
            if derived != summed:
                raise AccountingError(
                    Code.ATTRIBUTION_RECONCILIATION_FAILED,
                    f"{self.identity.label}: global {label} is {derived} but its pools "
                    f"sum to {summed}; a global figure has been written directly "
                    "instead of derived",
                )

    def pool_for(self, owner: str) -> AttributionCostPool:
        """This owner's pool, empty if it has never held anything.

        Args:
            owner: The attribution owner.

        Returns:
            The pool.
        """
        return self.pools.get(owner) or AttributionCostPool(identity=self.identity, owner=owner)

    def _with_pool(self, pool: AttributionCostPool) -> PooledPosition:
        """Return the position with one pool replaced, dropping empty history."""
        pools = dict(self.pools)
        if pool.has_history:
            pools[pool.owner] = pool
        else:
            pools.pop(pool.owner, None)
        return replace(self, pools=pools)

    def buy(
        self,
        *,
        quantity: Decimal,
        price: Decimal,
        fees: Decimal,
        attribution: Sequence[StrategySlice],
    ) -> PooledPosition:
        """Apply a purchase to each named owner's pool.

        The same fill price enters each pool, but the prior basis in each differs,
        so each owner's average moves differently. That is the whole point.

        Args:
            quantity: Total shares bought.
            price: Execution price per share.
            fees: Transaction costs, apportioned across the owners by quantity.
            attribution: Who the shares belong to. Empty attributes the fill to
                `UNATTRIBUTED_EXISTING`, which is honest about not knowing.

        Returns:
            The position after the purchase.
        """
        slices = attribution or (StrategySlice(strategy_id=UNATTRIBUTED, quantity=quantity),)
        after = self
        ordered = sorted(slices, key=lambda row: row.strategy_id)
        total = quantise_quantity(sum((row.quantity for row in ordered), ZERO))
        apportioned = _apportion(fees, ordered, total)
        for row, fee_share in zip(ordered, apportioned, strict=True):
            after = after._with_pool(
                after.pool_for(row.strategy_id).buy(
                    quantity=row.quantity, price=price, fees=fee_share
                )
            )
        after.check_invariants()
        return after

    def sell(
        self,
        *,
        quantity: Decimal,
        price: Decimal,
        fees: Decimal,
        attribution: Sequence[StrategySlice] = (),
    ) -> tuple[PooledPosition, PoolSaleAllocation]:
        """Apply a sale, taking each owner's shares at that owner's basis.

        Args:
            quantity: Total shares sold.
            price: Execution price per share.
            fees: Transaction costs, apportioned across the debited owners.
            attribution: Whose shares were sold. Required when more than one
                owner holds the security.

        Returns:
            The position after the sale, and what it realised per owner.

        Raises:
            AccountingError: `INSUFFICIENT_POSITION` if the security does not
                hold the quantity; `SELL_ATTRIBUTION_REQUIRED` if ownership is
                ambiguous and unstated; `ATTRIBUTION_DOES_NOT_SUM` if the slices
                disagree with the sale; `INSUFFICIENT_ATTRIBUTED_POSITION` if a
                named owner does not hold its slice.
        """
        held = self.quantity
        sold = quantise_quantity(quantity)
        if sold > held:
            raise AccountingError(
                Code.INSUFFICIENT_POSITION,
                f"{self.identity.label}: the sale is for {sold} shares but the ledger "
                f"holds {held} across all owners. Allowing it would create a short "
                "position the long-only baseline does not support.",
            )
        slices = self._sale_slices(sold, attribution)
        ordered = sorted(slices, key=lambda row: row.strategy_id)
        apportioned = _apportion(fees, ordered, sold)

        after = self
        proceeds = ZERO
        released = ZERO
        realised_total = ZERO
        rows: list[dict[str, str]] = []
        for row, fee_share in zip(ordered, apportioned, strict=True):
            pool = after.pool_for(row.strategy_id)
            updated, pool_proceeds, pool_released = pool.sell(
                quantity=row.quantity, price=price, fees=fee_share
            )
            pool_realised = quantise_cash(pool_proceeds - pool_released)
            proceeds = quantise_cash(proceeds + pool_proceeds)
            released = quantise_cash(released + pool_released)
            realised_total = quantise_cash(realised_total + pool_realised)
            rows.append(
                {
                    "owner": row.strategy_id,
                    "quantity": str(quantise_quantity(row.quantity)),
                    "basis_per_share": str(pool.average_cost),
                    "cost_basis_released": str(pool_released),
                    "proceeds": str(pool_proceeds),
                    "realised_trading_pnl_gross": str(pool_realised),
                    "fees": str(fee_share),
                }
            )
            after = after._with_pool(updated)

        after.check_invariants()
        allocation = PoolSaleAllocation(
            identity=self.identity,
            quantity=sold,
            proceeds=proceeds,
            cost_basis_released=released,
            fees=quantise_cash(fees),
            realised_trading_pnl_gross=realised_total,
            per_owner=tuple(rows),
        )
        return after, allocation

    def _sale_slices(
        self, sold: Decimal, attribution: Sequence[StrategySlice]
    ) -> tuple[StrategySlice, ...]:
        """Decide which pools a sale consumed.

        Args:
            sold: Shares sold.
            attribution: What the caller supplied.

        Returns:
            The slices to debit.

        Raises:
            AccountingError: If ownership is ambiguous and unstated, or the
                slices do not sum to the sale.
        """
        if not attribution:
            owners = self.owners
            if len(owners) > 1:
                raise AccountingError(
                    Code.SELL_ATTRIBUTION_REQUIRED,
                    f"{self.identity.label} is held by {len(owners)} owners "
                    f"({', '.join(owners)}) and the sale does not say whose shares "
                    "were sold. Each owner has its own cost basis, so a pro-rata or "
                    "first-owner assumption would decide a realised result on no "
                    "evidence — the sale is refused instead.",
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
        return tuple(attribution)

    def apply_split(self, *, numerator: Decimal, denominator: Decimal) -> PooledPosition:
        """Apply a split ratio to every pool.

        Each pool scales independently, so total cost is unchanged both per owner
        and globally, and no owner gains or loses. The quantised remainder goes to
        the largest open pool rather than being dropped, so the pools keep summing
        to the position.

        Args:
            numerator: New shares per `denominator` old shares.
            denominator: The old share count the ratio is against.

        Returns:
            The position after the split.
        """
        ratio = numerator / denominator
        target = quantise_quantity(self.quantity * ratio)
        pools = {
            key: pool.apply_split(numerator=numerator, denominator=denominator)
            for key, pool in self.pools.items()
        }
        scaled = quantise_quantity(sum((pool.quantity for pool in pools.values()), ZERO))
        drift = quantise_quantity(target - scaled)
        open_keys = sorted(key for key, pool in pools.items() if pool.is_open)
        if not is_zero(drift) and open_keys:
            largest = max(open_keys, key=lambda key: pools[key].quantity)
            pools[largest] = replace(
                pools[largest],
                quantity=quantise_quantity(pools[largest].quantity + drift),
            )
        after = replace(self, pools=pools)
        after.check_invariants()
        return after

    def credit_dividend(
        self, amount: Decimal, *, attribution: Sequence[StrategySlice] = ()
    ) -> PooledPosition:
        """Credit a dividend, to named owners or to the unknown-owner bucket.

        Args:
            amount: The credit.
            attribution: Which owners earned it, where the event says. The
                quantities are read as weights over the stated owners.

        Returns:
            The position with the dividend recorded.

        Raises:
            AccountingError: If the stated attribution carries no quantity to
                weight by.
        """
        if not attribution:
            # The event states an aggregate entitlement and nothing more. Dividing
            # it across today's holders would be a guess about who held the shares
            # on the ex-date, so it is recorded as unattributed. Global dividend
            # income stays exact either way.
            return self._with_pool(self.pool_for(UNATTRIBUTED).credit_dividend(amount))
        ordered = sorted(attribution, key=lambda row: row.strategy_id)
        total = quantise_quantity(sum((row.quantity for row in ordered), ZERO))
        if is_zero(total):
            raise AccountingError(
                Code.ATTRIBUTION_DOES_NOT_SUM,
                f"{self.identity.label}: a dividend's attribution carries no quantity "
                "to apportion by",
            )
        after = self
        shares = _apportion(amount, ordered, total)
        for row, share in zip(ordered, shares, strict=True):
            after = after._with_pool(after.pool_for(row.strategy_id).credit_dividend(share))
        return after

    def as_json(self) -> dict[str, Any]:
        """Return the position as a JSON-safe mapping."""
        return {
            "identity": self.identity.as_json(),
            "label": self.identity.label,
            "quantity": str(self.quantity),
            "average_cost": str(self.average_cost),
            "total_cost": str(self.total_cost),
            "cost_basis_policy": PER_ATTRIBUTION_COST_POLICY,
            "realised_pnl": str(self.realised_pnl),
            "fees_paid": str(self.fees_paid),
            "dividends_received": str(self.dividends_received),
            "attribution": [row.as_json() for row in self.attribution_slices()],
            "pools": [pool.as_json() for pool in self.retained_pools()],
        }


def _decimal(value: str) -> Decimal:
    """Parse a quantity written back out of an allocation row."""
    from .money import as_decimal

    return as_decimal(value, field="allocation quantity")


def _apportion(
    amount: Decimal, slices: Sequence[StrategySlice], total: Decimal
) -> tuple[Decimal, ...]:
    """Split a cash amount across slices by quantity, losing nothing.

    The last slice takes the rounding remainder, so the parts sum to the whole
    exactly. Nothing is being estimated: the weights come from quantities the
    event stated.

    Args:
        amount: The cash to split.
        slices: The slices to split across, in their final order.
        total: The quantity the weights are against.

    Returns:
        One amount per slice, summing exactly to `amount`.
    """
    if not slices:
        return ()
    if is_zero(total) or amount == ZERO:
        return tuple(ZERO for _ in slices)
    parts: list[Decimal] = []
    running = ZERO
    for index, row in enumerate(slices):
        if index == len(slices) - 1:
            share = quantise_cash(amount - running)
        else:
            share = quantise_cash(amount * row.quantity / total)
            running = quantise_cash(running + share)
        parts.append(share)
    return tuple(parts)
