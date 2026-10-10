"""The submission authority: a frozen decision, through every authority, once.

This is the callable P2.3.1B left injected. It is the only place in the runtime
where an order reaches a venue, and it reaches one by *asking* the authorities
that already exist rather than by constructing anything itself:

    frozen StrategyDecision      what was wanted, taken at the month-end close
      -> StrategyIntent          S1.0's own contract, re-derived from the record
      -> PortfolioDecision       risk's verdict: what is permitted now
      -> RiskTransformation      the desired-to-permitted mapping, recorded
      -> ExecutionPlan           the execution engine's authorised orders
      -> AlpacaPaperAdapter      the certified mutation, one order at a time
      -> admit_fill              accounting, exactly once per fill
      -> reconciliation          the three records, compared and never repaired

Two properties are worth stating because they are what make this safe rather
than merely working.

**The ranking is never recomputed.** The intents come from the persisted
decision. Everything downstream runs against *current* state, which is correct —
the selection is frozen, the sizing is not, and risk is entitled to a different
answer this morning than it would have given last night. What it is not
entitled to do is change which securities were selected.

**The gate is consulted before every single order**, not once per run. A kill
that lands between the first and second order must stop the second, and the
only way to guarantee that is to ask again each time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stock_screener.execution.errors import Code as ExecCode
from stock_screener.execution.errors import ExecutionError

from .errors import Code, RuntimeBindingError
from .risk_transformation import transformation_for

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.strategies import StrategyDecision

    from .binding import PaperOperationsRuntimeBinding
    from .risk_transformation import RiskTransformation

__all__ = ["SUBMISSION_AUTHORITY_VERSION", "RuntimeSubmissionAuthority"]

#: The submission authority contract.
SUBMISSION_AUTHORITY_VERSION = "RUNTIME_SUBMISSION_AUTHORITY_V1"

#: How an unrecoverable execution-journal classification is reported. The same
#: codes the execution engine's own recovery raises, so one condition does not
#: get two names depending on which caller found it.
_JOURNAL_REFUSALS: dict[str, Any] = {
    "TRUNCATION_DETECTED": ExecCode.EXECUTION_JOURNAL_TRUNCATION_DETECTED,
    "HEAD_MISMATCH": ExecCode.EXECUTION_JOURNAL_HEAD_ANCHOR_MISMATCH,
    "ANCHOR_MISSING": ExecCode.EXECUTION_ANCHOR_MISSING,
    "GENERATION_MISMATCH": ExecCode.EXECUTION_GENERATION_MISMATCH,
}


@dataclass
class RuntimeSubmissionAuthority:
    """Takes one frozen decision through the certified chain.

    Attributes:
        root: The operations root.
        adapter: The certified paper adapter. The only object here that reaches
            a network, and the only one that may mutate.
        risk_policy: The approved risk policy, bound by the runtime.
        risk_policy_fingerprint: Its digest, as the runtime recorded it.
        marks: Sizing prices by identity label. Supplied rather than fetched:
            this authority sizes against prices somebody else established, so a
            replay uses the same numbers.
        intent_log: The durable submission intent log.
        journal: The operations run journal. Supplied so this authority records
            what it actually did — the plan it authorised, the gate value it
            observed before each mutation, the orders that became real and the
            fills presented to accounting. None of those facts were durable
            before, which is why no soak gate could read them.
        clock: The operational clock, for the journal.
        fee_schedule: The fee to declare for an execution the venue priced
            without one. `None` declares nothing, and such a fill is then
            refused with `BROKER_FILL_FEE_UNKNOWN` rather than admitted at
            zero. A commission-free venue is an explicit `Decimal(0)`, because
            "the venue charges nothing" and "nobody checked" must not look the
            same in the book.
        transformation: Filled in once risk has spoken.
        submitted: How many orders reached the venue.
    """

    root: Path
    adapter: Any
    risk_policy: Any
    risk_policy_fingerprint: str
    marks: dict[str, Decimal] = field(default_factory=dict)
    intent_log: Any = None
    journal: Any = None
    clock: Any = None
    fee_schedule: Decimal | None = None
    transformation: RiskTransformation | None = field(default=None, init=False)
    submitted: int = field(default=0, init=False)

    def __call__(
        self,
        *,
        decision: StrategyDecision,
        gate: Any,
        account: str,
        run_id: str,
        now: str,
        binding: PaperOperationsRuntimeBinding,
    ) -> dict[str, Any]:
        """Execute a frozen decision, or report why nothing was executed.

        Args:
            decision: The frozen decision. Never re-decided.
            gate: The coordinator's submission gate, asked before every order.
            account: The expected account binding.
            run_id: The operational run.
            now: The operational instant.
            binding: The runtime binding.

        Returns:
            What happened: planned and submitted counts, the risk
            transformation, and the reconciliation verdicts.

        Raises:
            RuntimeBindingError: `RISK_POLICY_NOT_BOUND` without an approved
                policy; `RISK_OUTPUT_EXCEEDS_STRATEGY_AUTHORITY` if risk
                granted exposure the strategy never requested;
                `STATE_DRIFT_SINCE_APPROVAL` if the book moved between the plan
                and the submission. Each refuses before any order is sent.
        """
        from stock_screener.portfolio_risk import evaluate
        from stock_screener.strategies import CompounderMonthlyStrategy

        if self.risk_policy is None:
            raise RuntimeBindingError(
                Code.RISK_POLICY_NOT_BOUND,
                "this runtime names no approved risk policy, so no exposure can be "
                "authorised. A default policy is never invented: a defaulted risk "
                "limit is the one nobody meant",
            )

        # The intents come from the record. The strategy is asked only to
        # re-derive its own contract from the frozen decision — not to rank
        # anything, which is why no bundle and no score source appear here.
        intents = list(CompounderMonthlyStrategy().intents(decision))

        portfolio, ledger, state = self._open_book(binding)
        snapshot = self._portfolio_snapshot(state, now=now)
        risk = evaluate(intents, snapshot, self.risk_policy)
        # `PortfolioDecision.approved` is non-optional: the engine always
        # returns a portfolio, and a refusal shows up as an empty or reduced
        # target set rather than as an absent one. So there is no "risk
        # rejected" branch here — the risk-constrained outcome below is how
        # "permitted nothing" arrives.
        approved = risk.approved

        self.transformation = transformation_for(
            decision=decision,
            approved=approved,
            risk_policy_fingerprint=self.risk_policy_fingerprint,
            risk_decisions=getattr(approved, "risk_decisions", ()),
        )
        self._record_transformation(decision=decision, now=now, run_id=run_id)

        if self.transformation.outcome == "RISK_CONSTRAINED_NO_ACTION":
            return self._no_action(
                decision=decision,
                reason="COMPLETED_NO_ACTION_RISK_CONSTRAINED",
                detail=(
                    "every requested exposure was validly reduced to zero by the risk "
                    f"policy {self.risk_policy_fingerprint[:16]}…. The strategy is not "
                    "broken, no holding is forced and no other name is increased"
                ),
            )

        execution_intents = [row for row in risk.execution_intents if self._is_actionable(row)]
        if not execution_intents:
            return self._no_action(
                decision=decision,
                reason="COMPLETED_NO_ACTION_ALREADY_AT_TARGET",
                detail="the book already holds the approved targets; nothing to trade",
            )

        plan = self._plan(
            execution_intents=execution_intents,
            approved=approved,
            portfolio=portfolio,
            ledger=ledger,
            state=state,
            run_id=run_id,
            now=now,
            strategy_id=decision.strategy_id,
        )

        self._record_plan_authorized(plan=plan, decision=decision, approved=approved, run_id=run_id)

        # State drift. The plan is bound to the book's fingerprint at planning
        # time; if the book has moved since, the plan authorises quantities
        # against capital that is no longer there. Checked immediately before
        # the first order rather than once at the start of the run.
        self._require_no_drift(plan=plan, portfolio=portfolio, binding=binding)

        # §14: the execution journal of record. Opened before the first order,
        # so a journal that cannot be trusted stops the run *before* a mutation
        # rather than after one.
        execution_store, execution_journal = self._open_execution_journal(binding)

        results: list[dict[str, Any]] = []
        for order in plan.orders:
            context = f"order {len(results) + 1} of {len(plan.orders)} for {decision.decision_id}"
            # The gate is consulted per order, and the value it observed for
            # *this* order is recorded before the order is sent. A refusal is
            # recorded too: the gate that asks "did a mutation happen without a
            # permitting check" needs both the permits and the refusals, and a
            # session-level result reused across orders would prove neither.
            try:
                gate.require(context=context, expected_account=account)
            except Exception as exc:
                self._record_gate_check(
                    run_id=run_id,
                    plan=plan,
                    order=order,
                    context=context,
                    permitted=False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
                raise
            self._record_gate_check(
                run_id=run_id, plan=plan, order=order, context=context, permitted=True
            )
            # The order exists in the record before it exists at the venue.
            # A crash between the two then leaves a created-but-unsubmitted
            # order, which is recoverable; the reverse leaves an order at a
            # venue that no local record knows about, which is not.
            self._journal_order_created(
                store=execution_store,
                journal=execution_journal,
                plan=plan,
                order=order,
                now=now,
            )
            outcome = self._submit_one(order=order, now=now)
            results.append(outcome)
            self.submitted += 1
            self._journal_order_submitted(
                store=execution_store,
                journal=execution_journal,
                plan=plan,
                order=order,
                now=now,
                acknowledged=bool(outcome["remote_order_id"]),
            )
            self._record_order_submitted(
                run_id=run_id, plan=plan, decision=decision, order=order, outcome=outcome
            )

        fills = self._ingest(
            results=results,
            portfolio=portfolio,
            ledger=ledger,
            run_id=run_id,
            execution_store=execution_store,
            execution_journal=execution_journal,
        )
        return {
            "planned_orders": len(plan.orders),
            "orders_submitted": self.submitted,
            "orders_filled": fills["admitted"],
            "external_mutations": self.submitted,
            "risk_transformation": self.transformation.as_json(),
            "execution_plan_fingerprint": plan.fingerprint(),
            "detail": (
                f"{self.submitted} order(s) through the certified path for "
                f"{len(self.transformation.approved_identities)} risk-approved name(s)"
            ),
        }

    # -- the chain, one authority at a time -------------------------------

    def _open_book(self, binding: PaperOperationsRuntimeBinding) -> tuple[Any, Any, Any]:
        """Open this runtime's accounting book at its bound generation."""
        from stock_screener import portfolio_accounting as accounting
        from stock_screener.portfolio_accounting import PortfolioStore

        from .binding import RUNTIME_DIRECTORY

        store = PortfolioStore(
            self.root / RUNTIME_DIRECTORY / "portfolio",
            accounting_version=binding.accounting_engine_version,
            generation=binding.accounting_generation,
        )
        ledger = store.load_ledger()
        return store, ledger, accounting.replay(ledger)

    def _open_execution_journal(self, binding: PaperOperationsRuntimeBinding) -> tuple[Any, Any]:
        """Open this runtime's execution journal at its bound generation.

        The journal already existed — the bootstrap creates it and every health
        gate verifies it — but nothing had ever appended to it, so the record of
        record for operational execution was permanently empty while orders went
        to a venue and fills entered the book. Opening it here is what gives
        `verify()` something to verify.

        Returns:
            The store and the journal loaded from it.

        Raises:
            ExecutionError: Whatever the store's recovery classification
                refuses. A journal that is ahead of its anchor, truncated or of
                the wrong lineage fails closed: each means a person has to
                decide what happened, and guessing would put an order outside
                the record.
        """
        from stock_screener.execution import ExecutionStore

        from .binding import RUNTIME_DIRECTORY

        store = ExecutionStore(
            self.root / RUNTIME_DIRECTORY / "execution",
            generation=binding.execution_journal_generation,
        )
        journal = store.load_journal()
        result = store.classify()
        status = str(result["status"])
        if status == "RECOVERY_REQUIRED":
            # The crash-between-record-and-anchor case the anchor exists for.
            # Advancing it is recovery, not repair: the record is already
            # durable and the anchor is catching up to what it witnessed.
            store.write_anchor(store.anchor_for(journal, journal.derive()))
        elif not result["consistent"]:
            # The same four fail-closed classifications the execution engine's
            # own recovery refuses, with the same codes. Each means a person has
            # to decide what happened, and proceeding would put an order
            # outside the record of record.
            raise ExecutionError(
                _JOURNAL_REFUSALS.get(status, ExecCode.EXECUTION_ANCHOR_MISSING),
                f"the runtime execution journal classifies as {status}: "
                f"{result.get('detail', '')}. Operational execution does not "
                "proceed against a journal whose completeness is unestablished",
            )
        return store, journal

    def _portfolio_snapshot(self, state: Any, *, now: str) -> Any:
        """Build the risk engine's view of the book as it stands."""
        from stock_screener.portfolio_risk import PortfolioSnapshot, Position

        positions = tuple(
            Position(
                identity=row.identity,
                quantity=float(row.quantity),
                mark_price=float(self.marks.get(row.identity.label, Decimal(0))),
                cost_basis=None,
            )
            for row in state.positions.values()
            if self.marks.get(row.identity.label) is not None
        )
        return PortfolioSnapshot(as_of=now, cash=float(state.cash), positions=positions)

    def _plan(
        self,
        *,
        execution_intents: list[Any],
        approved: Any,
        portfolio: Any,
        ledger: Any,
        state: Any,
        run_id: str,
        now: str,
        strategy_id: str,
    ) -> Any:
        """Ask the execution engine for an authorised plan.

        The plan is bound to the book's state fingerprint, which is what makes
        an order it authorises unexecutable against a book that has moved.

        Every order is attributed to the strategy that requested it. Supplying
        the attribution here rather than at ingest is what lets the plan itself
        say whose quantity this is: a fill is then reconciled against the plan
        that authorised it, instead of against an owner the submitter named
        afterwards.
        """
        from stock_screener import portfolio_accounting as accounting
        from stock_screener.execution import (
            BookBinding,
            ExecutionSizingSnapshot,
            SizingPrice,
            fingerprint_of,
            plan_execution,
        )
        from stock_screener.paper_operations import REQUIRED_ACCOUNTING_VERSION

        marks = tuple(
            accounting.MarkPrice(
                identity=row.identity,
                price=self.marks[row.identity.label],
                as_of=now,
                source="RUNTIME_SIZING",
            )
            for row in execution_intents
            if row.identity.label in self.marks
        )
        nav = accounting.value_portfolio(
            state, accounting.MarkPriceSnapshot(valuation_time=now, marks=marks)
        ).nav
        return plan_execution(
            execution_plan_id=f"{run_id}-plan",
            as_of=now,
            intents=execution_intents,
            sizing=ExecutionSizingSnapshot(
                as_of=now,
                nav=nav,
                cash=state.cash,
                prices=tuple(
                    SizingPrice(
                        identity=row.identity,
                        price=self.marks[row.identity.label],
                        source_fingerprint="RUNTIME_SIZING_MARK",
                    )
                    for row in execution_intents
                    if row.identity.label in self.marks
                ),
                source="ALPHATRADER_PAPER_RUNTIME",
            ),
            book=BookBinding(
                ledger_generation=portfolio.generation,
                accounting_engine_version=REQUIRED_ACCOUNTING_VERSION,
                accounting_state_fingerprint=accounting.state_fingerprint(state),
                ledger_sequence=len(ledger),
                ledger_head=ledger.head_hash,
            ),
            approved_portfolio_fingerprint=str(approved.fingerprint()),
            valued_snapshot_fingerprint=fingerprint_of(
                {"nav": str(nav), "as_of": now, "source": "ALPHATRADER_PAPER_RUNTIME"}
            ),
            attribution={
                row.identity.label: [(strategy_id, Decimal(1))] for row in execution_intents
            },
        )

    def _require_no_drift(
        self, *, plan: Any, portfolio: Any, binding: PaperOperationsRuntimeBinding
    ) -> None:
        """Refuse unless the book is still the one the plan was authorised against.

        Raises:
            RuntimeBindingError: `STATE_DRIFT_SINCE_APPROVAL`. This is not a
                risk reduction and must not be confused with one: the plan's
                quantities were derived from capital and holdings that have
                since changed, so submitting them would act on a book that no
                longer exists. The strategy is never re-ranked to resolve it.
        """
        from stock_screener import portfolio_accounting as accounting

        current = accounting.state_fingerprint(accounting.replay(portfolio.load_ledger()))
        if current != plan.book.accounting_state_fingerprint:
            raise RuntimeBindingError(
                Code.STATE_DRIFT_SINCE_APPROVAL,
                f"the accounting state was {plan.book.accounting_state_fingerprint[:16]}… "
                f"when this plan was authorised and is {current[:16]}… now. The plan's "
                "quantities were sized against capital that has moved, so nothing is "
                "submitted. This is state drift, not a risk reduction, and the "
                "strategy selection is not re-ranked to resolve it",
            )

    def _submit_one(self, *, order: Any, now: str) -> dict[str, Any]:
        """Submit one authorised order through the certified path.

        The adapter's `submit` is the only mutating call, and it delegates to
        `submit_order` — the durable-intent-then-lookup protocol that never
        resends. Nothing here touches a transport.
        """
        from stock_screener.execution.external import SubmissionIntentLog

        log = self.intent_log or SubmissionIntentLog(self.root / "submissions")
        result = self.adapter.submit(order, log=log, recorded_at=now)
        return {
            "order": order,
            "outcome": str(getattr(result, "outcome", "")),
            "remote_order_id": str(getattr(result, "remote_order_id", "") or ""),
            "result": result,
        }

    def _ingest(
        self,
        *,
        results: list[dict[str, Any]],
        portfolio: Any,
        ledger: Any,
        run_id: str = "",
        execution_store: Any = None,
        execution_journal: Any = None,
    ) -> dict[str, Any]:
        """Read each order's fills and admit them to accounting exactly once.

        A fill that is not visible yet is not a fill that did not happen — the
        P2.2E lesson. The adapter's bounded fill read is used rather than one
        poll concluding "no fill".
        """
        from stock_screener.execution import admit_fill
        from stock_screener.execution.external import execution_fill_from

        admitted = 0
        for row in results:
            remote = row["remote_order_id"]
            if not remote:
                continue
            order = row["order"]
            for provider_fill in self.adapter.fills_evidence(remote_order_id=remote).value:
                # The venue reports its own execution and its own order id.
                # Accounting admits a local fill against a local order, so the
                # translation is explicit and the local order id comes from the
                # order this authority sent — never from the venue's echo of it.
                fill = execution_fill_from(
                    provider_fill,
                    order_id=order.order_id,
                    identity=order.identity,
                    side=order.side,
                    strategy_id=self._strategy_id(order),
                    fee_schedule=self.fee_schedule,
                )
                outcome = admit_fill(fill, ledger=ledger, store=portfolio)
                written = bool(outcome.get("written"))
                if written:
                    admitted += 1
                # The accounting link, in the execution journal as well as the
                # operations journal: one says a fill was applied and which
                # ledger event it became, the other says an operator can see it
                # happened. Neither is a second ledger — the quantity lives in
                # the accounting book alone.
                self._journal_fill_applied(
                    store=execution_store,
                    journal=execution_journal,
                    order=order,
                    fill=fill,
                    outcome=outcome,
                    now=fill.effective_at,
                )
                self._record_fill_ingested(
                    run_id=run_id,
                    provider_fill=provider_fill,
                    fill=fill,
                    remote_order_id=remote,
                    admitted=written,
                    accounting_event_id=str(outcome.get("accounting_event_id", "") or ""),
                )
        return {"admitted": admitted}

    @staticmethod
    def _strategy_id(order: Any) -> str:
        """Which strategy the filled quantity belongs to.

        Read from the order's own attribution rather than from the binding: the
        planner already decided whose quantity this is, and asking a second
        authority would let the two disagree about ownership of one fill.

        Raises:
            RuntimeBindingError: `RUNTIME_BINDING_INVALID` if the order carries
                no attribution. A fill is never attributed to a default owner.
        """
        shares = tuple(getattr(order, "attribution", ()) or ())
        if len(shares) != 1:
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_INVALID,
                f"order {getattr(order, 'order_id', '?')} carries "
                f"{len(shares)} attribution share(s); this authority runs one "
                "strategy and splits nothing, so a fill it cannot attribute to "
                "exactly one owner is refused rather than assigned to a default",
            )
        return str(shares[0].strategy_id)

    # -- the execution journal of record (§14) ----------------------------

    def _journal(self, *, store: Any, journal: Any, event: Any) -> Any:
        """Commit one execution event, or do nothing without a journal.

        The commit is `ExecutionStore.commit`, so the ordering is the certified
        one: admissibility decided, record written and fsynced, then the head
        anchor advanced. A refusal propagates — unlike the operations-journal
        sensors, an execution event that the state machine rejects means this
        authority and the record disagree about what is happening, and the safe
        reading of that is to stop.
        """
        if store is None or journal is None:
            return None
        return store.commit(journal, event)

    def _journal_order_created(
        self, *, store: Any, journal: Any, plan: Any, order: Any, now: str
    ) -> None:
        """Record that an authorised order exists, before it is sent."""
        from stock_screener.execution.events import ExecutionEventType, OrderCreatedEvent

        self._journal(
            store=store,
            journal=journal,
            event=OrderCreatedEvent(
                event_id=f"CREATED:{order.order_id}",
                event_type=ExecutionEventType.ORDER_CREATED,
                order_id=order.order_id,
                execution_plan_id=plan.execution_plan_id,
                occurred_at=now,
                recorded_at=now,
                order=order,
            ),
        )

    def _journal_order_submitted(
        self,
        *,
        store: Any,
        journal: Any,
        plan: Any,
        order: Any,
        now: str,
        acknowledged: bool,
    ) -> None:
        """Record the submission, and the venue's acceptance when it answered.

        The acknowledgement is a separate fact, as the lifecycle requires: a
        submission whose fate is unknown leaves the order SUBMITTED and
        unacknowledged, which is exactly the state an ambiguous POST should
        produce. Only a venue that answered with the order advances it.
        """
        from stock_screener.execution.events import (
            ExecutionEventType,
            OrderAcknowledgedEvent,
            OrderSubmittedEvent,
        )
        from stock_screener.execution.external import ExecutionVenue

        venue = str(ExecutionVenue.EXTERNAL_PAPER)
        self._journal(
            store=store,
            journal=journal,
            event=OrderSubmittedEvent(
                event_id=f"SUBMITTED:{order.order_id}",
                event_type=ExecutionEventType.ORDER_SUBMITTED,
                order_id=order.order_id,
                execution_plan_id=plan.execution_plan_id,
                occurred_at=now,
                recorded_at=now,
                venue=venue,
            ),
        )
        if not acknowledged:
            return
        self._journal(
            store=store,
            journal=journal,
            event=OrderAcknowledgedEvent(
                event_id=f"ACKNOWLEDGED:{order.order_id}",
                event_type=ExecutionEventType.ORDER_ACKNOWLEDGED,
                order_id=order.order_id,
                execution_plan_id=plan.execution_plan_id,
                occurred_at=now,
                recorded_at=now,
                venue=venue,
            ),
        )

    def _journal_fill_applied(
        self, *, store: Any, journal: Any, order: Any, fill: Any, outcome: Any, now: str
    ) -> None:
        """Record the fill, then that it reached the book.

        Two events, deliberately. `ORDER_FILLED` says a quantity traded;
        `FILL_APPLIED` says accounting durably admitted it and names the ledger
        event. Keeping them apart is what makes the handoff recoverable — a
        crash between them leaves a fill the journal knows about and can replay
        into the book, where one combined event would lose the distinction
        between "it traded" and "the book has it".
        """
        from stock_screener.execution.events import (
            ExecutionEventType,
            FillAppliedEvent,
            OrderFilledEvent,
        )

        self._journal(
            store=store,
            journal=journal,
            event=OrderFilledEvent(
                event_id=f"FILLED:{fill.fill_id}",
                event_type=ExecutionEventType.ORDER_FILLED,
                order_id=order.order_id,
                execution_plan_id=str(getattr(order, "execution_plan_id", "")),
                occurred_at=now,
                recorded_at=now,
                fill=fill,
            ),
        )
        self._journal(
            store=store,
            journal=journal,
            event=FillAppliedEvent(
                event_id=f"APPLIED:{fill.fill_id}",
                event_type=ExecutionEventType.FILL_APPLIED,
                order_id=order.order_id,
                execution_plan_id=str(getattr(order, "execution_plan_id", "")),
                occurred_at=now,
                recorded_at=now,
                fill_id=fill.fill_id,
                accounting_event_id=str(outcome.get("accounting_event_id", "")),
                accounting_sequence=int(outcome.get("accounting_sequence", 0) or 0),
                accounting_state_fingerprint=str(outcome.get("accounting_state_fingerprint", "")),
            ),
        )

    # -- the sensors -------------------------------------------------------

    def _emit(self, *, event: Any, run_id: str, detail: str, evidence: Any) -> None:
        """Append one operational event, if a journal was supplied.

        Journaling is best-effort *after* a mutation and mandatory before one:
        `_record_gate_check` is called before the order is sent and its failure
        propagates, while `_record_order_submitted` runs afterwards and must not
        turn a completed order into an exception.
        """
        if self.journal is None or self.clock is None:
            return
        self.journal.append(
            clock=self.clock,
            event_type=event,
            run_id=run_id,
            detail=detail,
            evidence=tuple(evidence),
        )

    def _record_plan_authorized(
        self, *, plan: Any, decision: Any, approved: Any, run_id: str
    ) -> None:
        """Record that a plan passed portfolio and risk authority.

        Binds the frozen decision the plan consumed. That binding is what lets
        a later reader prove execution used decision X rather than a decision
        ranked fresh that morning — the re-ranking gate has no other evidence.
        """
        from stock_screener.execution import fingerprint_of

        body = {
            "execution_plan_fingerprint": plan.fingerprint(),
            "strategy_decision_fingerprint": decision.fingerprint(),
            "strategy_decision_id": decision.decision_id,
            "strategy_fingerprint": decision.strategy_fingerprint,
            "approved_portfolio_fingerprint": str(approved.fingerprint()),
            "risk_policy_fingerprint": self.risk_policy_fingerprint,
            "transformation_fingerprint": (
                self.transformation.fingerprint() if self.transformation else ""
            ),
            "accounting_state_fingerprint": plan.book.accounting_state_fingerprint,
            "ledger_generation": plan.book.ledger_generation,
            "orders_planned": len(plan.orders),
        }
        self._emit(
            event=_event("PLAN_AUTHORIZED"),
            run_id=run_id,
            detail=(f"{len(plan.orders)} order(s) authorised from decision {decision.decision_id}"),
            evidence=(
                ("plan_authorized", fingerprint_of(body)),
                ("decision", decision.fingerprint()),
                ("plan", plan.fingerprint()),
            ),
        )

    def _record_gate_check(
        self,
        *,
        run_id: str,
        plan: Any,
        order: Any,
        context: str,
        permitted: bool,
        detail: str = "",
    ) -> None:
        """Record the gate value observed immediately before one mutation."""
        from stock_screener.execution import fingerprint_of

        body = {
            "execution_plan_fingerprint": plan.fingerprint(),
            "client_order_id": str(getattr(order, "client_order_id", "") or ""),
            "order_id": str(getattr(order, "order_id", "") or ""),
            "context": context,
            "permitted": permitted,
            "detail": detail,
            "per_mutation": True,
        }
        self._emit(
            event=_event("SUBMISSION_GATE_CHECKED"),
            run_id=run_id,
            detail=(
                f"{'permitted' if permitted else 'REFUSED'}: {context}"
                + (f" — {detail}" if detail else "")
            ),
            evidence=(
                ("gate_check", fingerprint_of(body)),
                ("permitted", str(permitted)),
            ),
        )

    def _record_order_submitted(
        self, *, run_id: str, plan: Any, decision: Any, order: Any, outcome: dict[str, Any]
    ) -> None:
        """Record that a remote order became known to exist.

        Deliberately not "a request was attempted" — an attempt whose fate is
        unknown is recorded by the request audit as UNRESOLVED and settled by
        the submission protocol asking the venue about the derived client order
        id. Collapsing the two would make an ambiguous POST indistinguishable
        from a confirmed order, which is the one distinction that matters.
        """
        from stock_screener.execution import fingerprint_of

        # The client order id lives on the durable submission intent, not on
        # the planned order: it is *derived* from the local order id by the
        # certified protocol, and reading it from the intent records the value
        # the venue was actually asked about rather than one re-derived here.
        intent = getattr(outcome.get("result"), "intent", None)
        body = {
            "execution_plan_fingerprint": plan.fingerprint(),
            "strategy_decision_fingerprint": decision.fingerprint(),
            "client_order_id": str(getattr(intent, "client_order_id", "") or ""),
            "order_id": str(order.order_id),
            "identity": str(order.identity.label),
            "side": str(order.side),
            "quantity": str(order.quantity),
            "remote_order_id": outcome.get("remote_order_id", ""),
            "submission_outcome": outcome.get("outcome", ""),
            "provider_mode": "PAPER",
        }
        self._emit(
            event=_event("ORDER_SUBMITTED"),
            run_id=run_id,
            detail=(
                f"{body['submission_outcome']} remote={body['remote_order_id'] or '?'} "
                f"client={body['client_order_id']}"
            ),
            evidence=(
                ("order_submitted", fingerprint_of(body)),
                ("remote_order_id", str(body["remote_order_id"])),
                ("client_order_id", str(body["client_order_id"])),
            ),
        )

    def _record_fill_ingested(
        self,
        *,
        run_id: str,
        provider_fill: Any,
        fill: Any,
        remote_order_id: str,
        admitted: bool,
        accounting_event_id: str,
        detail: str = "",
    ) -> None:
        """Record every provider execution presented to accounting admission.

        Including the duplicates that were correctly ignored. Without that, a
        reader cannot distinguish "never saw a duplicate" from "saw one and
        handled it safely", and only the second is evidence that the
        idempotency works.
        """
        from stock_screener.execution import fingerprint_of

        # §13's chain of custody, all three links in one record: what the
        # venue called this execution, what it became locally, and which
        # accounting event it was admitted as. Each is read from the object
        # that actually owns it, so a renamed field breaks the test rather
        # than silently recording an empty string.
        body = {
            "provider_execution_id": str(provider_fill.remote_fill_id),
            "remote_order_id": remote_order_id,
            "client_order_id": str(provider_fill.client_order_id),
            "execution_fill_id": str(fill.fill_id),
            "local_order_id": str(fill.order_id),
            "identity": str(fill.identity.label),
            "quantity": str(fill.quantity),
            "price": str(fill.price),
            "fee": str(fill.fee),
            "fee_stated_by_venue": bool(provider_fill.fee_is_known),
            "admission_result": "ADMITTED" if admitted else "DUPLICATE_IGNORED",
            "accounting_event_id": accounting_event_id,
            "detail": detail,
        }
        self._emit(
            event=_event("FILL_INGESTED"),
            run_id=run_id,
            detail=(
                f"{body['admission_result']} execution={body['provider_execution_id']} "
                f"accounting={accounting_event_id or '-'}"
            ),
            evidence=(
                ("fill_ingested", fingerprint_of(body)),
                ("provider_execution_id", str(body["provider_execution_id"])),
                ("execution_fill_id", str(body["execution_fill_id"])),
                ("admission_result", str(body["admission_result"])),
                ("accounting_event_id", accounting_event_id),
            ),
        )

    def _is_actionable(self, row: Any) -> bool:
        """Whether an execution intent implies a trade.

        A `HOLD` implies none, and an identity with no sizing mark cannot be
        sized — refused here rather than sent with a guessed price.
        """
        from stock_screener.portfolio_risk import Action

        if getattr(row, "action", None) is Action.HOLD:
            return False
        return row.identity.label in self.marks

    def _no_action(self, *, decision: StrategyDecision, reason: str, detail: str) -> dict[str, Any]:
        """Report a valid outcome in which nothing was submitted."""
        return {
            "planned_orders": 0,
            "orders_submitted": 0,
            "orders_filled": 0,
            "external_mutations": 0,
            "strategy_outcome": reason,
            "risk_transformation": (self.transformation.as_json() if self.transformation else None),
            "decision_fingerprint": decision.fingerprint(),
            "detail": detail,
        }

    def _record_transformation(self, *, decision: StrategyDecision, now: str, run_id: str) -> None:
        """Write the desired-to-permitted mapping beside the decision.

        Recorded rather than derived later: risk's answer depends on the book
        and the policy at this instant, and neither is reconstructable from the
        decision alone.
        """
        from stock_screener.paper_operations import atomic_write_verified

        from .binding import RUNTIME_DIRECTORY

        assert self.transformation is not None  # noqa: S101 - set by the caller
        atomic_write_verified(
            self.root / RUNTIME_DIRECTORY / "transformations" / f"{decision.decision_session}.json",
            {
                **self.transformation.as_json(),
                "decision_session": decision.decision_session,
                "recorded_at": now,
                "run_id": run_id,
            },
        )


def _event(name: str) -> Any:
    """Look up a run event type by name.

    Resolved by name so this module does not import the operations journal at
    module scope — the submission authority must stay importable without the
    control plane, which is what keeps the dependency pointing one way.

    Args:
        name: The event type's name.

    Returns:
        The member.
    """
    from stock_screener.paper_operations.run_journal import RunEventType

    return getattr(RunEventType, name)
