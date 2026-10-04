"""What a paper fill is priced at, and how much of it is available.

Every number the paper broker uses comes from here, and every number here is
supplied explicitly by the caller. There is no price lookup, no default and no
fallback — in particular, **a missing execution price is not replaced by the
sizing price**. That fallback is the single most tempting shortcut in a paper
engine and it quietly destroys the thing the engine is for: if the fill price is
always the price that sized the order, every rebalance executes at exactly the
level it was planned against and the simulation can never show a cost.

The baseline is `PAPER_REFERENCE_PRICE_FILL_V1`: a market order fills at the one
price the snapshot states. No spread, no slippage, no impact, no participation
rate, no latency, no queue. That is not a modelling oversight, it is the scope —
and `engine_policy()` says so in the manifest, because a simulated return that
does not disclose its cost assumptions is a misleading number.

Availability is explicit too. A snapshot may state how much of a security can be
filled, which is how partial fills are exercised deterministically. Absent, the
policy is `PAPER_FULL_FILL_V1` — fill the remainder. Nothing is random: a
simulator that produced a different fill each run could not be used to establish
that a change to the engine was safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import as_decimal, quantise_price, quantise_quantity

from .errors import Code, ExecutionError
from .fingerprint import fingerprint_of
from .versions import PAPER_FILL_MODEL, PAPER_FILL_POLICY

if TYPE_CHECKING:
    from stock_screener.portfolio_risk import SecurityIdentity

__all__ = [
    "ExecutionPrice",
    "PaperExecutionSnapshot",
    "paper_market_policy",
]


def _parse_instant(value: str, *, field_name: str) -> datetime:
    """Parse an aware ISO 8601 instant.

    Args:
        value: The timestamp.
        field_name: What to name in an error.

    Returns:
        The aware moment.

    Raises:
        ExecutionError: If it is unparseable or naive. A naive execution instant
            cannot be ordered against an acknowledgement in another zone, and
            that ordering is the only temporal control this layer has.
    """
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExecutionError(
            Code.EXECUTION_EVENT_INVALID, f"{field_name} {value!r} is not ISO 8601"
        ) from exc
    if moment.tzinfo is None:
        raise ExecutionError(
            Code.EXECUTION_EVENT_INVALID,
            f"{field_name} must carry a UTC offset; a naive instant cannot be "
            "ordered against an acknowledgement recorded in another zone",
        )
    return moment


@dataclass(frozen=True)
class ExecutionPrice:
    """What one security fills at, and how much of it is available.

    Attributes:
        identity: The security.
        price: The fill price. Positive and finite.
        available_quantity: How much can fill. None means no stated limit, which
            under `PAPER_FULL_FILL_V1` fills the remainder. Zero means explicitly
            no liquidity — the order stays working rather than being rejected,
            because a venue with nothing on the other side has not refused the
            order.
        fee: The deterministic fee charged **per fill** of this security.
            Supplied, never invented: a commission schedule nobody configured
            would understate or overstate every simulated result. Per fill rather
            than per order because that is what a venue charges — an order that
            fills in three parts is three executions — so a partially filled
            order costs more than one that fills at once, which is a real
            consequence of partial fills rather than a modelling artefact.
        source_fingerprint: Where the price came from.
    """

    identity: SecurityIdentity
    price: Decimal
    available_quantity: Decimal | None = None
    fee: Decimal = Decimal(0)
    source_fingerprint: str = ""

    def __post_init__(self) -> None:
        """Validate the price and availability.

        Raises:
            ExecutionError: `INVALID_EXECUTION_PRICE` on a non-positive or
                non-finite price, a negative availability, or a negative fee.
        """
        price = as_decimal(self.price, field="execution price")
        if not price.is_finite() or price <= Decimal(0):
            raise ExecutionError(
                Code.INVALID_EXECUTION_PRICE,
                f"{self.identity.label}: an execution price must be positive and "
                f"finite, got {price}",
            )
        object.__setattr__(self, "price", quantise_price(price))
        if self.available_quantity is not None:
            available = quantise_quantity(
                as_decimal(self.available_quantity, field="available quantity")
            )
            if available < Decimal(0):
                raise ExecutionError(
                    Code.INVALID_EXECUTION_PRICE,
                    f"{self.identity.label}: available quantity may not be "
                    f"negative ({available}); zero states that nothing can fill",
                )
            object.__setattr__(self, "available_quantity", available)
        fee = quantise_price(as_decimal(self.fee, field="fee"))
        if fee < Decimal(0):
            raise ExecutionError(
                Code.INVALID_EXECUTION_PRICE,
                f"{self.identity.label}: a fee may not be negative ({fee})",
            )
        object.__setattr__(self, "fee", fee)

    def fillable(self, remaining: Decimal) -> Decimal:
        """How much of a remaining quantity this price can fill.

        Args:
            remaining: What is left on the order.

        Returns:
            `min(remaining, available)`, or the whole remainder when no
            availability is stated. Deterministic in both cases — there is no
            fraction drawn from anywhere.
        """
        if self.available_quantity is None:
            return quantise_quantity(remaining)
        return quantise_quantity(min(quantise_quantity(remaining), self.available_quantity))

    def as_json(self) -> dict[str, Any]:
        """Return the price as a JSON-safe mapping."""
        return {
            "identity": self.identity.label,
            "price": str(self.price),
            "available_quantity": (
                None if self.available_quantity is None else str(self.available_quantity)
            ),
            "fee": str(self.fee),
            "source_fingerprint": self.source_fingerprint,
        }


@dataclass(frozen=True)
class PaperExecutionSnapshot:
    """The prices and availability a paper broker may fill against.

    Attributes:
        as_of: The instant it describes, ISO 8601 with offset.
        prices: One entry per security.
        source: Where it came from.
    """

    as_of: str
    prices: tuple[ExecutionPrice, ...] = ()
    source: str = "UNSPECIFIED"

    def __post_init__(self) -> None:
        """Validate the instant and refuse a duplicated security.

        Raises:
            ExecutionError: `EXECUTION_EVENT_INVALID` on a bad instant;
                `INVALID_EXECUTION_PRICE` if a security is priced twice.
        """
        _parse_instant(self.as_of, field_name="paper execution snapshot as_of")
        seen: set[tuple[str, int]] = set()
        for row in self.prices:
            if row.identity.key in seen:
                raise ExecutionError(
                    Code.INVALID_EXECUTION_PRICE,
                    f"{row.identity.label} is priced twice in one execution "
                    "snapshot; whichever was read last would decide the fill",
                )
            seen.add(row.identity.key)

    @property
    def moment(self) -> datetime:
        """The snapshot's instant."""
        return _parse_instant(self.as_of, field_name="paper execution snapshot as_of")

    def price_for(self, identity: SecurityIdentity) -> ExecutionPrice | None:
        """The execution price for one security, if stated.

        Args:
            identity: The security.

        Returns:
            Its price, or None. None is returned rather than raised so the broker
            can decide what an unpriced order means for that order's state.
        """
        for row in self.prices:
            if row.identity.key == identity.key:
                return row
        return None

    def require_not_before(self, acknowledged_at: str, *, order_id: str) -> None:
        """Refuse a snapshot that predates the order being working.

        An order cannot fill at a price from before the venue accepted it. The
        check is explicit because the alternative — filling against whatever
        snapshot is to hand — is how a backtest ends up trading on information it
        did not have.

        Args:
            acknowledged_at: When the order became working.
            order_id: For the message.

        Raises:
            ExecutionError: `PAPER_SNAPSHOT_BEFORE_ACKNOWLEDGEMENT` if the
                snapshot is earlier.
        """
        acknowledged = _parse_instant(acknowledged_at, field_name="acknowledgement time")
        if self.moment < acknowledged:
            raise ExecutionError(
                Code.PAPER_SNAPSHOT_BEFORE_ACKNOWLEDGEMENT,
                f"{order_id} became working at {acknowledged_at} and the execution "
                f"snapshot describes {self.as_of}, which is earlier. Filling "
                "against it would trade at a price from before the order existed.",
            )

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return {
            "model": PAPER_FILL_MODEL,
            "policy": PAPER_FILL_POLICY,
            "as_of": self.as_of,
            "source": self.source,
            "prices": [row.as_json() for row in sorted(self.prices, key=lambda r: r.identity)],
        }

    def fingerprint(self) -> str:
        """The snapshot's deterministic digest."""
        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the snapshot as a JSON-safe mapping."""
        return {**self.body(), "snapshot_fingerprint": self.fingerprint()}


def paper_market_policy() -> dict[str, Any]:
    """Describe the paper fill model and what it deliberately omits.

    Returns:
        The policy, for the manifest. The omissions are listed explicitly
        because a simulated result that does not disclose them invites being
        read as a forecast.
    """
    return {
        "model": PAPER_FILL_MODEL,
        "fill_policy": PAPER_FILL_POLICY,
        "price_source": "the explicit execution snapshot, and nothing else",
        "sizing_price_fallback": (
            "NONE — a missing execution price leaves the order working rather "
            "than filling at the price that sized it, which would make every "
            "rebalance execute at exactly its planned level"
        ),
        "availability": ("explicit; absent means fill the remainder. Never a random fraction."),
        "fees": "explicit per security; default zero. No commission is invented.",
        "not_modelled": [
            "bid-ask spread",
            "slippage",
            "market impact",
            "volume participation",
            "latency",
            "queue position",
            "partial-day price paths",
            "order expiry across sessions",
        ],
        "consequence": (
            "a strategy run through this engine will look better than it would "
            "trade, by roughly the costs listed above. The engine exists to prove "
            "the order lifecycle and the accounting handoff are correct, not to "
            "estimate a return."
        ),
    }
