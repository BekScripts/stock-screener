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

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ..errors import Code, ExecutionError
from ..state_machine import OrderState
from .cancellation import CancellationOutcome, CancellationResult
from .client_ids import client_order_id_for
from .environment import refuse_live
from .models import (
    ExternalAsset,
    ExternalExecutionFill,
    ExternalOrderSnapshot,
    ExternalPaperAccountSnapshot,
    ExternalPosition,
    decimal_or_none,
    require_decimal,
)
from .paper_proof import ProviderPaperEnvironmentProof
from .submission import ORDERS_PATH, SubmissionIntentLog, SubmissionResult, submit_order
from .transport import (
    READ_RETRY_LIMIT,
    ExternalReadEvidence,
    RequestMethod,
    read_with_retry,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from decimal import Decimal

    from ..models import Order
    from .binding import ExternalBrokerAccountBinding
    from .symbols import SymbolMapping
    from .transport import BrokerTransport

__all__ = [
    "ACCOUNT_PATH",
    "ACTIVITIES_PATH",
    "ASSETS_PATH",
    "POSITIONS_PATH",
    "PROVIDER",
    "AlpacaPaperAdapter",
]

#: The order-listing query that answers "what is working right now". Separate
#: from the history query because the two are different questions and P2.2C
#: conflated them: it asked for `status=all`, got 50 rows of mostly-closed
#: history, and reported the number as "orders at venue". Reconciliation cares
#: about *open* unexpected orders; a terminal order from last month is history.
OPEN_ORDER_QUERY: dict[str, str] = {"status": "open"}

#: The order-listing query that answers "what has this account ever done".
ORDER_HISTORY_QUERY: dict[str, str] = {"status": "all"}

#: Which provider this adapter speaks to.
PROVIDER = "ALPACA"

#: Read-only account path.
ACCOUNT_PATH = "/v2/account"

#: Read-only positions path.
POSITIONS_PATH = "/v2/positions"

#: Read-only executions path.
ACTIVITIES_PATH = "/v2/account/activities/FILL"

#: Read-only asset path. Every order precondition the frozen contract depends on
#: — tradable, active, US equity, fractionable — is read from here rather than
#: assumed, because the alternative is discovering a halted asset from a
#: rejection after an intent is already durable.
ASSETS_PATH = "/v2/assets"


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

    def account_evidence(
        self, *, received_at: str = ""
    ) -> ExternalReadEvidence[ExternalPaperAccountSnapshot]:
        """Read the account once, and return it with that read's own evidence.

        One request to one endpoint. The returned fingerprint is of the very
        response the snapshot was built from, so an artefact that reports a
        figure and a digest side by side is reporting them about the same
        observation.

        Positions are a separate endpoint and are not fetched here; the snapshot
        carries none. Use `positions_evidence`, or `read_account` when you want
        both combined.

        Args:
            received_at: When the read was made.

        Returns:
            The account snapshot and its evidence.

        Raises:
            ExecutionError: `BROKER_UNREACHABLE` if the account cannot be read;
                `BROKER_ACCOUNT_MISMATCH` if the provider answers for another
                account.
        """
        response = read_with_retry(self.transport, ACCOUNT_PATH, limit=self.read_limit)
        if not response.ok or not isinstance(response.body, dict):
            raise ExecutionError(
                Code.BROKER_UNREACHABLE,
                f"the account could not be read ({response.outcome} after "
                f"{response.attempts} attempts)",
            )
        snapshot = ExternalPaperAccountSnapshot.from_payload(response.body, ())
        self.binding.require_account(snapshot.account_id, context="reading the account")
        if snapshot.is_paper is False:
            raise ExecutionError(
                Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
                "the provider reports this account is not a paper account",
            )
        return ExternalReadEvidence.of(snapshot, response, received_at=received_at)

    def positions_evidence(
        self, *, received_at: str = ""
    ) -> ExternalReadEvidence[tuple[ExternalPosition, ...]]:
        """Read the account's holdings once, with that read's evidence.

        Args:
            received_at: When the read was made.

        Returns:
            The holdings and their evidence.

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
        if response.body is not None and not isinstance(response.body, list):
            raise ExecutionError(
                Code.REMOTE_STATE_UNKNOWN,
                "the positions endpoint did not answer with a list",
            )
        rows = response.body if isinstance(response.body, list) else []
        positions = tuple(
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
            for row in rows
            if isinstance(row, dict)
        )
        return ExternalReadEvidence.of(positions, response, received_at=received_at)

    def read_account(self) -> ExternalPaperAccountSnapshot:
        """Read the account and its positions.

        Two reads, one per endpoint — not two reads of one endpoint.

        Returns:
            The venue's view of the account. Evidence, never a book.

        Raises:
            ExecutionError: As `account_evidence` and `positions_evidence` do.
        """
        account = self.account_evidence().value
        positions = self.positions_evidence().value
        return replace(account, positions=positions)

    def prove_paper_environment(
        self, *, origin: str, verified_at: str = ""
    ) -> ProviderPaperEnvironmentProof:
        """Prove this is the paper account, from a real authenticated response.

        The replacement for a gate that could not fail. `account_evidence`
        raises unless the provider answered successfully *and* the account it
        answered for is the one the binding names, so reaching the end of this
        method is itself the evidence — and the proof records the response
        fingerprint that established it.

        Args:
            origin: The origin the transport actually reached. Supplied by the
                caller that built the transport, because the `BrokerTransport`
                protocol deliberately declares no attributes.
            verified_at: When the proof was taken.

        Returns:
            The proof.

        Raises:
            ExecutionError: As `account_evidence` does, plus
                `BROKER_ENVIRONMENT_NOT_PROVEN_PAPER` if the origin is not the
                approved paper origin.
        """
        evidence = self.account_evidence(received_at=verified_at)
        return ProviderPaperEnvironmentProof.from_account_evidence(
            provider=PROVIDER,
            origin=origin,
            endpoint=ACCOUNT_PATH,
            account_binding_fingerprint=self.binding.binding_fingerprint(),
            account_status=evidence.value.status,
            evidence=evidence,
            verified_at=verified_at,
        )

    def asset_evidence(
        self, symbol: str, *, received_at: str = ""
    ) -> ExternalReadEvidence[ExternalAsset]:
        """Read one instrument's trading status, with that read's evidence.

        Args:
            symbol: The venue symbol.
            received_at: When the read was made.

        Returns:
            The asset and its evidence.

        Raises:
            ExecutionError: `BROKER_ASSET_NOT_TRADABLE` if the venue has no such
                asset; `BROKER_UNREACHABLE` if it cannot be read. A failed read
                is never treated as "tradable" — the conservative default of
                every field is "not permitted".
        """
        response = read_with_retry(
            self.transport, f"{ASSETS_PATH}/{symbol.strip().upper()}", limit=self.read_limit
        )
        if response.not_found:
            raise ExecutionError(
                Code.BROKER_ASSET_NOT_TRADABLE,
                f"the venue lists no asset {symbol!r}",
            )
        if not response.ok or not isinstance(response.body, dict):
            raise ExecutionError(
                Code.BROKER_UNREACHABLE,
                f"{symbol}: the asset could not be read ({response.outcome})",
            )
        return ExternalReadEvidence.of(
            ExternalAsset.from_payload(response.body), response, received_at=received_at
        )

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

    def open_orders_evidence(
        self, *, received_at: str = ""
    ) -> ExternalReadEvidence[tuple[ExternalOrderSnapshot, ...]]:
        """Read the orders the venue currently has **working**, with evidence.

        `status=open`, which is the question reconciliation actually asks. P2.2C
        used `status=all` here and called the result "orders at venue" — on a
        real account that was 50 rows of mostly-terminal history presented as
        though it were live state.

        Args:
            received_at: When the read was made.

        Returns:
            The working orders and their evidence.

        Raises:
            ExecutionError: `BROKER_UNREACHABLE` if the listing cannot be read.
        """
        return self._order_listing(OPEN_ORDER_QUERY, received_at=received_at)

    def order_history_evidence(
        self, *, received_at: str = ""
    ) -> ExternalReadEvidence[tuple[ExternalOrderSnapshot, ...]]:
        """Read every order the account has ever had, with evidence.

        History, not live state. A terminal order here is evidence about the
        past and is not by itself a reconciliation break.

        Args:
            received_at: When the read was made.

        Returns:
            The orders and their evidence.

        Raises:
            ExecutionError: `BROKER_UNREACHABLE` if the listing cannot be read.
        """
        return self._order_listing(ORDER_HISTORY_QUERY, received_at=received_at)

    def _order_listing(
        self, query: dict[str, str], *, received_at: str
    ) -> ExternalReadEvidence[tuple[ExternalOrderSnapshot, ...]]:
        """Read one order listing.

        Args:
            query: The provider query.
            received_at: When the read was made.

        Returns:
            The orders and their evidence.

        Raises:
            ExecutionError: `BROKER_UNREACHABLE` if the listing cannot be read.
        """
        response = read_with_retry(self.transport, ORDERS_PATH, params=query, limit=self.read_limit)
        if not response.ok:
            raise ExecutionError(
                Code.BROKER_UNREACHABLE,
                f"the order listing could not be read ({response.outcome}); an empty "
                "listing is not substituted, as it would hide an untracked order",
            )
        rows = response.body if isinstance(response.body, list) else []
        orders = tuple(
            ExternalOrderSnapshot.from_payload(row) for row in rows if isinstance(row, dict)
        )
        return ExternalReadEvidence.of(orders, response, received_at=received_at)

    def read_open_orders(self) -> tuple[ExternalOrderSnapshot, ...]:
        """Read the orders the venue currently has working.

        Returns:
            The working orders.

        Raises:
            ExecutionError: `BROKER_UNREACHABLE` if the listing cannot be read.
        """
        return self.open_orders_evidence().value

    def read_fills(self, *, remote_order_id: str) -> tuple[ExternalExecutionFill, ...]:
        """Read the executions the venue reports for one *provider* order.

        Keyed on the venue's own order id, which is the only identifier Alpaca's
        activities endpoint filters on. P2.2 sent `client_order_id` here, and
        that is a worse failure than it looks: the endpoint does not recognise
        the parameter, so it ignores it and answers with the account's FILL
        activities broadly. The read then *looked* successful while returning
        other orders' fills, and the adapter stamped each of them with the
        requested order's client id. On an account with any prior activity that
        posts unrelated trades into this book.

        So the filter is the provider id, there is deliberately no fallback to
        the client id, and — because an ignored filter is exactly the kind of
        failure that is invisible from the response — every row is checked
        against the order it was requested for before it is admitted.

        Individual executions, never the order's average price. Two partials at
        different prices have one average and two cost-basis effects, and the
        book needs the second.

        Args:
            remote_order_id: The venue's order id, from a confirmed submission
                or a lookup. Not a local order id.

        Returns:
            The executions, in a total order, deduplicated by remote execution
            id — so reading the same window repeatedly yields the same set,
            which is what makes a sweep safe to repeat.

        Raises:
            ExecutionError: `REMOTE_PROVIDER_ORDER_ID_REQUIRED` if no provider
                order id was given; `BROKER_UNREACHABLE` if the executions
                cannot be read; `REMOTE_FILL_ORDER_MISMATCH` if the venue
                returns an execution belonging to another order;
                `REMOTE_FILL_CONFLICT` if one execution id arrives twice with
                different content; `REMOTE_EVENT_ORDER_AMBIGUOUS` if two
                executions cannot be put in a definite order.
        """
        return self.fills_evidence(remote_order_id=remote_order_id).value

    def fills_evidence(
        self, *, remote_order_id: str, received_at: str = ""
    ) -> ExternalReadEvidence[tuple[ExternalExecutionFill, ...]]:
        """Read one order's executions, with the evidence for that same read.

        Args:
            remote_order_id: The venue's order id.
            received_at: When the read was made.

        Returns:
            The executions and the fingerprint of the response they came from.

        Raises:
            ExecutionError: As `read_fills` does.
        """
        provider_order_id = remote_order_id.strip()
        if not provider_order_id:
            raise ExecutionError(
                Code.REMOTE_PROVIDER_ORDER_ID_REQUIRED,
                "reading executions needs the venue's own order id. The client "
                "order id is not accepted as a substitute: the activities endpoint "
                "does not filter on it, so sending it would return the account's "
                "other fills while appearing to have worked",
            )
        response = read_with_retry(
            self.transport,
            ACTIVITIES_PATH,
            params={"order_id": provider_order_id},
            limit=self.read_limit,
        )
        if not response.ok:
            raise ExecutionError(
                Code.BROKER_UNREACHABLE,
                f"{provider_order_id}: executions could not be read "
                f"({response.outcome}). An empty list is not substituted — it would "
                "read as the order having never filled",
            )
        rows = response.body if isinstance(response.body, list) else []
        seen: dict[str, ExternalExecutionFill] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not self._is_fill_activity(row):
                # A non-fill activity — a dividend, a fee, a journal entry. Not
                # an error: the endpoint can be asked for a type and still be
                # given a mixed page, and a fee is not an execution.
                continue
            self._require_belongs_to(row, provider_order_id)
            remote_fill_id = str(row.get("id", ""))
            fill = ExternalExecutionFill.from_payload(
                row,
                remote_fill_id=remote_fill_id,
                remote_order_id=str(row.get("order_id", "")),
                client_order_id="",
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
                f"{provider_order_id}: two executions share a complete ordering key, "
                "so the sequence they traded in cannot be established",
            )
        return ExternalReadEvidence.of(tuple(ordered), response, received_at=received_at)

    @staticmethod
    def _is_fill_activity(row: Mapping[str, Any]) -> bool:
        """Whether an activity row describes an execution.

        Args:
            row: The provider activity object.

        Returns:
            True for a fill or partial fill. Alpaca spells the kind in
            `activity_type` and the completeness in `type`, and either may be
            absent on a page that was not filtered by type, so a row is a fill
            only when something positively says so.
        """
        activity_type = str(row.get("activity_type", "")).upper()
        kind = str(row.get("type", "")).lower()
        if activity_type and activity_type != "FILL":
            return False
        if kind and kind not in {"fill", "partial_fill"}:
            return False
        return bool(activity_type) or kind in {"fill", "partial_fill"}

    def _require_belongs_to(self, row: Mapping[str, Any], provider_order_id: str) -> None:
        """Refuse an execution that is not this order's.

        The check that makes an ignored query filter survivable. If the venue
        answers with another order's execution — because the filter was not
        honoured, or a page was mixed — the row is refused by name rather than
        attributed to the order that was asked about.

        Args:
            row: The provider activity object.
            provider_order_id: The order the executions were requested for.

        Raises:
            ExecutionError: `REMOTE_FILL_ORDER_MISMATCH` if the row names a
                different order, names no order at all, or names a symbol this
                run never mapped.
        """
        row_order_id = str(row.get("order_id", "")).strip()
        if not row_order_id:
            raise ExecutionError(
                Code.REMOTE_FILL_ORDER_MISMATCH,
                f"an execution returned for {provider_order_id} names no order id, so "
                "it cannot be shown to belong to it",
            )
        if row_order_id != provider_order_id:
            raise ExecutionError(
                Code.REMOTE_FILL_ORDER_MISMATCH,
                f"the venue returned an execution for order {row_order_id} when asked "
                f"about {provider_order_id}. It is refused rather than attributed: "
                "the query filter was evidently not honoured, and crediting another "
                "order's fill to this one would put unauthorised quantity in the book",
            )
        symbol = str(row.get("symbol", "")).strip()
        if symbol and not self.mapping.knows_symbol(symbol):
            raise ExecutionError(
                Code.REMOTE_FILL_ORDER_MISMATCH,
                f"an execution for {provider_order_id} is in symbol {symbol!r}, which "
                "this run never mapped to an identity; it cannot be attributed",
            )

    def provider_order_id_for(self, order_id: str, *, log: SubmissionIntentLog) -> str:
        """Resolve a local order id to the venue's own order id, durably.

        The mapping is read from the submission resolution the protocol already
        wrote when the venue confirmed the order, so it survives a restart: the
        client order id is derived from the local order id, and the resolution
        filed under it records the provider's id.

        Args:
            order_id: The local order id.
            log: The durable submission intent log.

        Returns:
            The venue's order id.

        Raises:
            ExecutionError: `REMOTE_PROVIDER_ORDER_ID_REQUIRED` if no confirmed
                submission recorded one. Fails closed rather than guessing —
                an order with no recorded provider id has not been confirmed, and
                reading fills for it would be reading fills for nothing.
        """
        client_order_id = client_order_id_for(
            order_id, limit=self.binding.capabilities.client_order_id_limit
        )
        resolution = log.resolution_for(client_order_id)
        remote_order_id = str((resolution or {}).get("remote_order_id") or "").strip()
        if not remote_order_id:
            raise ExecutionError(
                Code.REMOTE_PROVIDER_ORDER_ID_REQUIRED,
                f"{order_id} has no recorded provider order id, so its executions "
                "cannot be read. A confirmed submission records one; an unresolved or "
                "rejected submission does not, and neither has fills to read",
            )
        return remote_order_id

    def read_fills_for_local_order(
        self, order_id: str, *, log: SubmissionIntentLog
    ) -> tuple[ExternalExecutionFill, ...]:
        """Read one local order's executions, resolving the provider id first.

        Args:
            order_id: The local order id.
            log: The durable submission intent log.

        Returns:
            The executions.

        Raises:
            ExecutionError: As `provider_order_id_for` and `read_fills` do.
        """
        return self.read_fills(remote_order_id=self.provider_order_id_for(order_id, log=log))

    def cancel_order(
        self,
        *,
        remote_order_id: str,
        log: SubmissionIntentLog,
        requested_at: str,
    ) -> CancellationResult:
        """Ask the venue to cancel one order, and prove what became of it.

        Three things were wrong with the P2.2 version and all three mattered.
        It built the path from the *client* order id, where Alpaca's path
        parameter is the provider UUID, so a cancel would have 404'd. It
        recorded no durable intent, so a crash during the request left nothing
        naming the order it may have cancelled. And it returned the HTTP result
        as though a 204 meant the order was terminal — which it does not: a
        cancel can be accepted and then lose a race to a fill.

        So the protocol mirrors submission: validate, record intent durably,
        send **one** DELETE, then establish the outcome by *reading*. A
        cancellation is never adopted from the mutation's own status code.

        Args:
            remote_order_id: The venue's order id. Not a local order id.
            log: The durable intent log.
            requested_at: When the cancellation is being requested, ISO-8601.

        Returns:
            What was established, including the venue's own view of the order.
            `REMOTE_STILL_OPEN` and `REMOTE_STATE_UNKNOWN` are both possible and
            neither is a cancellation.

        Raises:
            ExecutionError: `BROKER_CAPABILITY_UNSUPPORTED` if the venue does
                not support cancellation; `REMOTE_PROVIDER_ORDER_ID_REQUIRED` if
                no provider order id was given; `LIVE_TRADING_NOT_AUTHORIZED` if
                the binding is somehow not paper.
        """
        refuse_live(self.binding.environment, context=f"cancelling order {remote_order_id}")
        if not self.binding.capabilities.cancel_supported:
            raise ExecutionError(
                Code.BROKER_CAPABILITY_UNSUPPORTED,
                "this venue does not support cancelling an order",
            )
        provider_order_id = remote_order_id.strip()
        if not provider_order_id:
            raise ExecutionError(
                Code.REMOTE_PROVIDER_ORDER_ID_REQUIRED,
                "cancelling needs the venue's own order id; the client order id is "
                "not the path parameter Alpaca documents and would not match an order",
            )

        log.record_cancellation_intent(
            provider_order_id,
            requested_at=requested_at,
            binding_fingerprint=self.binding.binding_fingerprint(),
        )

        response = self.transport.request(
            RequestMethod.DELETE, f"{ORDERS_PATH}/{provider_order_id}"
        )

        # The mutation's own status is never the answer. Even a clean 204 is
        # only "the venue accepted the request"; what the order *became* comes
        # from reading it back.
        resolved = self.resolve_cancellation(provider_order_id)
        return CancellationResult(
            remote_order_id=provider_order_id,
            outcome=resolved.outcome,
            snapshot=resolved.snapshot,
            detail=(
                f"the cancellation request answered {response.outcome}"
                + (f" ({response.status_code})" if response.status_code else "")
                + f"; {resolved.detail}"
            ),
            mutating_requests=1,
            lookups=resolved.lookups,
            request_id=response.request_id,
        )

    def resolve_cancellation(self, provider_order_id: str) -> CancellationResult:
        """Establish what a cancelled order actually became, by reading.

        Reads only. Called after a cancellation request whatever its status, and
        callable again later for one that could not be settled — which is how an
        ambiguous DELETE is recovered without a second DELETE.

        Args:
            provider_order_id: The venue's order id.

        Returns:
            What the venue says. A terminal state is adopted; `NEW`,
            `ACCEPTED` or `PARTIALLY_FILLED` means the cancellation is not yet
            proven and is reported as `REMOTE_STILL_OPEN`; an unreadable order
            is `REMOTE_STATE_UNKNOWN` and stops progression.
        """
        response = read_with_retry(
            self.transport, f"{ORDERS_PATH}/{provider_order_id}", limit=self.read_limit
        )
        if response.not_found:
            return CancellationResult(
                remote_order_id=provider_order_id,
                outcome=CancellationOutcome.REMOTE_STATE_UNKNOWN,
                detail=(
                    "the venue reports no such order. That is not evidence of a "
                    "cancellation — an order that was never accepted and an order "
                    "that was cancelled are different facts, and this answer "
                    "distinguishes neither"
                ),
                lookups=response.attempts,
            )
        if not response.ok or not isinstance(response.body, dict):
            return CancellationResult(
                remote_order_id=provider_order_id,
                outcome=CancellationOutcome.REMOTE_STATE_UNKNOWN,
                detail=(
                    f"the order could not be read back ({response.outcome} after "
                    f"{response.attempts} attempts), so the cancellation is unproven. "
                    "No second DELETE is sent on this evidence"
                ),
                lookups=response.attempts,
            )
        snapshot = ExternalOrderSnapshot.from_payload(response.body)
        state = snapshot.state
        if state is OrderState.FILLED or snapshot.filled_quantity >= snapshot.quantity:
            outcome = CancellationOutcome.REMOTE_FILLED
            detail = (
                "the order filled before the cancellation took effect. Its fills "
                "stand and the order is FILLED, not CANCELLED"
            )
        elif state in {OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED}:
            outcome = CancellationOutcome.REMOTE_CANCELLED
            detail = f"the venue reports the order terminal as {snapshot.reading.provider_status}"
        else:
            outcome = CancellationOutcome.REMOTE_STILL_OPEN
            detail = (
                f"the venue still reports {snapshot.reading.provider_status}, so the "
                "cancellation is not complete. Any further attempt is a deliberate "
                "decision taken on this proven state, not a retry"
            )
        return CancellationResult(
            remote_order_id=provider_order_id,
            outcome=outcome,
            snapshot=snapshot,
            detail=detail,
            lookups=response.attempts,
        )

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
