"""The concrete `OperationsEnvironment` for the certified Alpaca paper path.

P2.3 defined `OperationsEnvironment` as an injected protocol so the coordinator
could gate a system it did not know the shape of. This is the implementation it
was waiting for, and it orchestrates — it decides nothing.

Every authority it touches already exists and is already certified:

    S1.0 strategy          -> what exposure is wanted
    portfolio construction -> how those wants combine
    risk engine V1.2       -> what exposure is permitted
    execution engine V1.2  -> what orders achieve it
    Alpaca paper adapter   -> the only thing that talks to a venue
    accounting V2          -> what the fills did to the book
    reconciliation         -> whether the three records agree

This module calls them in that order and reimplements none of them. It holds no
ranking rule, no sizing rule, no order constructor and no fill logic. The one
thing it adds is the part that is genuinely operational: knowing which of those
authorities to involve for *this* slot's purpose, and refusing when the runtime
it was handed does not match the records on disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import Code, RuntimeBindingError

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.paper_operations import OperationalInput

    from .binding import PaperOperationsRuntimeBinding

__all__ = [
    "RUNTIME_ENVIRONMENT_VERSION",
    "AlphaTraderPaperOperationsEnvironment",
]

#: The environment contract.
RUNTIME_ENVIRONMENT_VERSION = "ALPHATRADER_PAPER_OPERATIONS_ENVIRONMENT_V1"


@dataclass
class AlphaTraderPaperOperationsEnvironment:
    """Everything a run needs to be gated, read from one explicit binding.

    Attributes:
        root: The operations root this runtime owns.
        binding: The runtime binding. Authoritative for which account, which
            strategy, which ledger lineage and which journal lineage.
        adapter: The certified `AlpacaPaperAdapter`. The only object here that
            reaches a network, and it cannot be constructed against a live host.
        freshness_policies: The freshness bound for each required input, by
            name. Supplied rather than invented: a default freshness window is
            the one nobody meant.
        score_as_of: Which stored cross-section the strategy should read. The
            caller resolves it, because "the latest admissible one" is an
            operational judgement and not a property of the environment.
    """

    root: Path
    binding: PaperOperationsRuntimeBinding
    adapter: Any = None
    freshness_policies: dict[str, int] = field(default_factory=dict)
    score_as_of: str = ""
    _account: Any = field(default=None, init=False, repr=False)

    @property
    def version(self) -> str:
        """This environment's contract version."""
        return RUNTIME_ENVIRONMENT_VERSION

    # -- the OperationsEnvironment protocol -------------------------------

    def verify_local_integrity(self) -> dict[str, Any]:
        """Verify this runtime's own execution journal and accounting ledger.

        Both are opened at the *bound* generation, not at whatever the directory
        happens to hold. That is the check that stops a run from verifying the
        canary's book and reporting it healthy — those records are valid and
        belong to something else.

        Returns:
            What the coordinator's health gate expects: whether each verified,
            and a sanitised detail when one did not.
        """
        result: dict[str, Any] = {
            "execution_journal_verified": False,
            "accounting_ledger_verified": False,
            "execution_detail": "",
            "accounting_detail": "",
        }
        try:
            execution = self._execution_store()
            journal = execution.load_journal().verify()
            result["execution_journal_verified"] = bool(journal.get("verified", False))
            if not journal.get("verified", False):
                result["execution_detail"] = str(journal.get("failure", "chain not verified"))
        except Exception as exc:
            result["execution_detail"] = f"{type(exc).__name__}: execution journal did not verify"
        try:
            portfolio = self._portfolio_store()
            store = portfolio.verify()
            result["accounting_ledger_verified"] = bool(store.get("verified", False))
            if not store.get("verified", False):
                result["accounting_detail"] = str(
                    store.get("truncation_detected", "ledger not verified")
                )
        except Exception as exc:
            result["accounting_detail"] = f"{type(exc).__name__}: accounting ledger did not verify"
        return result

    def prove_paper_environment(self) -> dict[str, Any]:
        """Prove the venue is the bound paper account.

        Returns:
            Whether it is proven, the account binding fingerprint the venue
            answered for, and a sanitised detail.

        Raises:
            Exception: Whatever the transport raises when the venue cannot be
                reached. Deliberately not caught here: the coordinator's health
                gate distinguishes "unreachable" from "reachable and wrong", and
                collapsing them would let an outage read as a mismatch.
        """
        if self.adapter is None:
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_INVALID,
                "this environment has no broker adapter, so the paper venue cannot be "
                "proven. An unproven venue is never assumed to be the right one",
            )
        evidence = self.adapter.account_evidence()
        account = evidence.value
        self._account = account
        fingerprint = self.adapter.binding.binding_fingerprint()
        return {
            "proven": fingerprint == self.binding.account_binding_fingerprint,
            "account_binding_fingerprint": fingerprint,
            "detail": (
                ""
                if fingerprint == self.binding.account_binding_fingerprint
                else "the venue answered for an account this runtime is not bound to"
            ),
            "status": getattr(account, "status", ""),
            "trading_blocked": bool(getattr(account, "trading_blocked", False)),
        }

    def reconcile(self) -> dict[str, Any]:
        """Reconcile the three records for this runtime's epoch.

        Only activity from the bootstrap epoch onward is this runtime's. Orders
        and fills that predate it — the P2.2E canary's among them — are
        historical evidence at the venue and are not runtime activity. Counting
        them would report a break on the runtime's first day for something that
        happened before the book existed.

        Returns:
            The three-way verdict, whether the run may execute, and the counts
            the coordinator's gates read.

        Raises:
            Exception: Whatever the transport raises when the venue cannot be
                reached. Not caught, for the reason `prove_paper_environment`
                gives.
        """
        if self.adapter is None:
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_INVALID,
                "this environment has no broker adapter, so nothing can be reconciled",
            )
        positions = self.adapter.positions_evidence().value
        working = self.adapter.open_orders_evidence().value
        epoch = self.binding.bootstrap_epoch
        ours = [order for order in working if self._is_runtime_order(order)]
        foreign = [order for order in working if not self._is_runtime_order(order)]
        book = self._accounting_positions()
        unexplained = [row for row in positions if str(getattr(row, "symbol", "")) not in book]
        breaks = len(foreign) + len(unexplained)
        return {
            "verdict": "RECONCILED" if breaks == 0 else "BREAKS_BLOCKING",
            "may_execute": breaks == 0,
            "remote_state_unknown": False,
            "unexpected_remote_orders": len(foreign),
            "unexplained_remote_positions": len(unexplained),
            "runtime_open_orders": len(ours),
            "bootstrap_epoch": epoch,
            "epoch_note": (
                "activity before the bootstrap epoch is historical evidence at the "
                "venue and is not runtime activity"
            ),
        }

    def persist_reconciliation(self, *, result: dict[str, Any], run_id: str, now: str) -> None:
        """Keep the reconciliation report itself, beside the journal entry.

        `PortfolioStore.write_reconciliation` has existed since P1 with no
        caller, which is why no soak gate could read a reconciliation result.
        The journal entry records the verdict and links it to the run; this
        keeps the report, including the breaks — a store that only kept clean
        reconciliations would show an unblemished record of exactly what it
        failed to record.

        Args:
            result: What `reconcile` returned.
            run_id: The run it belongs to.
            now: The operational instant.
        """
        from stock_screener.paper_operations import atomic_write_verified

        from .binding import RUNTIME_DIRECTORY

        atomic_write_verified(
            self.root / RUNTIME_DIRECTORY / "reconciliations" / f"{run_id}.json",
            {
                "version": "OPERATIONAL_RECONCILIATION_RECORD_V1",
                "run_id": run_id,
                "evaluated_at": now,
                "runtime_binding_fingerprint": self.binding.fingerprint(),
                "account_binding_fingerprint": self.binding.account_binding_fingerprint,
                "accounting_generation": self.binding.accounting_generation,
                "execution_journal_generation": self.binding.execution_journal_generation,
                "accounting_head": self.accounting_state_fingerprint(),
                "bootstrap_epoch": self.binding.bootstrap_epoch,
                # Nested, not splatted. Flattening the provider-derived result
                # into this record would let a future reconcile key — `run_id`,
                # `version` — silently overwrite the record's own provenance,
                # and the overwritten field is exactly the one a reader would
                # trust to say which run this belonged to.
                "result": {str(key): value for key, value in result.items()},
            },
        )

    def required_inputs(self) -> tuple[OperationalInput, ...]:
        """The inputs this run depends on, with their real freshness state.

        No input is fabricated to satisfy the coordinator. The score
        cross-section's own `as_of` and its digest are reported as they are, so
        a stale or missing cross-section blocks rather than passing a check it
        did not meet.

        Returns:
            The operational inputs.
        """
        from stock_screener.paper_operations import FreshnessPolicy, OperationalInput

        if not self.score_as_of:
            return ()
        return (
            OperationalInput(
                name="compounder_score_cross_section",
                as_of=self.score_as_of,
                source_fingerprint=self.binding.strategy_fingerprint,
                policy=FreshnessPolicy(
                    name="compounder_score_cross_section",
                    max_age_seconds=self.freshness_policies.get(
                        "compounder_score_cross_section", 0
                    ),
                ),
            ),
        )

    def accounting_state_fingerprint(self) -> str:
        """This runtime's accounting state digest, at the bound generation.

        Returns:
            The digest, or `""` when the ledger could not be read — which the
            coordinator treats as a missing input rather than as an empty book.
        """
        try:
            return str(self._portfolio_store().load_ledger().verify().get("head_hash", ""))
        except Exception:
            return ""

    # -- helpers ----------------------------------------------------------

    def require_generations(self) -> None:
        """Refuse unless the records on disk are the bound lineages.

        Raises:
            RuntimeBindingError: `RUNTIME_GENERATION_MISMATCH`.
        """
        # The stores refuse a mismatched lineage themselves, and earlier than
        # this comparison would — opening one at the wrong generation raises
        # before `verify` returns. That refusal is correct and is translated
        # here rather than leaked, so an operator sees one code for "the records
        # on disk are not the ones this runtime is bound to" however it was
        # detected.
        try:
            accounting = str(self._portfolio_store().verify().get("generation", ""))
            execution = self._execution_store().generation
        except Exception as exc:
            raise RuntimeBindingError(
                Code.RUNTIME_GENERATION_MISMATCH,
                f"{type(exc).__name__}: the records on disk are not this runtime's "
                f"bound lineage ({self.binding.accounting_generation!r} / "
                f"{self.binding.execution_journal_generation!r}). The run is refused "
                "rather than proceeding against whichever book it found",
            ) from exc
        self.binding.require_matches(
            accounting_generation=accounting,
            execution_journal_generation=execution,
        )

    def _portfolio_store(self) -> Any:
        """Open this runtime's accounting store at its bound generation."""
        from stock_screener.portfolio_accounting import PortfolioStore

        from .binding import RUNTIME_DIRECTORY

        return PortfolioStore(
            self.root / RUNTIME_DIRECTORY / "portfolio",
            accounting_version=self.binding.accounting_engine_version,
            generation=self.binding.accounting_generation,
        )

    def _execution_store(self) -> Any:
        """Open this runtime's execution store at its bound generation."""
        from stock_screener.execution import ExecutionStore

        from .binding import RUNTIME_DIRECTORY

        return ExecutionStore(
            self.root / RUNTIME_DIRECTORY / "execution",
            generation=self.binding.execution_journal_generation,
        )

    def _accounting_positions(self) -> dict[str, Any]:
        """The symbols this runtime's book accounts for.

        Returns:
            Symbol to position. Empty when the ledger cannot be read, which
            makes every venue position unexplained — the safe direction, since
            the alternative is reporting a clean reconciliation against a book
            nobody could open.
        """
        try:
            ledger = self._portfolio_store().load_ledger()
        except Exception:
            return {}
        state = getattr(ledger, "state", None)
        positions = getattr(state, "positions", None) if state is not None else None
        if not positions:
            return {}
        return {str(getattr(row, "symbol", key)): row for key, row in dict(positions).items()}

    def _is_runtime_order(self, order: Any) -> bool:
        """Whether a working order was minted by this runtime.

        A client order id this adapter derived is the only evidence of
        ownership. An order without one belongs to somebody else — which on a
        shared paper account is a real possibility and is exactly what the
        unexpected-order gate exists to catch.

        Args:
            order: An external order.

        Returns:
            True only when the adapter recognises the client order id.
        """
        client_id = str(getattr(order, "client_order_id", "") or "")
        return bool(client_id) and client_id.startswith(self.binding.runtime_id)

    def manifest(self) -> dict[str, Any]:
        """Describe what this environment is bound to. Carries no credential."""
        return {
            "environment_version": RUNTIME_ENVIRONMENT_VERSION,
            "runtime_id": self.binding.runtime_id,
            "binding_fingerprint": self.binding.fingerprint(),
            "account_binding_fingerprint": self.binding.account_binding_fingerprint,
            "strategy_id": self.binding.strategy_id,
            "strategy_version": self.binding.strategy_version,
            "strategy_fingerprint": self.binding.strategy_fingerprint,
            "accounting_engine_version": self.binding.accounting_engine_version,
            "accounting_generation": self.binding.accounting_generation,
            "execution_engine_version": self.binding.execution_engine_version,
            "execution_journal_generation": self.binding.execution_journal_generation,
            "bootstrap_epoch": self.binding.bootstrap_epoch,
            "score_as_of": self.score_as_of,
            "orchestrates_only": True,
            "reimplements": [],
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
        }
