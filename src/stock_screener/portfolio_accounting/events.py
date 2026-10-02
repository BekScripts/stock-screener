"""Portfolio events: the only things that may change authoritative state.

Every holding, every cent of cash and every unit of cost basis in this system
traces to an event in this module. That is the whole design: a target is not a
holding, an intent is not a fill, and an approved portfolio is a statement about
what the risk engine would permit rather than a record of what is owned. Only an
admitted event moves the ledger.

The event set is deliberately small and deliberately specific. There is no generic
"set the balance to this" event, because such an event makes every invariant in the
package unenforceable — anything that cannot be expressed as a fill, a cash flow, a
dividend or a split is something the accounting engine does not understand, and
`UNSUPPORTED_CORPORATE_ACTION` is the honest answer rather than a guess at the
economics.

Each event carries its own fingerprint over its semantic payload. Ingestion
timestamps live beside that fingerprint, never inside it: a digest that moves with
the wall clock can never establish that two ledgers agree.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_risk import SecurityIdentity

from .errors import AccountingError, Code
from .money import ZERO, as_decimal, quantise_cash, quantise_price, quantise_quantity

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "UNATTRIBUTED",
    "UNSUPPORTED_ACTIONS",
    "CashDividendEvent",
    "CashFlowDirection",
    "CorporateActionEvent",
    "EventType",
    "ExternalCashFlowEvent",
    "FillEvent",
    "OpeningBalanceEvent",
    "OpeningPosition",
    "PortfolioEvent",
    "Side",
    "SplitEvent",
    "StrategySlice",
    "event_fingerprint",
]

#: The sentinel owner for a holding whose strategy attribution is unknown. An
#: imported legacy position has no history that says who bought it, and inventing
#: one to make a report look complete would put a fiction into the audit trail.
UNATTRIBUTED = "UNATTRIBUTED_EXISTING"

#: Corporate actions whose economics this engine does not model. Naming them is
#: the point: each has a real treatment that depends on terms the event cannot
#: carry, so guessing would silently misstate cost basis or invent a position.
UNSUPPORTED_ACTIONS: frozenset[str] = frozenset(
    {
        "MERGER",
        "SPINOFF",
        "RIGHTS_DISTRIBUTION",
        "TENDER_OFFER",
        "STOCK_DIVIDEND",
        "SYMBOL_SUBSTITUTION",
        "CASH_IN_LIEU",
        "RETURN_OF_CAPITAL",
    }
)


class EventType(StrEnum):
    """What kind of fact an event records."""

    OPENING_BALANCE = "OPENING_BALANCE"
    FILL = "FILL"
    EXTERNAL_CASH_FLOW = "EXTERNAL_CASH_FLOW"
    CASH_DIVIDEND = "CASH_DIVIDEND"
    SPLIT = "SPLIT"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    """An action presented for recording. Only the supported kinds are admitted."""


class Side(StrEnum):
    """Which way a fill went."""

    BUY = "BUY"
    SELL = "SELL"


class CashFlowDirection(StrEnum):
    """Whether external cash arrived or left.

    External flows change NAV and never P&L. The distinction is mandatory: a
    deposit that counted as performance would make a portfolio look skilful for
    receiving money.
    """

    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"


def event_fingerprint(body: Any) -> str:
    """Digest an event's semantic payload.

    Args:
        body: The payload. `Decimal` is serialised as its string form so the
            digest reflects the recorded figure exactly.

    Returns:
        SHA-256 over the canonical JSON form.

    Raises:
        AccountingError: If the payload contains a set, whose iteration order is
            not part of its value and would make the digest depend on history.
    """

    def canonical(value: Any) -> Any:
        if isinstance(value, set | frozenset):
            raise AccountingError(
                Code.EVENT_INVALID,
                "a set cannot be fingerprinted: its order is not part of its value",
            )
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {str(k): canonical(v) for k, v in sorted(value.items(), key=lambda p: str(p[0]))}
        if isinstance(value, list | tuple):
            return [canonical(item) for item in value]
        if isinstance(value, SecurityIdentity):
            return value.label
        return value

    return hashlib.sha256(
        json.dumps(canonical(body), sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


@dataclass(frozen=True)
class StrategySlice:
    """How much of a fill belongs to one strategy.

    Attributes:
        strategy_id: The owner. `UNATTRIBUTED` for a legacy holding.
        quantity: The share count attributed to it.
    """

    strategy_id: str
    quantity: Decimal

    def __post_init__(self) -> None:
        """Normalise the quantity and refuse a negative slice.

        Raises:
            AccountingError: On a missing owner or negative quantity.
        """
        if not self.strategy_id:
            raise AccountingError(Code.EVENT_INVALID, "an attribution slice must name its strategy")
        quantity = quantise_quantity(as_decimal(self.quantity, field="slice quantity"))
        if quantity < ZERO:
            raise AccountingError(
                Code.EVENT_INVALID,
                f"{self.strategy_id}: an attribution slice may not be negative "
                f"({quantity}); the long-only baseline has no short slices",
            )
        object.__setattr__(self, "quantity", quantity)

    def as_json(self) -> dict[str, Any]:
        """Return the slice as a JSON-safe mapping."""
        return {"strategy_id": self.strategy_id, "quantity": str(self.quantity)}


@dataclass(frozen=True)
class PortfolioEvent:
    """The envelope every event carries.

    Attributes:
        event_id: The caller's idempotency key. Two events with one id must have
            the same payload, or the ledger refuses the second.
        event_type: What kind of fact this is.
        effective_at: When the fact took effect, ISO 8601 with offset. Semantic.
        source_id: What produced it.
        source_fingerprint: The digest of the evidence behind it. Absent is
            refused: an unattributable change to cash cannot be audited.
        ingested_at: When it reached the system. Operational, and deliberately
            outside the fingerprint.
    """

    event_id: str
    event_type: EventType
    effective_at: str
    source_id: str
    source_fingerprint: str
    ingested_at: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        """Refuse an envelope that could not be audited.

        Raises:
            AccountingError: On a missing id, effective time or fingerprint.
        """
        for name in ("event_id", "effective_at", "source_fingerprint"):
            if not getattr(self, name):
                raise AccountingError(
                    Code.EVENT_INVALID,
                    f"an event must carry {name}; a portfolio change nothing can be "
                    "traced back to is not auditable, and accepting one is how an "
                    "unexplained balance appears",
                )

    def envelope(self) -> dict[str, Any]:
        """The semantic envelope fields, without the ingestion clock."""
        return {
            "event_id": self.event_id,
            "event_type": str(self.event_type),
            "effective_at": self.effective_at,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
        }

    def payload(self) -> dict[str, Any]:  # pragma: no cover - overridden
        """The event-specific semantic payload."""
        raise NotImplementedError

    def body(self) -> dict[str, Any]:
        """The full fingerprinted body: envelope plus payload."""
        return {**self.envelope(), "payload": self.payload()}

    def fingerprint(self) -> str:
        """The event's deterministic digest."""
        return event_fingerprint(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the event with its fingerprint and ingestion time."""
        return {
            **self.body(),
            "fingerprint": self.fingerprint(),
            "ingested_at": self.ingested_at,
        }


@dataclass(frozen=True)
class OpeningPosition:
    """A holding imported when the ledger is established.

    Attributes:
        identity: The security.
        quantity: Shares held.
        cost_basis_per_share: What they cost. Required: a position with no basis
            would make every later realised P&L wrong by an unknown amount.
        attribution: Who owns it. Defaults to `UNATTRIBUTED`, because a legacy
            holding's history usually does not say.
    """

    identity: SecurityIdentity
    quantity: Decimal
    cost_basis_per_share: Decimal
    attribution: tuple[StrategySlice, ...] = ()

    def __post_init__(self) -> None:
        """Normalise the figures and default the attribution honestly.

        Raises:
            AccountingError: On a non-positive quantity, a negative basis, or an
                attribution that does not sum to the quantity.
        """
        quantity = quantise_quantity(as_decimal(self.quantity, field="opening quantity"))
        basis = quantise_price(as_decimal(self.cost_basis_per_share, field="opening cost basis"))
        if quantity <= ZERO:
            raise AccountingError(
                Code.EVENT_INVALID,
                f"{self.identity.label}: an opening position must hold a positive "
                f"quantity, got {quantity}",
            )
        if basis < ZERO:
            raise AccountingError(
                Code.EVENT_INVALID,
                f"{self.identity.label}: cost basis may not be negative ({basis})",
            )
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "cost_basis_per_share", basis)
        if not self.attribution:
            object.__setattr__(
                self, "attribution", (StrategySlice(strategy_id=UNATTRIBUTED, quantity=quantity),)
            )
        total = sum((row.quantity for row in self.attribution), ZERO)
        if quantise_quantity(total) != quantity:
            raise AccountingError(
                Code.ATTRIBUTION_DOES_NOT_SUM,
                f"{self.identity.label}: attribution sums to {total} but the opening "
                f"quantity is {quantity}",
            )

    def as_json(self) -> dict[str, Any]:
        """Return the opening position as a JSON-safe mapping."""
        return {
            "identity": self.identity.label,
            "quantity": str(self.quantity),
            "cost_basis_per_share": str(self.cost_basis_per_share),
            "attribution": [row.as_json() for row in self.attribution],
        }


@dataclass(frozen=True)
class OpeningBalanceEvent(PortfolioEvent):
    """Establishes the ledger. Exactly one per ledger generation.

    Attributes:
        cash: The starting cash.
        positions: Holdings imported with their cost basis.
    """

    cash: Decimal = ZERO
    positions: tuple[OpeningPosition, ...] = ()

    def __post_init__(self) -> None:
        """Normalise the cash and refuse a negative opening balance.

        Raises:
            AccountingError: If the opening cash is negative.
        """
        super().__post_init__()
        cash = quantise_cash(as_decimal(self.cash, field="opening cash"))
        if cash < ZERO:
            raise AccountingError(
                Code.EVENT_INVALID,
                f"opening cash may not be negative ({cash}); this baseline has no margin",
            )
        object.__setattr__(self, "cash", cash)

    def payload(self) -> dict[str, Any]:
        """The opening balance's semantic payload."""
        return {
            "cash": str(self.cash),
            "positions": [row.as_json() for row in self.positions],
        }


@dataclass(frozen=True)
class FillEvent(PortfolioEvent):
    """Evidence that a trade happened. Accounting records it; it never creates one.

    Attributes:
        identity: What traded.
        side: Buy or sell.
        quantity: Shares. Positive on both sides; `side` carries the direction.
        price: Execution price per share.
        fees: Transaction costs. Expensed, never capitalised into cost basis.
        attribution: Which strategies the fill belongs to. Required when the
            resulting position would otherwise have ambiguous ownership.
        execution_reference: The execution record this came from, where there is
            one. Informational.
    """

    identity: SecurityIdentity | None = None
    side: Side = Side.BUY
    quantity: Decimal = ZERO
    price: Decimal = ZERO
    fees: Decimal = ZERO
    attribution: tuple[StrategySlice, ...] = ()
    execution_reference: str | None = None

    def __post_init__(self) -> None:
        """Normalise the figures and refuse an unusable fill.

        Raises:
            AccountingError: On a missing identity, a non-positive quantity, a
                negative price or fee, or an attribution that does not sum.
        """
        super().__post_init__()
        if self.identity is None:
            raise AccountingError(Code.EVENT_INVALID, "a fill must name its security")
        quantity = quantise_quantity(as_decimal(self.quantity, field="fill quantity"))
        price = quantise_price(as_decimal(self.price, field="fill price"))
        fees = quantise_cash(as_decimal(self.fees, field="fill fees"))
        if quantity <= ZERO:
            raise AccountingError(
                Code.EVENT_INVALID,
                f"{self.identity.label}: a fill must move a positive quantity, got "
                f"{quantity}. Direction is carried by `side`, not by the sign.",
            )
        if price < ZERO:
            raise AccountingError(
                Code.EVENT_INVALID, f"{self.identity.label}: price may not be negative"
            )
        if fees < ZERO:
            raise AccountingError(
                Code.EVENT_INVALID, f"{self.identity.label}: fees may not be negative"
            )
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "fees", fees)
        if self.attribution:
            total = quantise_quantity(sum((row.quantity for row in self.attribution), ZERO))
            if total != quantity:
                raise AccountingError(
                    Code.ATTRIBUTION_DOES_NOT_SUM,
                    f"{self.identity.label}: attribution sums to {total} but the fill "
                    f"quantity is {quantity}. The slices are not scaled to fit: a "
                    "mismatch means the producer disagrees with itself.",
                )

    @property
    def gross_notional(self) -> Decimal:
        """Quantity times price, before fees."""
        return quantise_cash(self.quantity * self.price)

    def payload(self) -> dict[str, Any]:
        """The fill's semantic payload."""
        assert self.identity is not None  # noqa: S101 - established in __post_init__
        return {
            "identity": self.identity.label,
            "side": str(self.side),
            "quantity": str(self.quantity),
            "price": str(self.price),
            "fees": str(self.fees),
            "attribution": [row.as_json() for row in self.attribution],
            "execution_reference": self.execution_reference,
        }


@dataclass(frozen=True)
class ExternalCashFlowEvent(PortfolioEvent):
    """Cash arriving from or leaving for outside the portfolio.

    Never P&L. A deposit that counted as performance would make a portfolio look
    skilful for receiving money, and a withdrawal would look like a loss.

    Attributes:
        direction: Deposit or withdrawal.
        amount: The magnitude. Always positive; `direction` carries the sign.
    """

    direction: CashFlowDirection = CashFlowDirection.DEPOSIT
    amount: Decimal = ZERO

    def __post_init__(self) -> None:
        """Normalise the amount and refuse a non-positive flow.

        Raises:
            AccountingError: If the amount is not positive.
        """
        super().__post_init__()
        amount = quantise_cash(as_decimal(self.amount, field="cash flow amount"))
        if amount <= ZERO:
            raise AccountingError(
                Code.EVENT_INVALID,
                f"an external cash flow must be positive ({amount}); direction is "
                "carried by `direction`, not by the sign",
            )
        object.__setattr__(self, "amount", amount)

    @property
    def signed_amount(self) -> Decimal:
        """The flow as a signed change to cash."""
        return (
            self.amount
            if self.direction is CashFlowDirection.DEPOSIT
            else quantise_cash(-self.amount)
        )

    def payload(self) -> dict[str, Any]:
        """The cash flow's semantic payload."""
        return {"direction": str(self.direction), "amount": str(self.amount)}


@dataclass(frozen=True)
class CashDividendEvent(PortfolioEvent):
    """A known cash credit attributable to a holding.

    Entitlement is the producer's responsibility, not accounting's. Inferring it
    from today's holdings would be wrong in exactly the cases that matter — a
    position sold between the ex-date and the pay date still earns the dividend,
    and one bought after it does not.

    Attributes:
        identity: Which holding produced it.
        cash_amount: The credit.
        corporate_action_reference: The action this came from, where there is one.
    """

    identity: SecurityIdentity | None = None
    cash_amount: Decimal = ZERO
    corporate_action_reference: str | None = None

    def __post_init__(self) -> None:
        """Normalise the amount and refuse an unusable dividend.

        Raises:
            AccountingError: On a missing identity or a non-positive amount.
        """
        super().__post_init__()
        if self.identity is None:
            raise AccountingError(Code.EVENT_INVALID, "a dividend must name its security")
        amount = quantise_cash(as_decimal(self.cash_amount, field="dividend amount"))
        if amount <= ZERO:
            raise AccountingError(
                Code.EVENT_INVALID,
                f"{self.identity.label}: a cash dividend must be positive ({amount})",
            )
        object.__setattr__(self, "cash_amount", amount)

    def payload(self) -> dict[str, Any]:
        """The dividend's semantic payload."""
        assert self.identity is not None  # noqa: S101 - established in __post_init__
        return {
            "identity": self.identity.label,
            "cash_amount": str(self.cash_amount),
            "corporate_action_reference": self.corporate_action_reference,
        }


@dataclass(frozen=True)
class SplitEvent(PortfolioEvent):
    """A share split or reverse split, as an explicit ratio.

    `quantity * ratio` and `average_cost / ratio`, so total cost basis is exactly
    unchanged and no P&L is created. A reverse split is the same mechanism with a
    ratio below one, and the fractional quantity it produces is recorded rather
    than rounded away — cash in lieu is a separate economic event this engine does
    not invent.

    Attributes:
        identity: Which holding splits.
        numerator: New shares per `denominator` old shares.
        denominator: The old share count the ratio is expressed against.
    """

    identity: SecurityIdentity | None = None
    numerator: Decimal = ZERO
    denominator: Decimal = ZERO

    def __post_init__(self) -> None:
        """Normalise the ratio and refuse one that cannot be applied.

        Raises:
            AccountingError: On a missing identity or a non-positive ratio term.
        """
        super().__post_init__()
        if self.identity is None:
            raise AccountingError(Code.EVENT_INVALID, "a split must name its security")
        numerator = as_decimal(self.numerator, field="split numerator")
        denominator = as_decimal(self.denominator, field="split denominator")
        if numerator <= ZERO or denominator <= ZERO:
            raise AccountingError(
                Code.SPLIT_RATIO_INVALID,
                f"{self.identity.label}: a split ratio needs positive terms, got "
                f"{numerator}/{denominator}",
            )
        object.__setattr__(self, "numerator", numerator)
        object.__setattr__(self, "denominator", denominator)

    @property
    def ratio(self) -> Decimal:
        """New shares per old share."""
        return self.numerator / self.denominator

    def payload(self) -> dict[str, Any]:
        """The split's semantic payload."""
        assert self.identity is not None  # noqa: S101 - established in __post_init__
        return {
            "identity": self.identity.label,
            "numerator": str(self.numerator),
            "denominator": str(self.denominator),
        }


@dataclass(frozen=True)
class CorporateActionEvent(PortfolioEvent):
    """An action presented for recording whose economics may not be modelled.

    This type exists so an unsupported action can be *refused by name* rather than
    being absent from the vocabulary. A merger has a real treatment that depends on
    terms the event cannot carry; guessing would misstate cost basis or invent a
    position, and silently ignoring it would leave the ledger describing a company
    that no longer exists.

    Attributes:
        identity: The affected holding.
        action: What kind of action, as the producer named it.
        terms: Whatever the producer supplied, recorded for the refusal.
    """

    identity: SecurityIdentity | None = None
    action: str = ""
    terms: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse an action with no identity or name.

        Raises:
            AccountingError: On a missing identity or action name.
        """
        super().__post_init__()
        if self.identity is None or not self.action:
            raise AccountingError(
                Code.EVENT_INVALID, "a corporate action must name its security and its kind"
            )

    def payload(self) -> dict[str, Any]:
        """The action's semantic payload."""
        assert self.identity is not None  # noqa: S101 - established in __post_init__
        return {
            "identity": self.identity.label,
            "action": self.action,
            "terms": dict(sorted(self.terms.items())),
        }


def slices_from(rows: Sequence[Mapping[str, Any]]) -> tuple[StrategySlice, ...]:
    """Build attribution slices from their JSON form.

    Args:
        rows: `[{"strategy_id": ..., "quantity": ...}]`.

    Returns:
        The slices, in strategy order.
    """
    return tuple(
        sorted(
            (
                StrategySlice(
                    strategy_id=str(row["strategy_id"]),
                    quantity=as_decimal(row["quantity"], field="slice quantity"),
                )
                for row in rows
            ),
            key=lambda row: row.strategy_id,
        )
    )
