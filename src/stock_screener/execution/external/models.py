"""Remote state, normalised — and inert.

Every type here describes what a venue *said*. None of them is an authority over
anything. That is the same commitment P2.0 made about orders, applied to a
stranger's data: a remote position is evidence to reconcile against the book,
never the book, and there is deliberately no method anywhere in this package
that writes a remote figure into the ledger.

Three rules shape these types.

**Nothing is parsed optimistically.** A quantity is a `Decimal` built from the
provider's string, never a float — `0.1 + 0.2` in a share count is a break
report nobody can explain. A missing numeric field stays `None` and is refused
where it is needed, rather than becoming zero on the way in.

**A fee the venue did not state is unknown, not zero.** This is the project's
oldest rule and it costs something to honour here: a commission-free venue
really does charge nothing, so assuming zero would usually be right. Usually is
the problem — the day a venue starts reporting a regulatory fee, a silent zero
becomes a cost that never reaches the cost report and a NAV that drifts by
exactly the fees. So the fee is either stated by the venue, declared by a local
schedule, or `BROKER_FILL_FEE_UNKNOWN`.

**A remote execution has a durable remote id.** That id is what makes reading the
same fill ten times idempotent, and a provider that cannot name its executions
individually cannot be reconciled against at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import quantise_price, quantise_quantity

from ..errors import Code, ExecutionError
from ..models import OrderSide
from .status import StatusReading, read_provider_status

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ..state_machine import OrderState

__all__ = [
    "ExternalAsset",
    "ExternalExecutionFill",
    "ExternalOrderSnapshot",
    "ExternalPaperAccountSnapshot",
    "ExternalPosition",
    "decimal_or_none",
    "require_decimal",
]


def decimal_or_none(value: Any, *, label: str) -> Decimal | None:
    """Read a provider numeric field as an exact `Decimal`, or `None`.

    Args:
        value: Whatever the provider sent — a string, an int, or nothing.
        label: What the field is, for the message.

    Returns:
        The exact value, or `None` when the provider omitted it. A float is
        accepted but converted through its string form, because binary
        floating-point cannot represent a cent exactly and a reconciliation
        break of 0.0000000001 shares is indistinguishable from a real one.

    Raises:
        ExecutionError: `EXECUTION_EVENT_INVALID` if the field is present and
            not a number. A malformed figure is refused rather than dropped —
            dropping it would read as "the venue omitted it", which is a
            different and much more innocent fact.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ExecutionError(
            Code.EXECUTION_EVENT_INVALID,
            f"{label}: the provider sent {value!r}, which is not a number",
        ) from exc


def require_decimal(value: Any, *, label: str, code: str = Code.EXECUTION_EVENT_INVALID) -> Decimal:
    """Read a provider numeric field that must be present.

    Args:
        value: Whatever the provider sent.
        label: What the field is, for the message.
        code: Which refusal code to raise when it is absent.

    Returns:
        The exact value.

    Raises:
        ExecutionError: `code` if the field is absent, or
            `EXECUTION_EVENT_INVALID` if it is malformed.
    """
    read = decimal_or_none(value, label=label)
    if read is None:
        raise ExecutionError(
            code, f"{label}: the provider stated no value, and this one is required"
        )
    return read


@dataclass(frozen=True)
class ExternalExecutionFill:
    """One execution the venue reported.

    Attributes:
        remote_fill_id: The venue's durable id for this execution. The basis of
            idempotency: reading the same fill repeatedly must change nothing.
        remote_order_id: The venue's order id.
        client_order_id: The derived client id, so it can be matched locally
            without trusting the venue's own id.
        symbol: The venue symbol.
        side: Which way it traded.
        quantity: How much traded in this execution.
        price: The execution price.
        fee: The fee for this execution, or `None` when the venue stated none.
        reported_at: When the venue says it happened, ISO-8601.
        sequence: The venue's own ordering number, where it gives one.
    """

    remote_fill_id: str
    remote_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    reported_at: str
    fee: Decimal | None = None
    sequence: int | None = None

    def __post_init__(self) -> None:
        """Refuse an execution that could not be posted.

        Raises:
            ExecutionError: `REMOTE_FILL_CONFLICT` if it has no durable remote
                id, or `EXECUTION_EVENT_INVALID` if its quantity or price is not
                positive. A zero-quantity execution is not a small trade — it is
                a message the adapter has misread.
        """
        if not self.remote_fill_id.strip():
            raise ExecutionError(
                Code.REMOTE_FILL_CONFLICT,
                "an execution with no durable remote id cannot be read idempotently; "
                "reading the venue twice would post it twice",
            )
        if self.quantity <= 0:
            raise ExecutionError(
                Code.EXECUTION_EVENT_INVALID,
                f"{self.remote_fill_id}: an execution must have traded a positive "
                f"quantity, got {self.quantity}",
            )
        if self.price <= 0:
            raise ExecutionError(
                Code.EXECUTION_EVENT_INVALID,
                f"{self.remote_fill_id}: an execution needs a positive price, got "
                f"{self.price}. A missing execution price is never replaced by "
                "anything else",
            )
        if self.fee is not None and self.fee < 0:
            raise ExecutionError(
                Code.EXECUTION_EVENT_INVALID,
                f"{self.remote_fill_id}: a fee cannot be negative, got {self.fee}",
            )

    @property
    def fee_is_known(self) -> bool:
        """Whether the venue stated a fee for this execution."""
        return self.fee is not None

    def require_fee(self) -> Decimal:
        """Return the fee, refusing to invent one.

        Returns:
            The stated fee.

        Raises:
            ExecutionError: `BROKER_FILL_FEE_UNKNOWN` if the venue stated none
                and no schedule has supplied one. Zero is not substituted: a
                silent zero on a venue that later starts charging would keep the
                cost out of the cost report and leave NAV short by exactly the
                fees.
        """
        if self.fee is None:
            raise ExecutionError(
                Code.BROKER_FILL_FEE_UNKNOWN,
                f"{self.remote_fill_id}: the venue stated no fee and none was "
                "declared. Missing is not zero — assuming zero would keep a real "
                "cost out of the cost report",
            )
        return self.fee

    def with_fee(self, fee: Decimal) -> ExternalExecutionFill:
        """Return this execution with a declared fee attached.

        Args:
            fee: The fee from a declared schedule.

        Returns:
            A new execution. The original is unchanged, so what the venue said
            and what a local schedule declared stay distinguishable.
        """
        return ExternalExecutionFill(
            remote_fill_id=self.remote_fill_id,
            remote_order_id=self.remote_order_id,
            client_order_id=self.client_order_id,
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            price=self.price,
            reported_at=self.reported_at,
            fee=fee,
            sequence=self.sequence,
        )

    @property
    def ordering_key(self) -> tuple[str, int, str]:
        """A total order over executions of one order.

        Timestamp first, then the venue's sequence where it gives one, then the
        remote id as a final tie-break so the order is total rather than merely
        usually-total. An unstable order would make the same remote state
        reconcile differently on two reads.
        """
        return (
            self.reported_at,
            self.sequence if self.sequence is not None else 0,
            self.remote_fill_id,
        )

    def body(self) -> dict[str, Any]:
        """Return the execution's semantic content."""
        return {
            "remote_fill_id": self.remote_fill_id,
            "remote_order_id": self.remote_order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": str(self.side),
            "quantity": quantise_quantity(self.quantity),
            "price": quantise_price(self.price),
            "fee": None if self.fee is None else quantise_price(self.fee),
            "fee_is_known": self.fee_is_known,
            "reported_at": self.reported_at,
        }

    def as_json(self) -> dict[str, Any]:
        """Return the execution as a JSON-safe mapping, every figure a string."""
        body = self.body()
        return {
            **body,
            "quantity": str(body["quantity"]),
            "price": str(body["price"]),
            "fee": None if body["fee"] is None else str(body["fee"]),
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        remote_fill_id: str,
        remote_order_id: str,
        client_order_id: str,
    ) -> ExternalExecutionFill:
        """Read an execution from a provider payload.

        Args:
            payload: The provider's execution object.
            remote_fill_id: The venue's execution id.
            remote_order_id: The venue's order id.
            client_order_id: The derived client id.

        Returns:
            The normalised execution.

        Raises:
            ExecutionError: If a required field is absent or malformed.
        """
        side_text = str(payload.get("side", "")).upper()
        if side_text not in {"BUY", "SELL"}:
            raise ExecutionError(
                Code.EXECUTION_EVENT_INVALID,
                f"{remote_fill_id}: side {payload.get('side')!r} is neither BUY nor SELL",
            )
        return cls(
            remote_fill_id=remote_fill_id,
            remote_order_id=remote_order_id,
            client_order_id=client_order_id,
            symbol=str(payload.get("symbol", "")),
            side=OrderSide(side_text),
            quantity=require_decimal(payload.get("qty"), label=f"{remote_fill_id} quantity"),
            price=require_decimal(payload.get("price"), label=f"{remote_fill_id} price"),
            reported_at=str(payload.get("transaction_time", payload.get("timestamp", ""))),
            fee=decimal_or_none(payload.get("fee"), label=f"{remote_fill_id} fee"),
            sequence=payload.get("sequence") if isinstance(payload.get("sequence"), int) else None,
        )


@dataclass(frozen=True)
class ExternalOrderSnapshot:
    """What the venue currently says about one order.

    Attributes:
        remote_order_id: The venue's id.
        client_order_id: The derived client id this adapter sent.
        symbol: The venue symbol.
        side: Which way it trades.
        quantity: The ordered quantity.
        filled_quantity: How much has traded.
        reading: The status, read through the explicit map.
        average_fill_price: The venue's average, where it gives one. Never used
            to post a fill — individual executions are.
        submitted_at: When the venue says it was accepted.
        updated_at: When the venue says it last changed.
        order_type: What the venue calls the order type.
        time_in_force: What the venue calls the time in force.
    """

    remote_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    filled_quantity: Decimal
    reading: StatusReading
    average_fill_price: Decimal | None = None
    submitted_at: str = ""
    updated_at: str = ""
    order_type: str = ""
    time_in_force: str = ""

    def __post_init__(self) -> None:
        """Refuse a snapshot that contradicts itself.

        Raises:
            ExecutionError: `REMOTE_ORDER_MISMATCH` if more has filled than was
                ordered — an over-fill is not a rounding issue, and adopting it
                would put a position in the book that nothing authorised.
        """
        if self.filled_quantity < 0:
            raise ExecutionError(
                Code.REMOTE_ORDER_MISMATCH,
                f"{self.remote_order_id}: filled quantity {self.filled_quantity} is negative",
            )
        if self.filled_quantity > self.quantity:
            raise ExecutionError(
                Code.REMOTE_ORDER_MISMATCH,
                f"{self.remote_order_id}: the venue reports {self.filled_quantity} "
                f"filled against an order for {self.quantity}. An over-fill is not "
                "adopted — it would put unauthorised quantity in the book",
            )

    @property
    def state(self) -> OrderState | None:
        """The local state this status maps to, if any."""
        return self.reading.state

    @property
    def is_terminal(self) -> bool:
        """Whether the venue considers this order finished."""
        return self.reading.terminal

    @property
    def outstanding(self) -> Decimal:
        """How much quantity the venue still has working."""
        return self.quantity - self.filled_quantity

    def body(self) -> dict[str, Any]:
        """Return the snapshot's semantic content."""
        return {
            "remote_order_id": self.remote_order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": str(self.side),
            "quantity": quantise_quantity(self.quantity),
            "filled_quantity": quantise_quantity(self.filled_quantity),
            "outstanding": quantise_quantity(self.outstanding),
            "status": self.reading.as_json(),
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
        }

    def as_json(self) -> dict[str, Any]:
        """Return the snapshot as a JSON-safe mapping, every figure a string."""
        body = self.body()
        return {
            **body,
            "quantity": str(body["quantity"]),
            "filled_quantity": str(body["filled_quantity"]),
            "outstanding": str(body["outstanding"]),
            "average_fill_price": (
                None
                if self.average_fill_price is None
                else str(quantise_price(self.average_fill_price))
            ),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ExternalOrderSnapshot:
        """Read an order snapshot from a provider payload.

        Args:
            payload: The provider's order object.

        Returns:
            The normalised snapshot.

        Raises:
            ExecutionError: `BROKER_STATUS_UNMAPPED` for an unknown status, or
                `EXECUTION_EVENT_INVALID` for a malformed field.
        """
        remote_id = str(payload.get("id", ""))
        side_text = str(payload.get("side", "")).upper()
        if side_text not in {"BUY", "SELL"}:
            raise ExecutionError(
                Code.EXECUTION_EVENT_INVALID,
                f"{remote_id or 'an order'}: side {payload.get('side')!r} is neither BUY nor SELL",
            )
        return cls(
            remote_order_id=remote_id,
            client_order_id=str(payload.get("client_order_id", "")),
            symbol=str(payload.get("symbol", "")),
            side=OrderSide(side_text),
            quantity=require_decimal(payload.get("qty"), label=f"{remote_id} quantity"),
            filled_quantity=decimal_or_none(
                payload.get("filled_qty"), label=f"{remote_id} filled quantity"
            )
            or Decimal(0),
            reading=read_provider_status(
                payload.get("status"), context=f"reading order {remote_id}"
            ),
            average_fill_price=decimal_or_none(
                payload.get("filled_avg_price"), label=f"{remote_id} average price"
            ),
            submitted_at=str(payload.get("submitted_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            order_type=str(payload.get("type", "")),
            time_in_force=str(payload.get("time_in_force", "")),
        )


@dataclass(frozen=True)
class ExternalPosition:
    """One holding the venue reports.

    Attributes:
        symbol: The venue symbol.
        quantity: How much the venue says is held.
        average_entry_price: The venue's cost basis, for information only. The
            book's own basis is authoritative and is never replaced by this: the
            two use different conventions, and this adapter does not adopt the
            venue's.
        market_value: What the venue values it at.
    """

    symbol: str
    quantity: Decimal
    average_entry_price: Decimal | None = None
    market_value: Decimal | None = None

    def body(self) -> dict[str, Any]:
        """Return the position's semantic content."""
        return {
            "symbol": self.symbol,
            "quantity": quantise_quantity(self.quantity),
        }

    def as_json(self) -> dict[str, Any]:
        """Return the position as a JSON-safe mapping."""
        return {"symbol": self.symbol, "quantity": str(quantise_quantity(self.quantity))}


@dataclass(frozen=True)
class ExternalAsset:
    """What the venue says about one tradable instrument.

    Read rather than assumed, because every field here is a precondition the
    order contract depends on. A canary that picked a symbol and hoped would
    discover a halted or non-fractionable asset by having its order rejected.

    Attributes:
        symbol: The venue symbol.
        tradable: Whether the venue accepts orders in it.
        fractionable: Whether it accepts a non-integer quantity.
        status: What the venue says about the listing.
        asset_class: What the venue calls the class.
        exchange: Where it lists.
        shortable: Whether it may be sold short. Recorded and unused — this
            engine is long-only.
    """

    symbol: str
    tradable: bool = False
    fractionable: bool = False
    status: str = ""
    asset_class: str = ""
    exchange: str = ""
    shortable: bool = False

    @property
    def is_us_equity(self) -> bool:
        """Whether the venue classes this as a US equity."""
        return self.asset_class.strip().lower() == "us_equity"

    def require_tradable_us_equity(self) -> None:
        """Refuse an instrument the frozen order contract cannot trade.

        Raises:
            ExecutionError: `BROKER_ASSET_NOT_TRADABLE` if the venue is not
                accepting orders in it, the listing is not active, or it is not
                a US equity. Checked before an order is built rather than
                discovered from a rejection.
        """
        if not self.is_us_equity:
            raise ExecutionError(
                Code.BROKER_ASSET_NOT_TRADABLE,
                f"{self.symbol} is class {self.asset_class!r}; this engine's order "
                "contract covers US equities only",
            )
        if self.status.strip().lower() != "active":
            raise ExecutionError(
                Code.BROKER_ASSET_NOT_TRADABLE,
                f"{self.symbol} has listing status {self.status!r}, not active",
            )
        if not self.tradable:
            raise ExecutionError(
                Code.BROKER_ASSET_NOT_TRADABLE,
                f"the venue is not accepting orders in {self.symbol}",
            )

    def body(self) -> dict[str, Any]:
        """Return the asset's semantic content."""
        return {
            "symbol": self.symbol,
            "tradable": self.tradable,
            "fractionable": self.fractionable,
            "status": self.status,
            "asset_class": self.asset_class,
            "exchange": self.exchange,
        }

    def as_json(self) -> dict[str, Any]:
        """Return the asset as a JSON-safe mapping."""
        return self.body()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ExternalAsset:
        """Read an asset from a provider payload.

        Args:
            payload: The provider's asset object.

        Returns:
            The normalised asset. Every boolean defaults to the conservative
            answer, so a field the provider omits reads as "not permitted".
        """
        return cls(
            symbol=str(payload.get("symbol", "")),
            tradable=bool(payload.get("tradable", False)),
            fractionable=bool(payload.get("fractionable", False)),
            status=str(payload.get("status", "")),
            asset_class=str(payload.get("class", payload.get("asset_class", ""))),
            exchange=str(payload.get("exchange", "")),
            shortable=bool(payload.get("shortable", False)),
        )


@dataclass(frozen=True)
class ExternalPaperAccountSnapshot:
    """What the venue says about the paper account as a whole.

    Not a book. It holds no strategy attribution, no cost pools and no realised
    P&L, and nothing in this package writes any of these figures into the ledger.
    It exists to be compared against the book, and a disagreement is a break to
    report.

    Attributes:
        account_id: The venue's account id, kept only long enough to check it
            against the binding. Never written to a report.
        status: What the venue says about the account.
        cash: The venue's cash figure.
        positions: What the venue says is held.
        buying_power: The venue's own derived figure, recorded but unused — this
            system decides what it can afford from its own book.
        is_paper: What the venue claims. Checked, never trusted alone.
        trading_blocked: Whether the venue is refusing trades.
        observed_at: When this was read.
    """

    account_id: str = field(repr=False)
    status: str = ""
    cash: Decimal | None = None
    positions: tuple[ExternalPosition, ...] = ()
    buying_power: Decimal | None = None
    is_paper: bool | None = None
    trading_blocked: bool = False
    observed_at: str = ""

    def position_for(self, symbol: str) -> ExternalPosition | None:
        """Find the venue's holding in one symbol.

        Args:
            symbol: The venue symbol.

        Returns:
            The holding, or `None` when the venue reports none. `None` means the
            venue says it holds nothing — which is a real figure of zero for
            reconciliation, not a missing one.
        """
        upper = symbol.strip().upper()
        for position in self.positions:
            if position.symbol.strip().upper() == upper:
                return position
        return None

    def require_cash(self) -> Decimal:
        """Return the venue's cash figure, refusing to invent one.

        Returns:
            The stated cash.

        Raises:
            ExecutionError: `REMOTE_STATE_UNKNOWN` if the venue stated none.
                Reconciling against an assumed zero would report the whole book
                as a cash break.
        """
        if self.cash is None:
            raise ExecutionError(
                Code.REMOTE_STATE_UNKNOWN,
                "the venue stated no cash figure; reconciling against an assumed "
                "zero would report the entire book as a cash break",
            )
        return self.cash

    def body(self) -> dict[str, Any]:
        """Return the snapshot's semantic content, without the account id.

        Carries exact `Decimal` figures for fingerprinting. Use `as_json` for
        anything that will be serialised.
        """
        return {
            "status": self.status,
            "cash": None if self.cash is None else quantise_price(self.cash),
            "positions": [position.body() for position in self.positions],
            "is_paper": self.is_paper,
            "trading_blocked": self.trading_blocked,
            "observed_at": self.observed_at,
            "authority": "NONE — this is evidence to reconcile against, not a book",
        }

    def as_json(self) -> dict[str, Any]:
        """Return the snapshot as a JSON-safe mapping, without the account id."""
        body = self.body()
        return {
            **body,
            "cash": None if body["cash"] is None else str(body["cash"]),
            "positions": [position.as_json() for position in self.positions],
        }

    @classmethod
    def from_payload(
        cls,
        account: Mapping[str, Any],
        positions: Sequence[Mapping[str, Any]] = (),
    ) -> ExternalPaperAccountSnapshot:
        """Read an account snapshot from provider payloads.

        Args:
            account: The provider's account object.
            positions: The provider's position objects.

        Returns:
            The normalised snapshot.

        Raises:
            ExecutionError: `EXECUTION_EVENT_INVALID` for a malformed figure.
        """
        return cls(
            account_id=str(account.get("id", account.get("account_number", ""))),
            status=str(account.get("status", "")),
            cash=decimal_or_none(account.get("cash"), label="account cash"),
            buying_power=decimal_or_none(account.get("buying_power"), label="buying power"),
            is_paper=bool(account["is_paper"]) if "is_paper" in account else None,
            trading_blocked=bool(account.get("trading_blocked", False)),
            positions=tuple(
                ExternalPosition(
                    symbol=str(row.get("symbol", "")),
                    quantity=require_decimal(
                        row.get("qty"), label=f"{row.get('symbol')} position quantity"
                    ),
                    average_entry_price=decimal_or_none(
                        row.get("avg_entry_price"), label=f"{row.get('symbol')} entry price"
                    ),
                    market_value=decimal_or_none(
                        row.get("market_value"), label=f"{row.get('symbol')} market value"
                    ),
                )
                for row in positions
            ),
            observed_at=str(account.get("observed_at", "")),
        )
