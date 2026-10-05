"""The provider adapter: paths, payload shapes, and the account check on reads.

One provider's REST surface, expressed narrowly. The adapter can read the
account, read a position, read an order by its client id, read the executions of
an order, cancel an order, and submit one. That is the whole surface, and the
omissions are deliberate:

* **no funding or transfer call**, so no code path moves money into or out of
  the account;
* **no close-all or liquidate call**, because a one-call "flatten everything" is
  a convenience whose accidental invocation is unrecoverable, and the ordinary
  path — plan the sells, authorise them, submit them, reconcile — already does it
  with every check applied;
* **no replace or amend call**, since this engine's lifecycle has no amendment
  state and a venue-side replacement would produce an order the journal never
  authorised;
* **no streaming subscription**, because a push stream delivers events in an
  order the adapter cannot verify and pulling state is what makes a sweep
  reproducible.

Every read checks the account the provider answered for against the binding, so
a credential rotated to a different account is a refusal rather than a
reconciliation break.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import Code, ExecutionError
from .client_ids import client_order_id_for
from .environment import refuse_live
from .models import (
    ExternalExecutionFill,
    ExternalOrderSnapshot,
    ExternalPaperAccountSnapshot,
)
from .submission import ORDERS_PATH, SubmissionResult, submit_order
from .transport import READ_RETRY_LIMIT, RequestMethod, read_with_retry

if TYPE_CHECKING:
    from decimal import Decimal

    from ..models import Order
    from .binding import ExternalBrokerAccountBinding
    from .submission import SubmissionIntentLog
    from .symbols import SymbolMapping
    from .transport import BrokerTransport

__all__ = [
    "ACCOUNT_PATH",
    "ACTIVITIES_PATH",
    "POSITIONS_PATH",
    "PROVIDER",
    "AlpacaPaperAdapter",
]

#: Which provider this adapter speaks to.
PROVIDER = "ALPACA"

#: Read-only account path.
ACCOUNT_PATH = "/v2/account"

#: Read-only positions path.
POSITIONS_PATH = "/v2/positions"

#: Read-only executions path.
ACTIVITIES_PATH = "/v2/account/activities/FILL"


@dataclass(frozen=True)
class AlpacaPaperAdapter:
    """A read-mostly adapter over one proven paper account.

    Attributes:
        transport: How requests are made. Injected, so every path in this class
            is exercisable without a network.
        binding: The proven paper account.
        mapping: The declared symbol mapping.
        read_limit: How many attempts a read may make.
    """

    transport: BrokerTransport
    binding: ExternalBrokerAccountBinding
    mapping: SymbolMapping
    read_limit: int = READ_RETRY_LIMIT

    def __post_init__(self) -> None:
        """Refuse an adapter over anything but a proven paper account.

        Raises:
            ExecutionError: `LIVE_TRADING_NOT_AUTHORIZED` if the binding is not
                paper.
        """
        refuse_live(self.binding.environment, context="constructing the provider adapter")

    def read_account(self) -> ExternalPaperAccountSnapshot:
        """Read the account and its positions.

        Returns:
            The venue's view of the account. Evidence, never a book.

        Raises:
            ExecutionError: `BROKER_UNREACHABLE` if the account cannot be read;
                `BROKER_ACCOUNT_MISMATCH` if the provider answers for another
                account; `BROKER_ENVIRONMENT_NOT_PROVEN_PAPER` if the provider
                says the account is not a paper account.
        """
        response = read_with_retry(self.transport, ACCOUNT_PATH, limit=self.read_limit)
        if not response.ok or not isinstance(response.body, dict):
            raise ExecutionError(
                Code.BROKER_UNREACHABLE,
                f"the account could not be read ({response.outcome} after "
                f"{response.attempts} attempts)",
            )
        positions = self._read_positions()
        snapshot = ExternalPaperAccountSnapshot.from_payload(response.body, positions)
        self.binding.require_account(snapshot.account_id, context="reading the account")
        if snapshot.is_paper is False:
            raise ExecutionError(
                Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
                "the provider reports this account is not a paper account. The host "
                "allow-list and key prefix both said it was, and the account's own "
                "answer overrides both",
            )
        return snapshot

    def _read_positions(self) -> list[dict[str, Any]]:
        """Read the account's positions.

        Returns:
            The raw position rows.

        Raises:
            ExecutionError: `BROKER_UNREACHABLE` if they cannot be read. An
                empty list is a real answer — "holds nothing" — and is never
                substituted for a failed read, which would reconcile as the
                whole book having vanished.
        """
        response = read_with_retry(self.transport, POSITIONS_PATH, limit=self.read_limit)
        if not response.ok:
            raise ExecutionError(
                Code.BROKER_UNREACHABLE,
                f"positions could not be read ({response.outcome}). An empty list is "
                "not substituted: it would reconcile as every holding having vanished",
            )
        if response.body is None:
            return []
        if not isinstance(response.body, list):
            raise ExecutionError(
                Code.REMOTE_STATE_UNKNOWN,
                "the positions endpoint did not answer with a list",
            )
        return [row for row in response.body if isinstance(row, dict)]

    def read_order(self, order_id: str) -> ExternalOrderSnapshot | None:
        """Read what the venue says about one local order.

        Args:
            order_id: The local order id. The client id is derived from it, so
                this works after a restart with nothing but the journal.

        Returns:
            The snapshot, or `None` when the venue has no such order.

        Raises:
            ExecutionError: `REMOTE_STATE_UNKNOWN` if the venue could not be
                asked — distinct from `None`, which is the venue answering that
                the order does not exist.
        """
        client_order_id = client_order_id_for(
            order_id, limit=self.binding.capabilities.client_order_id_limit
        )
        response = read_with_retry(
            self.transport,
            f"{ORDERS_PATH}:by_client_order_id",
            params={"client_order_id": client_order_id},
            limit=self.read_limit,
        )
        if response.not_found:
            return None
        if not response.ok or not isinstance(response.body, dict):
            raise ExecutionError(
                Code.REMOTE_STATE_UNKNOWN,
                f"{order_id}: the venue could not be asked about this order "
                f"({response.outcome}). Not knowing is not the same as the order not "
                "existing, and it is not reported as such",
            )
        return ExternalOrderSnapshot.from_payload(response.body)

    def read_open_orders(self) -> tuple[ExternalOrderSnapshot, ...]:
        """Read every order the venue currently has.

        Used by reconciliation, which needs the venue's whole picture: an order
        this system never placed is only visible in a listing.

        Returns:
            The snapshots.

        Raises:
            ExecutionError: `BROKER_UNREACHABLE` if the listing cannot be read.
        """
        response = read_with_retry(
            self.transport, ORDERS_PATH, params={"status": "all"}, limit=self.read_limit
        )
        if not response.ok:
            raise ExecutionError(
                Code.BROKER_UNREACHABLE,
                f"the order listing could not be read ({response.outcome}); an empty "
                "listing is not substituted, as it would hide an untracked order",
            )
        rows = response.body if isinstance(response.body, list) else []
        return tuple(
            ExternalOrderSnapshot.from_payload(row) for row in rows if isinstance(row, dict)
        )

    def read_fills(self, order_id: str) -> tuple[ExternalExecutionFill, ...]:
        """Read the executions the venue reports for one order.

        Individual executions, never the order's average price. An average
        cannot be posted: two partials at different prices have one average and
        two cost-basis effects, and the book needs the second.

        Args:
            order_id: The local order id.

        Returns:
            The executions, in a total order. Deduplicated by remote execution
            id, so reading the same window repeatedly yields the same set —
            which is what makes a sweep safe to repeat.

        Raises:
            ExecutionError: `BROKER_UNREACHABLE` if they cannot be read;
                `REMOTE_EVENT_ORDER_AMBIGUOUS` if two executions cannot be put
                in a definite order.
        """
        client_order_id = client_order_id_for(
            order_id, limit=self.binding.capabilities.client_order_id_limit
        )
        response = read_with_retry(
            self.transport,
            ACTIVITIES_PATH,
            params={"client_order_id": client_order_id},
            limit=self.read_limit,
        )
        if not response.ok:
            raise ExecutionError(
                Code.BROKER_UNREACHABLE,
                f"{order_id}: executions could not be read ({response.outcome})",
            )
        rows = response.body if isinstance(response.body, list) else []
        seen: dict[str, ExternalExecutionFill] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            remote_fill_id = str(row.get("id", ""))
            fill = ExternalExecutionFill.from_payload(
                row,
                remote_fill_id=remote_fill_id,
                remote_order_id=str(row.get("order_id", "")),
                client_order_id=client_order_id,
            )
            existing = seen.get(remote_fill_id)
            if existing is not None and existing.body() != fill.body():
                raise ExecutionError(
                    Code.REMOTE_FILL_CONFLICT,
                    f"{remote_fill_id} was reported twice with different content; the "
                    "venue's executions cannot both be true",
                )
            seen[remote_fill_id] = fill
        ordered = sorted(seen.values(), key=lambda fill: fill.ordering_key)
        keys = [fill.ordering_key for fill in ordered]
        if len(set(keys)) != len(keys):
            raise ExecutionError(
                Code.REMOTE_EVENT_ORDER_AMBIGUOUS,
                f"{order_id}: two executions share a complete ordering key, so the "
                "sequence they traded in cannot be established",
            )
        return tuple(ordered)

    def cancel_order(self, order_id: str) -> bool:
        """Ask the venue to cancel one order.

        Args:
            order_id: The local order id.

        Returns:
            True if the venue accepted the request. A cancellation request is
            not a cancellation: the order may fill before it takes effect, and
            only a subsequent read establishes what happened.

        Raises:
            ExecutionError: `BROKER_CAPABILITY_UNSUPPORTED` if the venue does
                not support cancellation; `REMOTE_STATE_UNKNOWN` if the request
                did not complete, which is never retried here.
        """
        if not self.binding.capabilities.cancel_supported:
            raise ExecutionError(
                Code.BROKER_CAPABILITY_UNSUPPORTED,
                "this venue does not support cancelling an order",
            )
        client_order_id = client_order_id_for(
            order_id, limit=self.binding.capabilities.client_order_id_limit
        )
        response = self.transport.request(RequestMethod.DELETE, f"{ORDERS_PATH}/{client_order_id}")
        if response.is_ambiguous:
            raise ExecutionError(
                Code.REMOTE_STATE_UNKNOWN,
                f"{order_id}: the cancellation did not complete ({response.outcome}). "
                "It is not sent again — a read establishes the order's state",
            )
        return response.ok

    def submit(
        self,
        order: Order,
        *,
        log: SubmissionIntentLog,
        recorded_at: str,
        quantity: Decimal | None = None,
    ) -> SubmissionResult:
        """Submit one authorised order.

        The only mutating trade path in this package. It delegates to
        `submit_order` rather than reimplementing the protocol, so there is
        exactly one place where an order leaves this process.

        Args:
            order: The authorised order.
            log: The durable intent log.
            recorded_at: When the intent is recorded, ISO-8601.
            quantity: The quantity to send, defaulting to the order's own.

        Returns:
            What was established.

        Raises:
            ExecutionError: As `submit_order` does.
        """
        return submit_order(
            order,
            transport=self.transport,
            binding=self.binding,
            mapping=self.mapping,
            log=log,
            recorded_at=recorded_at,
            quantity=quantity,
        )
