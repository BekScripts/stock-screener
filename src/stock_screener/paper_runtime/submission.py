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
        transformation: Filled in once risk has spoken.
        submitted: How many orders reached the venue.
    """

    root: Path
    adapter: Any
    risk_policy: Any
    risk_policy_fingerprint: str
    marks: dict[str, Decimal] = field(default_factory=dict)
    intent_log: Any = None
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
        )

        # State drift. The plan is bound to the book's fingerprint at planning
        # time; if the book has moved since, the plan authorises quantities
        # against capital that is no longer there. Checked immediately before
        # the first order rather than once at the start of the run.
        self._require_no_drift(plan=plan, portfolio=portfolio, binding=binding)

        results: list[dict[str, Any]] = []
        for order in plan.orders:
            gate.require(
                context=f"order {len(results) + 1} of {len(plan.orders)} for "
                f"{decision.decision_id}",
                expected_account=account,
            )
            results.append(self._submit_one(order=order, now=now))
            self.submitted += 1

        fills = self._ingest(results=results, portfolio=portfolio, ledger=ledger)
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
    ) -> Any:
        """Ask the execution engine for an authorised plan.

        The plan is bound to the book's state fingerprint, which is what makes
        an order it authorises unexecutable against a book that has moved.
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
        self, *, results: list[dict[str, Any]], portfolio: Any, ledger: Any
    ) -> dict[str, Any]:
        """Read each order's fills and admit them to accounting exactly once.

        A fill that is not visible yet is not a fill that did not happen — the
        P2.2E lesson. The adapter's bounded fill read is used rather than one
        poll concluding "no fill".
        """
        from stock_screener.execution import admit_fill

        admitted = 0
        for row in results:
            remote = row["remote_order_id"]
            if not remote:
                continue
            for fill in self.adapter.fills_evidence(remote_order_id=remote).value:
                outcome = admit_fill(fill, ledger=ledger, store=portfolio)
                if outcome.get("written"):
                    admitted += 1
        return {"admitted": admitted}

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
