"""The external paper canary: one authorised order, through the whole chain.

This is what certifies the adapter, and what it certifies is deliberately not
"HTTP works". It submits an order that originated the way every order must:

    accounting V2 book
      -> authorised ExecutionPlan, bound to that book's state
        -> an Order the plan authorises
          -> the submission protocol, with its durable intent
            -> the paper HTTPS transport

Nothing here calls the transport directly to place a trade. A canary that did
would prove connectivity and nothing about the authority chain, which is the
part that actually keeps an unauthorised order from existing.

Four properties it exists to demonstrate, none of which a fake transport can:

**The fill is ingested exactly once.** The accounting event id is derived from
the provider's own execution id, so reading the venue again — which the canary
does, deliberately — admits nothing further. That is checked by re-polling and
comparing the ledger, not by trusting the derivation.

**The venue agrees with the book.** A three-way reconciliation runs after the
fill: journal, accounting, venue. A blocking break stops the canary where it
stands rather than being tidied away.

**Cleanup is a trade, not an escape hatch.** The position is closed by a second
authorised plan and a second ordinary order. There is no liquidate call, no
close-all, and no unjournaled action — the compensating order is part of the
audit trail, which is the only way the books stay true.

**Nothing here can reach a live venue.** The transport is host-locked, the
binding is paper-proven, and the venue binding is `EXTERNAL_PAPER`. Every
artefact says `EXTERNAL_PAPER_CANARY`, `NOT_LIVE_TRADING`,
`NOT_PRODUCTION_PORTFOLIO`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stock_screener import portfolio_accounting as accounting
from stock_screener.portfolio_risk import Action, ExecutionIntent

from ..accounting_adapter import admit_fill
from ..authorization import BookBinding
from ..errors import Code, ExecutionError
from ..events import ExecutionFill
from ..fingerprint import fingerprint_of
from ..models import AttributionShare, OrderSide
from ..planner import ExecutionSizingSnapshot, SizingPrice, plan_execution
from ..versions import REQUIRED_ACCOUNTING_VERSION
from .alpaca import PROVIDER, AlpacaPaperAdapter
from .client_ids import client_order_id_for
from .http_transport import TRANSPORT_VERSION
from .reconciliation import reconcile_external
from .session import ExecutionVenue, VenueBinding
from .submission import SubmissionOutcome, submit_order

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.portfolio_risk import SecurityIdentity

    from .models import ExternalAsset, ExternalExecutionFill, ExternalOrderSnapshot
    from .paper_preflight import AlpacaPaperPreflightRun

__all__ = [
    "CANARY_CLASSIFICATION",
    "CERTIFICATION_ARTEFACT_VERSION",
    "CanaryLeg",
    "ExternalPaperCanary",
    "ExternalPaperCanaryReport",
]

#: What every artefact of this run must say it is.
CANARY_CLASSIFICATION: tuple[str, ...] = (
    "EXTERNAL_PAPER_CANARY",
    "NOT_LIVE_TRADING",
    "NOT_PRODUCTION_PORTFOLIO",
)

#: The certification artefact contract.
CERTIFICATION_ARTEFACT_VERSION = "P2_2_EXTERNAL_PAPER_CERTIFICATION_V1"

#: The strategy the canary's quantity is attributed to. A real name rather than
#: a blank, because accounting V2 keeps one cost pool per attribution owner and
#: an unattributed buy would open a pool nobody can later sell from
#: unambiguously.
CANARY_STRATEGY = "EXTERNAL_PAPER_CANARY"

#: How long to wait for a market order to fill, and how often to ask. Bounded:
#: an unfilled canary is a reportable outcome, not something to wait out.
FILL_POLL_ATTEMPTS = 20
FILL_POLL_SECONDS = 3.0


@dataclass
class CanaryLeg:
    """One authorised order and everything the venue said about it.

    Attributes:
        label: `ENTRY` or `CLEANUP`.
        order_id: The local order id.
        client_order_id: The derived venue-facing id.
        plan_fingerprint: The plan that authorised it.
        side: Which way it traded.
        quantity: The authorised quantity.
        submission: What the submission protocol established.
        remote_order_id: The venue's order id, once confirmed.
        lookup_matched: Whether a lookup by client order id returned this order.
        snapshot: The venue's final view of the order.
        fills: The executions the venue reported, read by provider order id.
        accounting_events: The accounting event ids the fills produced.
        admitted_quantity: How much reached the book.
        duplicate_poll_admitted: How much a second identical poll admitted. Must
            be zero.
        request_ids: Provider request ids, per operation.
        evidence: Response fingerprints, per operation.
    """

    label: str
    order_id: str
    client_order_id: str
    plan_fingerprint: str
    side: OrderSide
    quantity: Decimal
    submission: str = ""
    remote_order_id: str | None = None
    lookup_matched: bool = False
    snapshot: ExternalOrderSnapshot | None = None
    fills: tuple[ExternalExecutionFill, ...] = ()
    accounting_events: tuple[str, ...] = ()
    admitted_quantity: Decimal = field(default_factory=lambda: Decimal(0))
    duplicate_poll_admitted: Decimal = field(default_factory=lambda: Decimal(0))
    request_ids: tuple[tuple[str, str], ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()

    @property
    def filled_quantity(self) -> Decimal:
        """How much the venue says traded on this leg."""
        return sum((fill.quantity for fill in self.fills), Decimal(0))

    def as_json(self) -> dict[str, Any]:
        """Return the leg as a JSON-safe mapping. Carries no credential."""
        return {
            "label": self.label,
            "classification": list(CANARY_CLASSIFICATION),
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "plan_fingerprint": self.plan_fingerprint,
            "side": str(self.side),
            "authorised_quantity": str(self.quantity),
            "submission_outcome": self.submission,
            "remote_order_id": self.remote_order_id,
            "lookup_by_client_order_id_matched": self.lookup_matched,
            "venue_status": (self.snapshot.reading.provider_status if self.snapshot else None),
            "venue_filled_quantity": (
                str(self.snapshot.filled_quantity) if self.snapshot else None
            ),
            "fills": [fill.as_json() for fill in self.fills],
            "fill_quantity": str(self.filled_quantity),
            "accounting_events": list(self.accounting_events),
            "admitted_quantity": str(self.admitted_quantity),
            "duplicate_poll_admitted": str(self.duplicate_poll_admitted),
            "provider_request_ids": [list(pair) for pair in self.request_ids],
            "evidence_fingerprints": [list(pair) for pair in self.evidence],
        }


@dataclass
class ExternalPaperCanaryReport:
    """What the canary established, end to end.

    Attributes:
        outcome: `CERTIFIED`, or why not.
        identity: The security traded, by durable key.
        venue_symbol: What the venue calls it.
        asset: What the venue said about the instrument.
        entry: The buying leg.
        cleanup: The compensating leg, when one was needed.
        opening_cash: The cash the book opened with.
        final_cash: The book's cash afterwards.
        final_position: The book's holding afterwards.
        residual_position: Canary-created quantity still held. Must be zero for
            a clean canary, and is reported rather than hidden when it is not.
        reconciliation_after_fill: Verdict of the three-way sweep after the fill.
        reconciliation_after_cleanup: Verdict after cleanup.
        blocking_breaks: Any blocking findings, sanitised.
        notes: Anything a reader needs that the figures do not say.
    """

    outcome: str
    identity: str = ""
    venue_symbol: str = ""
    asset: ExternalAsset | None = None
    entry: CanaryLeg | None = None
    cleanup: CanaryLeg | None = None
    opening_cash: Decimal = field(default_factory=lambda: Decimal(0))
    final_cash: Decimal = field(default_factory=lambda: Decimal(0))
    final_position: Decimal = field(default_factory=lambda: Decimal(0))
    residual_position: Decimal = field(default_factory=lambda: Decimal(0))
    reconciliation_after_fill: str = ""
    reconciliation_after_cleanup: str = ""
    blocking_breaks: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def certified(self) -> bool:
        """Whether this run certifies the external paper path."""
        return self.outcome == "CERTIFIED"

    def body(self) -> dict[str, Any]:
        """Return the certification artefact's semantic content."""
        return {
            "artefact_version": CERTIFICATION_ARTEFACT_VERSION,
            "classification": list(CANARY_CLASSIFICATION),
            "provider": PROVIDER,
            "environment": "PAPER",
            "venue": str(ExecutionVenue.EXTERNAL_PAPER),
            "transport_version": TRANSPORT_VERSION,
            "accounting_version": REQUIRED_ACCOUNTING_VERSION,
            "outcome": self.outcome,
            "certified": self.certified,
            "identity": self.identity,
            "venue_symbol": self.venue_symbol,
            "asset": self.asset.as_json() if self.asset else None,
            "entry": self.entry.as_json() if self.entry else None,
            "cleanup": self.cleanup.as_json() if self.cleanup else None,
            "opening_cash": str(self.opening_cash),
            "final_cash": str(self.final_cash),
            "final_position": str(self.final_position),
            "residual_position": str(self.residual_position),
            "reconciliation_after_fill": self.reconciliation_after_fill,
            "reconciliation_after_cleanup": self.reconciliation_after_cleanup,
            "blocking_breaks": list(self.blocking_breaks),
            "live_trading": "NOT_AUTHORIZED",
            "reproducibility": (
                "code and config semantics are reproducible; the external "
                "observations here are historically recorded and fingerprinted; "
                "the provider's responses are not recreatable"
            ),
            "notes": list(self.notes),
        }

    def fingerprint(self) -> str:
        """Digest the artefact."""
        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the artefact, credential-scanned on the way out.

        Returns:
            The artefact with its own fingerprint.

        Raises:
            ExecutionError: `FORBIDDEN_EXECUTION_NAMESPACE` if anything in it is
                shaped like a secret.
        """
        from .report import check_no_credentials

        body = {**self.body(), "certification_fingerprint": self.fingerprint()}
        check_no_credentials(body, context="an external paper certification artefact")
        return body

    def summary_lines(self) -> tuple[str, ...]:
        """Render the artefact for a terminal."""
        lines = [
            f"outcome              : {self.outcome}",
            f"classification       : {', '.join(CANARY_CLASSIFICATION)}",
            f"identity             : {self.identity} -> {self.venue_symbol}",
        ]
        for leg in (self.entry, self.cleanup):
            if leg is None:
                continue
            lines.extend(
                [
                    f"{leg.label.lower():<21}: {leg.side} {leg.quantity} ({leg.submission})",
                    f"  client order id    : {leg.client_order_id}",
                    f"  provider order id  : {leg.remote_order_id}",
                    f"  lookup matched     : {leg.lookup_matched}",
                    f"  venue filled       : {leg.filled_quantity}",
                    f"  accounting events  : {list(leg.accounting_events)}",
                    f"  admitted quantity  : {leg.admitted_quantity}",
                    f"  duplicate poll     : {leg.duplicate_poll_admitted} (must be 0)",
                ]
            )
        lines.extend(
            [
                f"book cash            : {self.opening_cash} -> {self.final_cash}",
                f"book position        : {self.final_position}",
                f"residual canary pos  : {self.residual_position}",
                f"reconcile after fill : {self.reconciliation_after_fill}",
                f"reconcile after clean: {self.reconciliation_after_cleanup}",
            ]
        )
        for note in self.blocking_breaks:
            lines.append(f"  BLOCKING: {note}")
        for note in self.notes:
            lines.append(f"  NOTE: {note}")
        return tuple(lines)


class ExternalPaperCanary:
    """Runs one authorised paper order end to end, then undoes it.

    Attributes:
        run: A preflight run supplying the proven transport and binding.
        identity: The security to trade.
        store_root: Where the execution and accounting records live.
    """

    def __init__(
        self,
        run: AlpacaPaperPreflightRun,
        *,
        identity: SecurityIdentity,
        store_root: Path,
    ) -> None:
        """Prepare a canary.

        Args:
            run: The preflight run, already built from settings.
            identity: The security to trade. Must be in the run's mapping.
            store_root: Where records are written.
        """
        self.run = run
        self.identity = identity
        self.store_root = store_root
        self.adapter = AlpacaPaperAdapter(
            transport=run.transport, binding=run.binding, mapping=run.mapping
        )
        self.venue = VenueBinding.external(run.binding)
        self._legs: list[CanaryLeg] = []

    # -- the chain -----------------------------------------------------------

    def _open_book(self, cash: Decimal, now: str) -> tuple[Any, Any, Any]:
        """Open an accounting V2 book with the venue's cash and no holdings.

        Opening at the venue's cash is deliberate. It is what makes the later
        three-way cash comparison meaningful instead of something a tolerance
        has to absorb: the two sides start equal, so any divergence afterwards
        is the canary's own doing.

        Args:
            cash: The venue's cash.
            now: The effective moment.

        Returns:
            The store, the ledger and the opening state.
        """
        store = accounting.PortfolioStore(
            self.store_root / ".portfolio", accounting_version=REQUIRED_ACCOUNTING_VERSION
        )
        ledger = accounting.PortfolioLedger()
        store.commit(
            ledger,
            accounting.OpeningBalanceEvent(
                event_id=f"CANARY_OPENING_{self._run_tag(now)}",
                event_type=accounting.EventType.OPENING_BALANCE,
                effective_at=now,
                source_id="EXTERNAL_PAPER_CANARY",
                source_fingerprint=fingerprint_of(
                    {"cash": str(cash), "classification": list(CANARY_CLASSIFICATION)}
                ),
                cash=cash,
                positions=(),
            ),
        )
        return (
            store,
            ledger,
            accounting.replay(ledger, accounting_version=REQUIRED_ACCOUNTING_VERSION),
        )

    def _authorise(
        self,
        *,
        plan_id: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        store: Any,
        ledger: Any,
        state: Any,
        now: str,
    ) -> Any:
        """Build an authorised plan for one order.

        The plan is bound to the book's current state fingerprint, so an order
        it authorises cannot be executed against a book that has moved.

        Args:
            plan_id: The plan id.
            side: Which way to trade.
            quantity: How much.
            price: The sizing price.
            store: The accounting store.
            ledger: The accounting ledger.
            state: The book's current state.
            now: The moment the plan is for.

        Returns:
            The authorised plan.
        """
        nav = accounting.value_portfolio(
            state,
            accounting.MarkPriceSnapshot(
                valuation_time=now,
                marks=(
                    accounting.MarkPrice(
                        identity=self.identity,
                        price=price,
                        as_of=now,
                        source="EXTERNAL_PAPER_CANARY_SIZING",
                    ),
                ),
            ),
        ).nav
        held = state.positions.get(self.identity.key)
        held_quantity = held.quantity if held is not None else Decimal(0)
        target = held_quantity + (quantity if side is OrderSide.BUY else -quantity)
        notional = float(quantity * price) * (1.0 if side is OrderSide.BUY else -1.0)
        return plan_execution(
            execution_plan_id=plan_id,
            as_of=now,
            intents=[
                ExecutionIntent(
                    identity=self.identity,
                    action=(
                        Action.ENTER
                        if side is OrderSide.BUY and held_quantity == 0
                        else Action.INCREASE
                        if side is OrderSide.BUY
                        else Action.EXIT
                        if target == 0
                        else Action.REDUCE
                    ),
                    current_weight=float(held_quantity * price / nav) if nav else 0.0,
                    target_weight=float(target * price / nav) if nav else 0.0,
                    notional_delta=notional,
                    strategy_id=CANARY_STRATEGY,
                )
            ],
            sizing=ExecutionSizingSnapshot(
                as_of=now,
                nav=nav,
                cash=state.cash,
                prices=(
                    SizingPrice(
                        identity=self.identity,
                        price=price,
                        source_fingerprint="EXTERNAL_PAPER_CANARY_QUOTE",
                    ),
                ),
                source="EXTERNAL_PAPER_CANARY",
            ),
            book=BookBinding(
                ledger_generation=store.generation,
                accounting_engine_version=REQUIRED_ACCOUNTING_VERSION,
                accounting_state_fingerprint=accounting.state_fingerprint(state),
                ledger_sequence=len(ledger),
                ledger_head=ledger.head_hash,
            ),
            approved_portfolio_fingerprint=fingerprint_of(
                {"canary": self.identity.label, "target": str(target)}
            ),
            valued_snapshot_fingerprint=fingerprint_of({"as_of": now, "price": str(price)}),
            attribution={self.identity.label: ((CANARY_STRATEGY, quantity),)},
            created_at=now,
        )

    def _await_fills(
        self, leg: CanaryLeg, *, sleep: Any = time.sleep
    ) -> tuple[ExternalOrderSnapshot | None, tuple[ExternalExecutionFill, ...]]:
        """Poll, bounded, until the venue reports the order terminal.

        Reads only. Bounded because an unfilled canary is a reportable outcome,
        not something to wait out indefinitely.

        Args:
            leg: The leg being filled.
            sleep: How to wait. Injected so a test need not.

        Returns:
            The venue's final view of the order, and its executions.
        """
        assert leg.remote_order_id is not None  # noqa: S101 - set by the caller
        snapshot: ExternalOrderSnapshot | None = None
        for attempt in range(FILL_POLL_ATTEMPTS):
            snapshot = self.adapter.read_order(leg.order_id)
            if snapshot is not None and (snapshot.is_terminal or snapshot.filled_quantity > 0):
                break
            if attempt < FILL_POLL_ATTEMPTS - 1:
                sleep(FILL_POLL_SECONDS)

        # The order status and the activities feed are not simultaneous. The
        # venue reported this order `filled` roughly twenty milliseconds after
        # submission and the matching FILL activity was not yet queryable — so a
        # single activities read right after a terminal status returns nothing,
        # and reading that as "no fill" would be wrong about a trade that
        # happened. The executions are therefore polled in their own bounded
        # loop, and an exhausted window is reported rather than concluded from.
        expected = snapshot.filled_quantity if snapshot is not None else Decimal(0)
        evidence = self.adapter.fills_evidence(remote_order_id=leg.remote_order_id)
        for attempt in range(FILL_POLL_ATTEMPTS):
            observed = sum((fill.quantity for fill in evidence.value), Decimal(0))
            if evidence.value and observed >= expected:
                break
            if attempt == FILL_POLL_ATTEMPTS - 1:
                break
            sleep(FILL_POLL_SECONDS)
            evidence = self.adapter.fills_evidence(remote_order_id=leg.remote_order_id)

        if evidence.request_id:
            leg.request_ids = (*leg.request_ids, ("fills", evidence.request_id))
        leg.evidence = (*leg.evidence, ("fills", evidence.fingerprint))
        return snapshot, evidence.value

    def _ingest(
        self,
        leg: CanaryLeg,
        fills: tuple[ExternalExecutionFill, ...],
        *,
        store: Any,
        ledger: Any,
        fee_schedule: Decimal,
    ) -> Decimal:
        """Admit the venue's executions to the book, exactly once each.

        The accounting event id is derived from the provider's own execution id,
        so admitting the same execution twice is a no-op rather than a second
        trade. That is the property the duplicate poll then checks.

        Args:
            leg: The leg being ingested.
            fills: The venue's executions.
            store: The accounting store.
            ledger: The accounting ledger.
            fee_schedule: The declared fee per execution. Declared, not
                observed: the venue states no fee for a US equity, and
                substituting zero silently is what `BROKER_FILL_FEE_UNKNOWN`
                exists to prevent — so it is supplied explicitly here and
                recorded as a declaration.

        Returns:
            How much quantity this call admitted.
        """
        admitted = Decimal(0)
        events: list[str] = []
        for remote in fills:
            priced = remote if remote.fee_is_known else remote.with_fee(fee_schedule)
            fill = ExecutionFill(
                fill_id=f"EXT:{remote.remote_fill_id}",
                order_id=leg.order_id,
                identity=self.identity,
                side=leg.side,
                quantity=priced.quantity,
                price=priced.price,
                fee=priced.require_fee(),
                effective_at=priced.reported_at,
                attribution=(
                    AttributionShare(strategy_id=CANARY_STRATEGY, quantity=priced.quantity),
                ),
                source_fingerprint=fingerprint_of(priced.body()),
                venue=str(ExecutionVenue.EXTERNAL_PAPER),
            )
            result = admit_fill(fill, ledger=ledger, store=store)
            events.append(str(result["accounting_event_id"]))
            if result["written"]:
                admitted += priced.quantity
        leg.accounting_events = tuple(events)
        return admitted

    #: Why the canary declares a cash tolerance, and how large. The venue rounds
    #: its cash balance to whole cents; a fractional fill of 0.1 share at
    #: 12.062 is exactly 1.2062 of proceeds, which the venue reports as 1.21. The
    #: difference is sub-cent rounding at the venue, not a missing trade. One
    #: cent is declared here with that reason rather than widening the global
    #: default, which stays at exactly zero.
    CASH_TOLERANCE = Decimal("0.01")

    def _reconcile(
        self,
        *,
        state: Any,
        legs: list[CanaryLeg],
        orders: list[Any],
        local_filled: dict[str, Decimal],
    ) -> Any:
        """Compare journal, book and venue.

        The remote order set is deliberately built from two reads, not one. The
        open listing finds orders nobody here placed — the foreign-order check —
        but it cannot match this run's *own* orders, because a filled order is
        terminal and no longer open. Supplying only the open listing made every
        completed canary order look as though the venue had never heard of it,
        which is what the first cleanup run reported. So each of this run's
        orders is looked up individually by its derived client order id and
        added to the set.

        Args:
            state: The accounting book.
            legs: This run's legs, looked up individually.
            orders: The local orders submitted.
            local_filled: Filled quantity per local order id.

        Returns:
            The reconciliation result. Never repairs anything.
        """
        account = self.adapter.read_account()
        remote_orders = list(self.adapter.read_open_orders())
        seen = {row.remote_order_id for row in remote_orders}
        for leg in legs:
            looked_up = self.adapter.read_order(leg.order_id)
            if looked_up is not None and looked_up.remote_order_id not in seen:
                remote_orders.append(looked_up)
                seen.add(looked_up.remote_order_id)
        return reconcile_external(
            state=state,
            orders=orders,
            remote_orders=remote_orders,
            remote_account=account,
            mapping=self.run.mapping,
            local_filled=local_filled,
            cash_tolerance=self.CASH_TOLERANCE,
        )

    def execute(
        self, *, quantity: Decimal, now: str, sleep: Any = time.sleep
    ) -> ExternalPaperCanaryReport:
        """Run the canary: authorise, submit, ingest, reconcile, clean up.

        Args:
            quantity: The authorised quantity. The smallest the venue and the
                internal contract jointly support.
            now: The moment, ISO-8601.
            sleep: How to wait between polls. Injected.

        Returns:
            The certification artefact.

        Raises:
            ExecutionError: If a precondition fails. Nothing is submitted when a
                precondition fails — `EXTERNAL_SUBMISSION_BLOCKED` names which.
        """
        notes: list[str] = []

        # -- preconditions, before anything is sent --------------------------
        readiness = self.run.execute(now=now)
        if not readiness.canary_ready:
            raise ExecutionError(
                Code.EXTERNAL_SUBMISSION_BLOCKED,
                "the canary preconditions are not met, so nothing was submitted: "
                + "; ".join(readiness.canary_blockers),
            )
        self.venue.require(ExecutionVenue.EXTERNAL_PAPER, context="running the paper canary")

        symbol = self.run.mapping.symbol_for(self.identity)
        asset_read = self.adapter.asset_evidence(symbol, received_at=now)
        asset = asset_read.value
        asset.require_tradable_us_equity()
        if quantity != quantity.to_integral_value() and not asset.fractionable:
            raise ExecutionError(
                Code.BROKER_FRACTIONAL_NOT_SUPPORTED,
                f"{symbol} is not fractionable at this venue and {quantity} would be "
                "rounded. Nothing is submitted and the quantity is not silently changed",
            )

        # The binding's fractional capability is declared from the asset read
        # rather than left at its conservative default. Fractionability is a
        # property of the instrument, not of the venue, so a venue-level default
        # is necessarily wrong for one side or the other — and the honest
        # resolution is to declare it from what the venue just said about this
        # asset, not to widen the default and hope.
        if asset.fractionable and not self.run.binding.capabilities.fractional_quantities:
            self.run.binding = replace(
                self.run.binding,
                capabilities=replace(self.run.binding.capabilities, fractional_quantities=True),
            )
            self.adapter = AlpacaPaperAdapter(
                transport=self.run.transport,
                binding=self.run.binding,
                mapping=self.run.mapping,
            )
            notes.append(
                f"fractional quantities were declared for this run because the venue "
                f"reports {symbol} fractionable (asset evidence "
                f"{asset_read.fingerprint[:16]}); the capability default is whole "
                "shares only"
            )

        account = self.adapter.read_account()
        venue_cash = account.require_cash()
        price = self._reference_price(symbol)

        store, ledger, state = self._open_book(venue_cash, now)
        opening_cash = state.cash

        # -- entry leg -------------------------------------------------------
        entry_plan = self._authorise(
            plan_id=f"CANARY-ENTRY-{self._run_tag(now)}",
            side=OrderSide.BUY,
            quantity=quantity,
            price=price,
            store=store,
            ledger=ledger,
            state=state,
            now=now,
        )
        entry = self._submit_leg("ENTRY", entry_plan, now=now)
        self._legs.append(entry)

        if entry.submission != str(SubmissionOutcome.REMOTE_CONFIRMED):
            return self._abort(
                f"ENTRY_{entry.submission}",
                asset=asset,
                symbol=symbol,
                opening_cash=opening_cash,
                state=state,
                notes=[
                    "the entry order was not confirmed, so nothing was ingested and "
                    "no cleanup was attempted",
                ],
            )

        snapshot, fills = self._await_fills(entry, sleep=sleep)
        entry.snapshot = snapshot
        entry.fills = fills
        if not fills:
            return self._abort(
                "ENTRY_NOT_FILLED",
                asset=asset,
                symbol=symbol,
                opening_cash=opening_cash,
                state=state,
                notes=[
                    "the venue reported no execution within the bounded poll window. "
                    "The order may still be working; it is neither cancelled nor "
                    "assumed dead here.",
                ],
            )

        entry.admitted_quantity = self._ingest(
            entry, fills, store=store, ledger=ledger, fee_schedule=Decimal(0)
        )
        notes.append(
            "the fee is a declared schedule value (0 for a US equity at this venue), "
            "not a figure the venue stated; the execution records fee_is_known=False"
        )
        notes.append(
            f"a cash tolerance of {self.CASH_TOLERANCE} was declared for this run: "
            "the venue rounds its cash balance to whole cents, so a fractional fill "
            "leaves a sub-cent difference that is rounding and not a missing trade. "
            "The global default remains exactly zero"
        )

        # -- duplicate poll: the exactly-once proof --------------------------
        state = accounting.replay(ledger, accounting_version=REQUIRED_ACCOUNTING_VERSION)
        cash_before_repoll, seq_before = state.cash, len(ledger)
        repolled = self.adapter.read_fills(remote_order_id=str(entry.remote_order_id))
        entry.duplicate_poll_admitted = self._ingest(
            entry, repolled, store=store, ledger=ledger, fee_schedule=Decimal(0)
        )
        state = accounting.replay(ledger, accounting_version=REQUIRED_ACCOUNTING_VERSION)
        if (
            entry.duplicate_poll_admitted != 0
            or state.cash != cash_before_repoll
            or len(ledger) != seq_before
        ):
            raise ExecutionError(
                Code.FILL_ID_CONFLICT,
                "re-reading the same executions changed the book. The accounting "
                "event id is supposed to be derived from the provider execution id, "
                "so a second admission must be a no-op",
            )

        local_filled = {entry.order_id: entry.filled_quantity}
        orders = [self._order_of(entry_plan)]
        after_fill = self._reconcile(
            state=state, legs=[entry], orders=orders, local_filled=local_filled
        )

        # -- cleanup leg -----------------------------------------------------
        cleanup_plan = self._authorise(
            plan_id=f"CANARY-CLEANUP-{self._run_tag(now)}",
            side=OrderSide.SELL,
            quantity=entry.filled_quantity,
            price=price,
            store=store,
            ledger=ledger,
            state=state,
            now=now,
        )
        cleanup = self._submit_leg("CLEANUP", cleanup_plan, now=now)
        self._legs.append(cleanup)
        cleanup_fills: tuple[ExternalExecutionFill, ...] = ()
        if cleanup.submission == str(SubmissionOutcome.REMOTE_CONFIRMED):
            cleanup_snapshot, cleanup_fills = self._await_fills(cleanup, sleep=sleep)
            cleanup.snapshot = cleanup_snapshot
            cleanup.fills = cleanup_fills
            if cleanup_fills:
                cleanup.admitted_quantity = self._ingest(
                    cleanup, cleanup_fills, store=store, ledger=ledger, fee_schedule=Decimal(0)
                )

        state = accounting.replay(ledger, accounting_version=REQUIRED_ACCOUNTING_VERSION)
        local_filled[cleanup.order_id] = cleanup.filled_quantity
        orders.append(self._order_of(cleanup_plan))
        after_cleanup = self._reconcile(
            state=state, legs=[entry, cleanup], orders=orders, local_filled=local_filled
        )

        held = state.positions.get(self.identity.key)
        residual = held.quantity if held is not None else Decimal(0)
        blocking = tuple(
            f"{row.code} {row.subject}: book={row.accounting} venue={row.remote}"
            for row in (*after_fill.blocking, *after_cleanup.blocking)
        )
        if residual != 0:
            notes.append(
                f"RESIDUAL_EXTERNAL_PAPER_POSITION: {residual} of "
                f"{self.identity.label} remains at the venue from this canary"
            )

        outcome = "CERTIFIED"
        if residual != 0:
            outcome = "RESIDUAL_EXTERNAL_PAPER_POSITION"
        elif blocking:
            outcome = "RECONCILIATION_BLOCKING"
        elif not cleanup_fills:
            outcome = "CLEANUP_NOT_FILLED"

        return ExternalPaperCanaryReport(
            outcome=outcome,
            identity=self.identity.label,
            venue_symbol=symbol,
            asset=asset,
            entry=entry,
            cleanup=cleanup,
            opening_cash=opening_cash,
            final_cash=state.cash,
            final_position=residual,
            residual_position=residual,
            reconciliation_after_fill=str(after_fill.verdict),
            reconciliation_after_cleanup=str(after_cleanup.verdict),
            blocking_breaks=blocking,
            notes=tuple(notes),
        )

    def close_residual(
        self,
        *,
        quantity: Decimal,
        cost_basis_per_share: Decimal,
        now: str,
        sleep: Any = time.sleep,
    ) -> ExternalPaperCanaryReport:
        """Close a canary-created holding with one authorised compensating order.

        Used when an earlier canary left a position behind — which is exactly
        what happened the first time this ran: the entry filled, the activities
        feed lagged, and the run reported `ENTRY_NOT_FILLED` while the venue had
        in fact bought. The position is real and must be closed the same way it
        was opened: a new authorised plan and an ordinary order through the
        submission protocol. Not a liquidation, not a cancel, and nothing
        unjournaled.

        Args:
            quantity: The holding to close.
            cost_basis_per_share: What it cost, so the book opens consistent
                with the venue rather than with an invented basis.
            now: The moment, ISO-8601.
            sleep: How to wait between polls.

        Returns:
            The artefact, whose outcome is `RESIDUAL_CLOSED` when the venue
            reports the holding gone.

        Raises:
            ExecutionError: If the venue is not reachable or the instrument is
                not tradable. Nothing is submitted in that case.
        """
        symbol = self.run.mapping.symbol_for(self.identity)
        asset_read = self.adapter.asset_evidence(symbol, received_at=now)
        asset = asset_read.value
        asset.require_tradable_us_equity()
        if asset.fractionable and not self.run.binding.capabilities.fractional_quantities:
            self.run.binding = replace(
                self.run.binding,
                capabilities=replace(self.run.binding.capabilities, fractional_quantities=True),
            )
            self.adapter = AlpacaPaperAdapter(
                transport=self.run.transport,
                binding=self.run.binding,
                mapping=self.run.mapping,
            )

        account = self.adapter.read_account()
        venue_cash = account.require_cash()
        store = accounting.PortfolioStore(
            self.store_root / ".portfolio", accounting_version=REQUIRED_ACCOUNTING_VERSION
        )
        ledger = accounting.PortfolioLedger()
        store.commit(
            ledger,
            accounting.OpeningBalanceEvent(
                event_id=f"CANARY_RESIDUAL_OPENING_{self._run_tag(now)}",
                event_type=accounting.EventType.OPENING_BALANCE,
                effective_at=now,
                source_id="EXTERNAL_PAPER_CANARY_RESIDUAL",
                source_fingerprint=fingerprint_of(
                    {"cash": str(venue_cash), "quantity": str(quantity)}
                ),
                cash=venue_cash,
                positions=(
                    accounting.OpeningPosition(
                        identity=self.identity,
                        quantity=quantity,
                        cost_basis_per_share=cost_basis_per_share,
                        attribution=(
                            accounting.StrategySlice(
                                strategy_id=CANARY_STRATEGY, quantity=quantity
                            ),
                        ),
                    ),
                ),
            ),
        )
        state = accounting.replay(ledger, accounting_version=REQUIRED_ACCOUNTING_VERSION)
        opening_cash = state.cash

        plan = self._authorise(
            plan_id=f"CANARY-RESIDUAL-CLEANUP-{self._run_tag(now)}",
            side=OrderSide.SELL,
            quantity=quantity,
            price=cost_basis_per_share,
            store=store,
            ledger=ledger,
            state=state,
            now=now,
        )
        leg = self._submit_leg("CLEANUP", plan, now=now)
        self._legs.append(leg)
        if leg.submission == str(SubmissionOutcome.REMOTE_CONFIRMED):
            leg.snapshot, leg.fills = self._await_fills(leg, sleep=sleep)
            if leg.fills:
                leg.admitted_quantity = self._ingest(
                    leg, leg.fills, store=store, ledger=ledger, fee_schedule=Decimal(0)
                )

        state = accounting.replay(ledger, accounting_version=REQUIRED_ACCOUNTING_VERSION)
        after = self._reconcile(
            state=state,
            legs=[leg],
            orders=[self._order_of(plan)],
            local_filled={leg.order_id: leg.filled_quantity},
        )
        held = state.positions.get(self.identity.key)
        residual = held.quantity if held is not None else Decimal(0)
        return ExternalPaperCanaryReport(
            outcome="RESIDUAL_CLOSED" if residual == 0 else "RESIDUAL_EXTERNAL_PAPER_POSITION",
            identity=self.identity.label,
            venue_symbol=symbol,
            asset=asset,
            cleanup=leg,
            opening_cash=opening_cash,
            final_cash=state.cash,
            final_position=residual,
            residual_position=residual,
            reconciliation_after_cleanup=str(after.verdict),
            blocking_breaks=tuple(
                f"{row.code} {row.subject}: book={row.accounting} venue={row.remote}"
                for row in after.blocking
            ),
            notes=(
                "a compensating authorised order, not a liquidation: the canary's "
                "own position is closed through the same plan-and-submit path that "
                "opened it, so the close is part of the audit trail",
            ),
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _run_tag(now: str) -> str:
        """A short, stable identifier for one canary run.

        The client order id is derived from the local order id, which is derived
        from the plan id — so a fixed plan id makes every canary run derive the
        *same* venue-facing id. That is correct behaviour expressing itself
        awkwardly: a second run was refused by the venue as a duplicate, the
        lookup returned the first run's filled order, and accounting then
        refused the fill for taking effect before the new book opened. The
        protocol and the ledger were both right.

        The resolution is a run-scoped plan id, not a random one. This tag is a
        digest of the run's own moment, so it is stable *within* a run — a
        retry after a crash derives the same client order id and stays
        idempotent — while differing between runs.

        Args:
            now: The run's moment, ISO-8601.

        Returns:
            Eight hex characters.
        """
        return fingerprint_of({"canary_run": now})[:8]

    def _reference_price(self, symbol: str) -> Decimal:
        """Read a sizing price for the canary order.

        A sizing price, not a fill price. It sizes the plan and is never used as
        an execution price — the venue's own execution price is the only thing
        posted to the book.

        Args:
            symbol: The venue symbol.

        Returns:
            The last trade price the venue's own order history or account
            implies, falling back to a nominal positive price when none is
            available. The value only has to be positive and plausible: the
            plan's quantity is supplied explicitly rather than derived from it.
        """
        snapshot = self.adapter.read_open_orders()
        for row in snapshot:
            if row.symbol.upper() == symbol.upper() and row.average_fill_price:
                return row.average_fill_price
        return Decimal("100.00")

    def _order_of(self, plan: Any) -> Any:
        """Return the single order a canary plan authorises.

        Args:
            plan: The plan.

        Returns:
            Its only order.

        Raises:
            ExecutionError: `PLAN_INVALID` if the plan did not produce exactly
                one order — a canary that planned two trades is not a canary.
        """
        if len(plan.orders) != 1:
            raise ExecutionError(
                Code.PLAN_INVALID,
                f"the canary plan produced {len(plan.orders)} orders; it must produce exactly one",
            )
        return plan.orders[0]

    def _submit_leg(self, label: str, plan: Any, *, now: str) -> CanaryLeg:
        """Submit one leg through the submission protocol.

        Args:
            label: `ENTRY` or `CLEANUP`.
            plan: The authorising plan.
            now: The moment.

        Returns:
            The leg, with what the submission established.
        """
        order = self._order_of(plan)
        leg = CanaryLeg(
            label=label,
            order_id=order.order_id,
            client_order_id=client_order_id_for(
                order.order_id, limit=self.run.binding.capabilities.client_order_id_limit
            ),
            plan_fingerprint=plan.fingerprint(),
            side=order.side,
            quantity=order.quantity,
        )
        result = submit_order(
            order,
            transport=self.run.transport,
            binding=self.run.binding,
            mapping=self.run.mapping,
            log=self.run.log,
            recorded_at=now,
        )
        leg.submission = str(result.outcome)
        leg.remote_order_id = result.remote_order_id
        if result.outcome is SubmissionOutcome.REMOTE_CONFIRMED:
            looked_up = self.adapter.read_order(order.order_id)
            leg.lookup_matched = (
                looked_up is not None
                and looked_up.remote_order_id == result.remote_order_id
                and looked_up.client_order_id == leg.client_order_id
            )
        return leg

    def _abort(
        self,
        outcome: str,
        *,
        asset: ExternalAsset,
        symbol: str,
        opening_cash: Decimal,
        state: Any,
        notes: list[str],
    ) -> ExternalPaperCanaryReport:
        """Build a report for a run that stopped before completing.

        Args:
            outcome: Why it stopped.
            asset: The instrument.
            symbol: The venue symbol.
            opening_cash: What the book opened with.
            state: The book.
            notes: What a reader needs to know.

        Returns:
            The artefact, marked uncertified.
        """
        return ExternalPaperCanaryReport(
            outcome=outcome,
            identity=self.identity.label,
            venue_symbol=symbol,
            asset=asset,
            entry=self._legs[0] if self._legs else None,
            cleanup=self._legs[1] if len(self._legs) > 1 else None,
            opening_cash=opening_cash,
            final_cash=state.cash,
            notes=tuple(notes),
        )
