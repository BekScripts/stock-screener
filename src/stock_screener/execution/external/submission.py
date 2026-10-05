"""Submitting one order to a venue, and surviving not knowing whether it worked.

This is the module the phase exists for. Everything else is translation; this is
where a request leaves the process and the answer may never come back.

The failure is specific. A `POST` that times out has three possible histories:
it never arrived; it arrived and was refused; it arrived and created a live
order. The transport cannot tell them apart, and the difference between the
first and the third is a real position. The instinctive fix — send it again — is
the one action that converts "I don't know" into "two orders", so it is not
available here.

What is available is **a name decided in advance**. The client order id is
derived from the local order id, so after any crash the order can still be named
and asked about. Around that, four commitments:

**The intent is durable before the request is made.** If the process dies
between the `POST` and the reply, a record already on disk names the order that
may now exist at the venue. Recording afterwards would lose exactly the orders
that most need finding.

**Exactly one mutating request per call.** Not "usually one", not "one plus a
retry" — one, always, assertable by counting. There is no code path through
`submit_order` that sends two.

**An unknown stays unknown.** When resolution cannot establish what happened,
the result is `REMOTE_STATE_UNKNOWN` and the intent stays unresolved on disk. It
is never downgraded to `NOT_SUBMITTED` for tidiness, because that would be a
licence to submit again.

**Nothing here resubmits.** A deliberate later submission of the same order is
safe — the derived id means the venue itself refuses the duplicate — but the
decision to make one is not this module's to take, and no loop in it will.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import Code, ExecutionError
from ..fingerprint import fingerprint_of
from ..models import OrderSide

# Reaching for the P2.0 durability helpers deliberately: the intent log needs the
# same flush-fsync-rename-fsync discipline as the journal, and a second
# implementation of it would be a second thing to get subtly wrong.
from ..persistence import _atomic_write, _guard_root
from .client_ids import client_order_id_for, is_our_client_order_id
from .environment import refuse_live
from .models import ExternalOrderSnapshot
from .transport import READ_RETRY_LIMIT, BrokerResponse, RequestMethod, read_with_retry

if TYPE_CHECKING:
    from ..models import Order
    from .binding import ExternalBrokerAccountBinding
    from .symbols import SymbolMapping
    from .transport import BrokerTransport

__all__ = [
    "INTENT_DIRECTORY",
    "ORDERS_PATH",
    "RESOLUTION_DIRECTORY",
    "SUBMISSION_PROTOCOL",
    "SubmissionIntent",
    "SubmissionIntentLog",
    "SubmissionOutcome",
    "SubmissionResult",
    "resolve_submission",
    "submit_order",
]

#: How a submission is made durable and resolved. In the manifest because a
#: report's credibility rests on it.
SUBMISSION_PROTOCOL = "DURABLE_INTENT_THEN_LOOKUP_NEVER_RESEND_V1"

#: Where unresolved intents live. An intent here with no matching resolution is
#: an order whose fate is unknown, and recovery must ask the venue about it.
INTENT_DIRECTORY = "submission_intents"

#: Where resolutions live. Separate directory so an intent record is never
#: rewritten — the log is append-only in both halves.
RESOLUTION_DIRECTORY = "submission_resolutions"

#: The provider path orders are submitted to.
ORDERS_PATH = "/v2/orders"


class SubmissionOutcome(StrEnum):
    """What is known about one submission."""

    NOT_SUBMITTED = "NOT_SUBMITTED"
    """Nothing was sent. Either the order was refused locally before any request,
    or a lookup established the venue has no such order."""

    SUBMISSION_INTENT_DURABLE = "SUBMISSION_INTENT_DURABLE"
    """The intent is on disk and the request has not been made. The state a
    crash between the two leaves behind."""

    REMOTE_CONFIRMED = "REMOTE_CONFIRMED"
    """The venue has the order. Its id and status are known."""

    REMOTE_REJECTED = "REMOTE_REJECTED"
    """The venue refused it explicitly. Nothing is working, and nothing traded."""

    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    """The venue may or may not have the order, and asking did not settle it.
    The one outcome that must never be treated as either of the others."""

    @property
    def is_settled(self) -> bool:
        """Whether the order's fate is known."""
        return self in {
            SubmissionOutcome.NOT_SUBMITTED,
            SubmissionOutcome.REMOTE_CONFIRMED,
            SubmissionOutcome.REMOTE_REJECTED,
        }

    @property
    def may_submit_again(self) -> bool:
        """Whether a deliberate later submission of this order is admissible.

        True only for `NOT_SUBMITTED`, where a lookup established the venue has
        nothing. Never true for `REMOTE_STATE_UNKNOWN` — that is the whole point
        of keeping the two apart.
        """
        return self is SubmissionOutcome.NOT_SUBMITTED


@dataclass(frozen=True)
class SubmissionIntent:
    """The durable record written before a request is made.

    Attributes:
        order_id: The local order.
        client_order_id: The derived venue-facing id. The order's name at the
            venue, recomputable from `order_id` alone.
        execution_plan_id: Which plan authorised it.
        binding_fingerprint: Which account it was to be sent to.
        symbol: The venue symbol.
        side: Which way it trades.
        quantity: How much.
        recorded_at: When the intent was recorded, ISO-8601.
    """

    order_id: str
    client_order_id: str
    execution_plan_id: str
    binding_fingerprint: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    recorded_at: str

    def body(self) -> dict[str, Any]:
        """Return the intent's semantic content."""
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "execution_plan_id": self.execution_plan_id,
            "binding_fingerprint": self.binding_fingerprint,
            "symbol": self.symbol,
            "side": str(self.side),
            "quantity": str(self.quantity),
            "recorded_at": self.recorded_at,
            "protocol": SUBMISSION_PROTOCOL,
        }

    def fingerprint(self) -> str:
        """Digest the intent."""
        return fingerprint_of(self.body())

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> SubmissionIntent:
        """Rebuild an intent read back from disk.

        Args:
            body: What was written.

        Returns:
            The intent.

        Raises:
            ExecutionError: `EXECUTION_PARTIAL_RECORD` if a required field is
                missing. A half-readable intent is refused rather than
                reconstructed, because the fields it is missing are the ones
                that would let the order be found.
        """
        required = ("order_id", "client_order_id", "symbol", "side", "quantity")
        missing = [name for name in required if not body.get(name)]
        if missing:
            raise ExecutionError(
                Code.EXECUTION_PARTIAL_RECORD,
                f"a submission intent is missing {missing}; it cannot be used to "
                "find the order it was recorded to protect",
            )
        return cls(
            order_id=str(body["order_id"]),
            client_order_id=str(body["client_order_id"]),
            execution_plan_id=str(body.get("execution_plan_id", "")),
            binding_fingerprint=str(body.get("binding_fingerprint", "")),
            symbol=str(body["symbol"]),
            side=OrderSide(str(body["side"])),
            quantity=Decimal(str(body["quantity"])),
            recorded_at=str(body.get("recorded_at", "")),
        )


class SubmissionIntentLog:
    """The durable, append-only record of what this process tried to submit.

    One file per intent and one per resolution, in separate directories, so a
    resolution never rewrites an intent. An intent with no resolution is the
    thing recovery exists to find: an order that may be live at a venue with no
    local record of what became of it.
    """

    def __init__(self, root: Path | str) -> None:
        """Open the log.

        Args:
            root: Where the records live.

        Raises:
            ExecutionError: `FORBIDDEN_EXECUTION_NAMESPACE` if the root lies in a
                research or evidence namespace.
        """
        self.root = Path(root)
        _guard_root(self.root)
        self.intents = self.root / INTENT_DIRECTORY
        self.resolutions = self.root / RESOLUTION_DIRECTORY

    def record_intent(self, intent: SubmissionIntent) -> Path:
        """Write an intent durably, before any request is made.

        Writing the same intent twice is a no-op; writing a *different* intent
        under the same client order id is a conflict, because the id names the
        order at the venue and two different orders cannot share it.

        Args:
            intent: What is about to be submitted.

        Returns:
            Where it was written.

        Raises:
            ExecutionError: `REMOTE_CLIENT_ORDER_ID_CONFLICT` if a different
                intent already holds that client order id.
        """
        path = self.intents / f"{intent.client_order_id}.json"
        if path.exists():
            existing = SubmissionIntent.from_body(_read_json(path)["intent"])
            if existing.fingerprint() != intent.fingerprint():
                raise ExecutionError(
                    Code.REMOTE_CLIENT_ORDER_ID_CONFLICT,
                    f"{intent.client_order_id} already names a different order "
                    f"({existing.order_id}, {existing.quantity} {existing.symbol}); it "
                    f"cannot also name {intent.order_id}",
                )
            return path
        _atomic_write(path, {"intent": intent.body()})
        return path

    def record_resolution(
        self,
        client_order_id: str,
        *,
        outcome: SubmissionOutcome,
        remote_order_id: str | None = None,
        detail: str = "",
    ) -> Path | None:
        """Record how an intent was settled.

        An unsettled outcome writes nothing: a `REMOTE_STATE_UNKNOWN` resolution
        file would mark the intent as handled when the order's fate is exactly
        what is still outstanding.

        Args:
            client_order_id: Which intent.
            outcome: What was established.
            remote_order_id: The venue's id, when there is one.
            detail: A sanitised note.

        Returns:
            Where it was written, or `None` when nothing was settled.
        """
        if not outcome.is_settled:
            return None
        path = self.resolutions / f"{client_order_id}.json"
        _atomic_write(
            path,
            {
                "client_order_id": client_order_id,
                "outcome": str(outcome),
                "remote_order_id": remote_order_id,
                "detail": detail,
            },
        )
        return path

    def recorded_intents(self) -> tuple[SubmissionIntent, ...]:
        """Every intent ever recorded, ordered by client order id.

        Returns:
            The intents.
        """
        if not self.intents.exists():
            return ()
        return tuple(
            SubmissionIntent.from_body(_read_json(path)["intent"])
            for path in sorted(self.intents.glob("*.json"))
        )

    def resolution_for(self, client_order_id: str) -> dict[str, Any] | None:
        """Read a recorded resolution.

        Args:
            client_order_id: Which intent.

        Returns:
            The resolution, or `None` if the intent is unresolved.
        """
        path = self.resolutions / f"{client_order_id}.json"
        return _read_json(path) if path.exists() else None

    def unresolved(self) -> tuple[SubmissionIntent, ...]:
        """Every intent with no recorded resolution.

        These are the orders that may exist at the venue with nothing local
        saying so. A restart has to ask about each one before planning anything
        new, because planning around a book that is missing a live order sizes
        the next trade wrongly.

        Returns:
            The unresolved intents.
        """
        return tuple(
            intent
            for intent in self.recorded_intents()
            if self.resolution_for(intent.client_order_id) is None
        )


def _read_json(path: Path) -> dict[str, Any]:
    """Read one record.

    Args:
        path: The record.

    Returns:
        Its content.

    Raises:
        ExecutionError: `EXECUTION_PARTIAL_RECORD` if it cannot be parsed.
    """
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExecutionError(
            Code.EXECUTION_PARTIAL_RECORD,
            f"{path.name} could not be read as a submission record: {exc}",
        ) from exc
    if not isinstance(body, dict):
        raise ExecutionError(
            Code.EXECUTION_PARTIAL_RECORD,
            f"{path.name} does not hold a submission record",
        )
    return body


@dataclass(frozen=True)
class SubmissionResult:
    """What one submission established.

    Attributes:
        outcome: What is known.
        intent: The durable intent.
        snapshot: The venue's order, when it is known to exist.
        detail: A sanitised explanation. Never carries a credential.
        mutating_requests: How many mutating requests this call made. Reported so
            a test can assert the number rather than trust the prose.
        lookups: How many read requests resolution needed.
    """

    outcome: SubmissionOutcome
    intent: SubmissionIntent
    snapshot: ExternalOrderSnapshot | None = None
    detail: str = ""
    mutating_requests: int = 0
    lookups: int = 0

    @property
    def remote_order_id(self) -> str | None:
        """The venue's order id, when known."""
        return self.snapshot.remote_order_id if self.snapshot else None

    def as_json(self) -> dict[str, Any]:
        """Return the result as a JSON-safe mapping."""
        return {
            "outcome": str(self.outcome),
            "settled": self.outcome.is_settled,
            "may_submit_again": self.outcome.may_submit_again,
            "order_id": self.intent.order_id,
            "client_order_id": self.intent.client_order_id,
            "remote_order_id": self.remote_order_id,
            "snapshot": self.snapshot.as_json() if self.snapshot else None,
            "detail": self.detail,
            "mutating_requests": self.mutating_requests,
            "lookups": self.lookups,
        }


def _order_payload(
    order: Order, *, symbol: str, client_order_id: str, quantity: Decimal
) -> dict[str, Any]:
    """Build the provider payload for one order.

    Args:
        order: The authorised order.
        symbol: The resolved venue symbol.
        client_order_id: The derived id.
        quantity: The quantity to send.

    Returns:
        The payload. Market and day only, with no extended-hours flag and no
        notional field — a payload cannot express what the capabilities refused.
    """
    return {
        "symbol": symbol,
        "qty": str(quantity),
        "side": str(order.side).lower(),
        "type": "market",
        "time_in_force": "day",
        "client_order_id": client_order_id,
    }


def _snapshot_from(response: BrokerResponse, *, context: str) -> ExternalOrderSnapshot:
    """Read an order snapshot out of a provider reply.

    Args:
        response: The reply.
        context: What was being read.

    Returns:
        The snapshot.

    Raises:
        ExecutionError: `REMOTE_STATE_UNKNOWN` if the reply carries no order
            object, or `BROKER_STATUS_UNMAPPED` for a status this adapter does
            not know.
    """
    if not isinstance(response.body, dict):
        raise ExecutionError(
            Code.REMOTE_STATE_UNKNOWN,
            f"{context}: the venue answered without an order object, so nothing "
            "about the order's state was established",
        )
    return ExternalOrderSnapshot.from_payload(response.body)


def submit_order(
    order: Order,
    *,
    transport: BrokerTransport,
    binding: ExternalBrokerAccountBinding,
    mapping: SymbolMapping,
    log: SubmissionIntentLog,
    recorded_at: str,
    quantity: Decimal | None = None,
) -> SubmissionResult:
    """Submit one authorised order to the paper venue.

    The sequence is fixed and the order of its steps is the whole safety
    argument: validate locally, derive the name, make the intent durable, send
    once, then classify. Validation precedes durability so a refusable order
    leaves no intent behind; durability precedes the request so a crash during
    the request leaves one.

    Args:
        order: The authorised order. Authorisation is checked by the caller —
            this function does not grant any.
        transport: The transport. Injected; never constructed here.
        binding: The proven paper account binding.
        mapping: The declared symbol mapping.
        log: The durable intent log.
        recorded_at: When the intent is being recorded, ISO-8601.
        quantity: The quantity to send, defaulting to the order's own. Supplied
            separately so a residual can be submitted without inventing a new
            order.

    Returns:
        What was established. Exactly one mutating request was made unless the
        order was refused locally, in which case none was.

    Raises:
        ExecutionError: `LIVE_TRADING_NOT_AUTHORIZED` if the binding is not
            paper; `BROKER_CAPABILITY_UNSUPPORTED`,
            `BROKER_FRACTIONAL_NOT_SUPPORTED`,
            `BROKER_SYMBOL_MAPPING_UNRESOLVED` or `BROKER_ASSET_NOT_TRADABLE`
            if the order cannot be expressed at this venue;
            `EXTERNAL_SUBMISSION_BLOCKED` if the venue is refusing trades.
    """
    refuse_live(binding.environment, context=f"submitting order {order.order_id}")
    send_quantity = order.quantity if quantity is None else quantity
    if send_quantity <= 0:
        raise ExecutionError(
            Code.EXECUTION_EVENT_INVALID,
            f"{order.order_id}: a submission needs a positive quantity, got {send_quantity}",
        )
    binding.capabilities.require_order_type(str(order.order_type))
    binding.capabilities.require_time_in_force(str(order.time_in_force))
    binding.capabilities.require_quantity(send_quantity)
    symbol = mapping.symbol_for(order.identity)
    client_order_id = client_order_id_for(
        order.order_id, limit=binding.capabilities.client_order_id_limit
    )

    intent = SubmissionIntent(
        order_id=order.order_id,
        client_order_id=client_order_id,
        execution_plan_id=order.execution_plan_id,
        binding_fingerprint=binding.binding_fingerprint(),
        symbol=symbol,
        side=order.side,
        quantity=send_quantity,
        recorded_at=recorded_at,
    )
    log.record_intent(intent)

    response = transport.request(
        RequestMethod.POST,
        ORDERS_PATH,
        json_body=_order_payload(
            order, symbol=symbol, client_order_id=client_order_id, quantity=send_quantity
        ),
    )

    if response.ok:
        snapshot = _snapshot_from(response, context=f"submitting {order.order_id}")
        log.record_resolution(
            client_order_id,
            outcome=SubmissionOutcome.REMOTE_CONFIRMED,
            remote_order_id=snapshot.remote_order_id,
            detail="accepted on submission",
        )
        return SubmissionResult(
            outcome=SubmissionOutcome.REMOTE_CONFIRMED,
            intent=intent,
            snapshot=snapshot,
            detail="the venue accepted the order",
            mutating_requests=1,
        )

    if response.is_ambiguous:
        # The request's fate is unknown. Resolution reads; it never resends.
        resolved = resolve_submission(intent, transport=transport, binding=binding, log=log)
        return SubmissionResult(
            outcome=resolved.outcome,
            intent=intent,
            snapshot=resolved.snapshot,
            detail=(f"the submission did not complete ({response.outcome}); {resolved.detail}"),
            mutating_requests=1,
            lookups=resolved.lookups,
        )

    if response.status_code in {409, 422} and _is_duplicate_id(response):
        # The venue already holds this client order id, which can only mean an
        # earlier attempt arrived. That is information, not a failure.
        resolved = resolve_submission(intent, transport=transport, binding=binding, log=log)
        return SubmissionResult(
            outcome=resolved.outcome,
            intent=intent,
            snapshot=resolved.snapshot,
            detail=(
                "the venue reports this client order id already exists, so an "
                f"earlier attempt arrived; {resolved.detail}"
            ),
            mutating_requests=1,
            lookups=resolved.lookups,
        )

    log.record_resolution(
        client_order_id,
        outcome=SubmissionOutcome.REMOTE_REJECTED,
        detail=f"status {response.status_code}",
    )
    return SubmissionResult(
        outcome=SubmissionOutcome.REMOTE_REJECTED,
        intent=intent,
        detail=f"the venue refused the order (status {response.status_code})",
        mutating_requests=1,
    )


def _is_duplicate_id(response: BrokerResponse) -> bool:
    """Whether a refusal says the client order id is already in use.

    Args:
        response: The reply.

    Returns:
        True if the venue named a duplicate client order id.
    """
    text = ""
    if isinstance(response.body, dict):
        text = str(response.body.get("message", ""))
    return "client_order_id" in text.lower() or "duplicate" in text.lower()


def resolve_submission(
    intent: SubmissionIntent,
    *,
    transport: BrokerTransport,
    binding: ExternalBrokerAccountBinding,
    log: SubmissionIntentLog,
    limit: int = READ_RETRY_LIMIT,
) -> SubmissionResult:
    """Establish what happened to an order whose submission did not complete.

    Reads only. This is the function that makes an ambiguous `POST` survivable,
    and it does so by asking the venue about the order by the name that was
    derived before the request was sent — which is why that name may not be
    generated.

    Three answers are possible, and keeping the third distinct from the first is
    the point of the whole module:

    * the venue has the order — `REMOTE_CONFIRMED`;
    * the venue answers that it does not exist — `NOT_SUBMITTED`, and a
      deliberate later submission is admissible;
    * the venue cannot be reached or does not answer clearly —
      `REMOTE_STATE_UNKNOWN`, the intent stays unresolved, and nothing may be
      submitted for this order until it is settled.

    Args:
        intent: The durable intent.
        transport: The transport.
        binding: The paper binding.
        log: The durable intent log.
        limit: How many read attempts at most.

    Returns:
        What was established.

    Raises:
        ExecutionError: `BROKER_ACCOUNT_MISMATCH` if the venue answers for
            another account, or `BROKER_STATUS_UNMAPPED` for a status this
            adapter does not know.
    """
    path = f"{ORDERS_PATH}:by_client_order_id"
    response = read_with_retry(
        transport, path, params={"client_order_id": intent.client_order_id}, limit=limit
    )

    if response.ok:
        snapshot = _snapshot_from(response, context=f"looking up {intent.order_id}")
        if is_our_client_order_id(snapshot.client_order_id) and (
            snapshot.client_order_id != intent.client_order_id
        ):
            raise ExecutionError(
                Code.REMOTE_CLIENT_ORDER_ID_CONFLICT,
                f"a lookup for {intent.client_order_id} answered with "
                f"{snapshot.client_order_id}; the venue has answered about a "
                "different order",
            )
        log.record_resolution(
            intent.client_order_id,
            outcome=SubmissionOutcome.REMOTE_CONFIRMED,
            remote_order_id=snapshot.remote_order_id,
            detail="found by lookup after an incomplete submission",
        )
        return SubmissionResult(
            outcome=SubmissionOutcome.REMOTE_CONFIRMED,
            intent=intent,
            snapshot=snapshot,
            detail="a lookup found the order, so the request did arrive",
            lookups=response.attempts,
        )

    if response.not_found:
        log.record_resolution(
            intent.client_order_id,
            outcome=SubmissionOutcome.NOT_SUBMITTED,
            detail="a lookup established the venue has no such order",
        )
        return SubmissionResult(
            outcome=SubmissionOutcome.NOT_SUBMITTED,
            intent=intent,
            detail=(
                "a lookup established the venue has no order under this id, so the "
                "request did not arrive. A deliberate later submission is "
                "admissible and would be idempotent under the same derived id"
            ),
            lookups=response.attempts,
        )

    return SubmissionResult(
        outcome=SubmissionOutcome.REMOTE_STATE_UNKNOWN,
        intent=intent,
        detail=(
            f"the venue could not be asked about this order ({response.outcome} after "
            f"{response.attempts} attempts). The intent stays unresolved: the order "
            "may be working, and submitting it again on this evidence is how a "
            "duplicate position is created"
        ),
        lookups=response.attempts,
    )
