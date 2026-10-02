"""Authoritative portfolio state: what is actually owned, and what it cost.

This is the distinction the whole phase exists to draw. The risk engine produces
*targets* — what a portfolio should look like if every intent were filled at the
assumed price. This module holds *holdings* — what the ledger says is owned.
Those two are different objects with different truth conditions, and a system
that conflates them reports the portfolio it wanted instead of the one it has.

`PortfolioState` is therefore derived only by replaying the ledger. It has no
setters, it is never constructed from a broker statement, and it is never patched
to agree with one: a disagreement between this state and an external account is a
reconciliation break to be investigated, not a number to be overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .errors import AccountingError, Code
from .events import UNATTRIBUTED
from .ledger import GENESIS_HASH
from .money import ZERO, quantise_cash
from .positions import COST_BASIS_POLICY, PositionState

if TYPE_CHECKING:
    from collections.abc import Mapping
    from decimal import Decimal

    from stock_screener.portfolio_risk import SecurityIdentity

__all__ = [
    "CashSummary",
    "PortfolioState",
]


@dataclass(frozen=True)
class CashSummary:
    """Why cash is what it is.

    Keeping the components rather than only the balance is what makes the cash
    identity checkable: a balance that cannot be decomposed into the flows that
    produced it can only be asserted.

    Attributes:
        opening: Cash established by the opening balance.
        deposits: External cash in. Never P&L.
        withdrawals: External cash out. Never P&L.
        purchases: Cash spent on buys, excluding fees.
        sales: Cash received from sells, excluding fees.
        fees: Transaction costs paid.
        dividends: Cash dividends credited.
    """

    opening: Decimal = ZERO
    deposits: Decimal = ZERO
    withdrawals: Decimal = ZERO
    purchases: Decimal = ZERO
    sales: Decimal = ZERO
    fees: Decimal = ZERO
    dividends: Decimal = ZERO

    @property
    def balance(self) -> Decimal:
        """Cash as the components imply it."""
        return quantise_cash(
            self.opening
            + self.deposits
            - self.withdrawals
            - self.purchases
            + self.sales
            - self.fees
            + self.dividends
        )

    @property
    def external_flow_net(self) -> Decimal:
        """Net external capital, which NAV includes and P&L must exclude."""
        return quantise_cash(self.deposits - self.withdrawals)

    def as_json(self) -> dict[str, Any]:
        """Return the summary as a JSON-safe mapping."""
        return {
            "opening": str(quantise_cash(self.opening)),
            "deposits": str(quantise_cash(self.deposits)),
            "withdrawals": str(quantise_cash(self.withdrawals)),
            "purchases": str(quantise_cash(self.purchases)),
            "sales": str(quantise_cash(self.sales)),
            "fees": str(quantise_cash(self.fees)),
            "dividends": str(quantise_cash(self.dividends)),
            "balance": str(self.balance),
            "external_flow_net": str(self.external_flow_net),
        }


@dataclass(frozen=True)
class PortfolioState:
    """The portfolio as the ledger establishes it.

    Attributes:
        cash: The cash balance.
        cash_summary: The flows that produced it.
        positions: Open holdings, keyed by durable identity.
        closed_positions: Holdings that went to zero, kept for their realised
            P&L — a position closed at a loss still happened, and dropping it
            would make cumulative realised P&L stop reconciling.
        realised_pnl: Cumulative realised trading result, net of fees.
        dividend_income: Cumulative dividend income.
        fees_paid: Cumulative transaction costs.
        opening_cost_basis: What the imported holdings cost at inception. Kept
            because the inception NAV this engine can state is a cost-basis NAV:
            an opening balance supplies what positions cost, never what they
            were worth, so a market-value inception would have to be invented.
        ledger_sequence: The last ledger record replayed.
        ledger_head_hash: That record's chain hash, so a state can be bound to
            exactly the history it came from.
        last_effective_at: When that record took effect.
        unsupported_actions: Corporate actions presented and refused, recorded
            rather than discarded: an unrecorded refusal is indistinguishable
            from never having been told.
    """

    cash: Decimal = ZERO
    cash_summary: CashSummary = field(default_factory=CashSummary)
    positions: Mapping[tuple[str, int], PositionState] = field(default_factory=dict)
    closed_positions: Mapping[tuple[str, int], PositionState] = field(default_factory=dict)
    realised_pnl: Decimal = ZERO
    dividend_income: Decimal = ZERO
    fees_paid: Decimal = ZERO
    opening_cost_basis: Decimal = ZERO
    ledger_sequence: int = 0
    ledger_head_hash: str = GENESIS_HASH
    last_effective_at: str | None = None
    unsupported_actions: tuple[dict[str, Any], ...] = ()

    @property
    def is_established(self) -> bool:
        """Whether an opening balance has been replayed."""
        return self.ledger_sequence > 0

    @property
    def open_identities(self) -> tuple[SecurityIdentity, ...]:
        """The held securities, in durable-key order."""
        return tuple(
            self.positions[key].identity
            for key in sorted(self.positions)
            if self.positions[key].is_open
        )

    @property
    def inception_nav_at_cost(self) -> Decimal:
        """The NAV the ledger started from, measured at cost.

        Opening cash plus what the imported holdings cost. This is the baseline
        the P&L decomposition is measured against, and it is a cost basis rather
        than a market value because an opening balance states the former and
        cannot state the latter. The consequence is explicit: an imported
        position's pre-existing gain is reported as unrealised P&L earned here.
        Presenting it as a change in NAV since inception is the honest reading of
        what the engine was told.
        """
        return quantise_cash(self.cash_summary.opening + self.opening_cost_basis)

    @property
    def total_cost_basis(self) -> Decimal:
        """What the open holdings cost."""
        return quantise_cash(sum((row.total_cost for row in self.positions.values()), ZERO))

    def position(self, identity: SecurityIdentity) -> PositionState:
        """The holding for an identity, open or not.

        Args:
            identity: The security.

        Returns:
            The position, or an empty one when nothing has ever been held.
        """
        existing = self.positions.get(identity.key) or self.closed_positions.get(identity.key)
        return existing or PositionState(identity=identity)

    def with_position(self, position: PositionState) -> PortfolioState:
        """Return the state with one position replaced.

        A position that has gone to zero moves to `closed_positions`, so an
        exposure calculation never iterates a holding of nothing while its
        realised history stays available.

        Args:
            position: The updated holding.

        Returns:
            The new state.
        """
        open_rows = dict(self.positions)
        closed_rows = dict(self.closed_positions)
        key = position.identity.key
        if position.is_open:
            open_rows[key] = position
            closed_rows.pop(key, None)
        else:
            open_rows.pop(key, None)
            closed_rows[key] = position
        return replace(self, positions=open_rows, closed_positions=closed_rows)

    def check_cash_identity(self) -> None:
        """Verify cash equals the flows that produced it.

        Raises:
            AccountingError: If the balance and its decomposition disagree. This
                is an internal invariant: a mismatch means an event changed cash
                without saying why, which is the one thing an accounting engine
                must never permit.
        """
        implied = self.cash_summary.balance
        if quantise_cash(self.cash) != implied:
            raise AccountingError(
                Code.NAV_IDENTITY_VIOLATED,
                f"cash is {quantise_cash(self.cash)} but its components imply "
                f"{implied}: some event moved cash without recording a reason",
            )

    def check_invariants(self) -> None:
        """Verify every invariant the state must satisfy.

        Raises:
            AccountingError: On a negative cash balance, a negative or
                unattributed position, or a broken cash identity.
        """
        self.check_cash_identity()
        if quantise_cash(self.cash) < ZERO:
            raise AccountingError(
                Code.INSUFFICIENT_CASH,
                f"cash is {quantise_cash(self.cash)}; this baseline has no margin, so a "
                "negative balance means an event was applied that should have been refused",
            )
        for key in sorted(self.positions):
            row = self.positions[key]
            if row.quantity < ZERO:
                raise AccountingError(
                    Code.SHORT_POSITION_NOT_SUPPORTED,
                    f"{row.identity.label} holds {row.quantity}: the long-only "
                    "baseline cannot represent a short",
                )
            row.check_attribution()

    @property
    def unattributed_quantity(self) -> Decimal:
        """Shares held whose owning strategy is unknown."""
        return quantise_cash(
            sum(
                (row.attribution.get(UNATTRIBUTED, ZERO) for row in self.positions.values()),
                ZERO,
            )
        )

    def as_json(self) -> dict[str, Any]:
        """Return the state as a JSON-safe mapping."""
        return {
            "cash": str(quantise_cash(self.cash)),
            "cash_summary": self.cash_summary.as_json(),
            "cost_basis_policy": COST_BASIS_POLICY,
            "positions": [self.positions[key].as_json() for key in sorted(self.positions)],
            "closed_positions": [
                self.closed_positions[key].as_json() for key in sorted(self.closed_positions)
            ],
            "total_cost_basis": str(self.total_cost_basis),
            "realised_pnl": str(quantise_cash(self.realised_pnl)),
            "dividend_income": str(quantise_cash(self.dividend_income)),
            "fees_paid": str(quantise_cash(self.fees_paid)),
            "open_position_count": sum(1 for row in self.positions.values() if row.is_open),
            "opening_cost_basis": str(quantise_cash(self.opening_cost_basis)),
            "inception_nav_at_cost": str(self.inception_nav_at_cost),
            "ledger_sequence": self.ledger_sequence,
            "ledger_head_hash": self.ledger_head_hash,
            "last_effective_at": self.last_effective_at,
            "unsupported_actions": [dict(row) for row in self.unsupported_actions],
            "unattributed_quantity": str(self.unattributed_quantity),
        }
