"""The one production route from a scheduled slot to an audited paper order.

Before this module the CLI built a `RuntimePlanner` with no submission
authority, so `paper-ops run-due` reached a decision slot and then raised
`RUNTIME_BINDING_INVALID` on every execution slot: the authority was fully
implemented, tested, and constructed nowhere in `src/`. It also built a
transport with no request audit, so nothing durable recorded where a mutation
went — which is why `live_mutations` derived as `EVIDENCE_MISSING` rather than
as a clean zero.

This is the composition root that closes both. It builds, in one place and in
one order:

    persisted runtime binding
    → operations run id, derived from the slot and that binding
    → durable request audit, rooted at this runtime's own operations root
    → audited HTTPS transport, host-locked to the paper origin
    → certified adapter
    → environment, coordinator, planner
    → RuntimeSubmissionAuthority

Three properties are load-bearing.

**Constructing the authority does not grant permission.** Composition is not
authorisation. Every gate that stood before stands now — armed, proven paper,
right account, health, reconciliation, control state, a frozen decision, a
risk-approved plan — and a run against today's real control state still submits
nothing, because that state is not armed and is killed.

**The audit is rooted, not incidental.** It is written under the operations root
the runtime binding names, never the working directory, and the run id is
derived before the transport exists so every request can carry it.

**There is one route.** Nothing here hands a transport to anything but the
certified adapter, and the only seam is a pre-built HTTP client used to put a
scripted network *under* the real transport — so a test exercises the real host
lock, the real headers and the real audit rather than stand-ins for them. There
is no flag that selects a transport in ordinary CLI use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .errors import Code, RuntimeBindingError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "AUDIT_DIRECTORY",
    "PRODUCTION_COMPOSITION_VERSION",
    "OperationsComposition",
    "compose_operations",
    "composition_readiness",
]

#: The composition contract. Bound into the readiness report so a reader can
#: tell which wiring produced a run.
PRODUCTION_COMPOSITION_VERSION = "PAPER_OPERATIONS_PRODUCTION_COMPOSITION_V1"

#: Where request-audit records live, under the operations root.
AUDIT_DIRECTORY = "request_audit"

#: The fee this venue charges for a US equity execution, declared rather than
#: observed. Alpaca states no fee on a paper equity fill, and `require_fee`
#: refuses to substitute zero for an unstated one — so the declaration is made
#: here, explicitly, where an operator can see it. "The venue charges nothing"
#: and "nobody checked" must not look the same in the book.
DECLARED_EQUITY_FEE = Decimal(0)


@dataclass
class OperationsComposition:
    """Everything one scheduled operational run needs, wired once.

    Attributes:
        root: The operations root.
        binding: The persisted runtime binding. Authoritative for the account,
            the strategy, the ledger lineage and the journal lineage.
        slot: The slot being run.
        run_id: The run id derived from that slot and binding.
        audit: The durable request audit sink.
        transport: The audited, host-locked HTTPS transport.
        adapter: The certified paper adapter — the only object here that
            reaches a network.
        environment: The concrete operations environment.
        coordinator: The gating coordinator.
        submit: The submission authority. Constructed here, which is the gap
            this module exists to close.
        planner: The slot router, holding the authority.
        schedule: The operational schedule.
        calendar: The market calendar.
    """

    root: Path
    binding: Any
    slot: Any
    run_id: str
    audit: Any
    transport: Any
    adapter: Any
    environment: Any
    coordinator: Any
    submit: Any
    planner: Any
    schedule: Any
    calendar: Any
    _closers: list[Any] = field(default_factory=list, repr=False)

    def close(self) -> None:
        """Release the transport's connection pool."""
        for row in self._closers:
            row()

    def __enter__(self) -> OperationsComposition:
        """Enter the composition's lifetime."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Release what was opened."""
        self.close()

    def readiness(self) -> dict[str, Any]:
        """What this composition can and cannot do, without touching a network.

        The §41 self-check. It answers the question a future activation needs
        answered — is the production path actually wired — without arming
        anything or reaching a venue.

        Returns:
            The report. Carries no credential: the account appears as its
            binding fingerprint and never as an account number.
        """
        from stock_screener.execution.external import LIVE_HOSTS

        from .operator_inputs import operator_input_status

        return {
            "operator_inputs": operator_input_status(self.root).as_json(),
            "composition_version": PRODUCTION_COMPOSITION_VERSION,
            "submission_authority_constructible": self.submit is not None,
            "request_audit_constructible": self.audit is not None,
            "request_audit_root": str(self.audit.root) if self.audit is not None else "",
            "certified_adapter_constructible": self.adapter is not None,
            "transport_origin": str(getattr(self.transport, "origin", "")),
            "transport_audited": getattr(self.transport, "audit", None) is not None,
            "binding_version": str(getattr(self.binding, "version", "")),
            "binding_fingerprint": self.binding.fingerprint(),
            "predecessor_fingerprint": str(getattr(self.binding, "predecessor_fingerprint", "")),
            "risk_policy_fingerprint": str(getattr(self.binding, "risk_policy_fingerprint", "")),
            "instrument_mapping_fingerprint": str(
                getattr(self.binding, "instrument_mapping_fingerprint", "")
            ),
            "account_capability": dict(getattr(self.binding, "account_capability", {}) or {}),
            "live_capability": False,
            "live_hosts_refused": sorted(LIVE_HOSTS),
            "accounting_generation": self.binding.accounting_generation,
            "execution_journal_generation": self.binding.execution_journal_generation,
            "account_binding_fingerprint": self.binding.account_binding_fingerprint,
            "run_id": self.run_id,
            "purpose": str(getattr(self.slot, "purpose", "")),
            "declared_equity_fee": str(DECLARED_EQUITY_FEE),
        }


def compose_operations(
    *,
    settings: Any,
    root: Path,
    binding: Any,
    slot: Any,
    schedule: Any,
    calendar: Any,
    mode: Any,
    clock: Any,
    code_commit: str = "",
    bundle_for: Any = None,
    client: Any = None,
    audit: Any = None,
    marks: dict[str, Decimal] | None = None,
) -> OperationsComposition:
    """Wire one scheduled operational run, end to end.

    Args:
        settings: Typed application settings. Credentials are read through
            typed fields only and never rendered here.
        root: The operations root.
        binding: The persisted runtime binding.
        slot: The slot to run.
        schedule: The operational schedule.
        calendar: The market calendar.
        mode: The requested operations mode.
        clock: The operational clock.
        code_commit: Which build is running.
        bundle_for: Supplies the research cross-section for a decision session.
        client: A pre-built HTTP client. The composition root's only seam, used
            to put a scripted network under the real transport.
        audit: A pre-built request audit sink, for a test that needs to make its
            persistence fail. Defaults to one rooted at `root`.
        marks: Sizing marks by identity label. Defaults to the stored closes on
            or before this slot's session, read from price history.

    Returns:
        The composition.

    Raises:
        RuntimeBindingError: `RUNTIME_BINDING_INVALID` if the request audit sink
            cannot be constructed — mutation capability fails closed rather than
            proceeding unaudited, and there is no flag that disables auditing;
            `RUNTIME_SYMBOL_MAPPING_NOT_AUTHORED` or
            `RUNTIME_RISK_POLICY_NOT_AUTHORED` if an operator has not authored
            the inputs the authority needs.
    """
    from stock_screener.execution import fingerprint_of
    from stock_screener.execution.external import (
        AlpacaPaperAdapter,
        AlpacaPaperPreflightRun,
        SubmissionIntentLog,
    )
    from stock_screener.execution.external.request_audit import RequestAuditStore

    from .environment import AlphaTraderPaperOperationsEnvironment
    from .operator_inputs import read_risk_policy, read_symbol_mapping
    from .orchestration import RuntimePlanner
    from .submission import RuntimeSubmissionAuthority

    # The run id first: it is derived from the slot and the *persisted* binding,
    # so it exists before the transport does and every audited request can name
    # the run it belongs to. Deriving it from a proven account would need the
    # network, and the audit has to be ready before the first request.
    account = binding.account_binding_fingerprint
    run_id = slot.run_id(account_binding_fingerprint=account, mode=mode)

    sink = audit
    if sink is None:
        try:
            sink = RequestAuditStore(root / AUDIT_DIRECTORY)
        except Exception as exc:
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_INVALID,
                f"the request audit sink could not be opened under {root}: "
                f"{type(exc).__name__}. Operations do not proceed unaudited: a "
                "mutation nothing recorded is worse than one that never happened",
            ) from exc

    run = AlpacaPaperPreflightRun.from_settings(
        settings,
        store_root=root / "submissions",
        audit=sink,
        operations_run_id=run_id,
        account_binding_fingerprint=account,
        client=client,
    )
    # Both operator inputs are read before the authority is built, so a run
    # that cannot size an order says which artefact is missing rather than
    # failing later with a guessed number.
    mapping = read_symbol_mapping(root)
    risk_policy = read_risk_policy(root)
    # Fractionability is a property of the instrument, not of the venue, so a
    # venue-level default is necessarily wrong for one side or the other — the
    # certified canary makes exactly this point and declares the capability
    # from the venue's own asset read. Here it is declared from the authored
    # mapping, which carries `fractionable` per symbol: the operator who
    # declares which securities this runtime may route also declares how they
    # trade. The default stays whole-shares-only and is widened only by that
    # declaration.
    venue_binding = _declared_binding(run.binding, mapping, binding=binding)
    adapter = AlpacaPaperAdapter(transport=run.transport, binding=venue_binding, mapping=mapping)
    environment = AlphaTraderPaperOperationsEnvironment(root=root, binding=binding, adapter=adapter)
    environment.require_generations()

    submit = RuntimeSubmissionAuthority(
        root=root,
        adapter=adapter,
        risk_policy=risk_policy,
        risk_policy_fingerprint=fingerprint_of(risk_policy.body()),
        marks=_resolve_marks(marks, settings=settings, mapping=mapping, slot=slot),
        intent_log=SubmissionIntentLog(root / "submissions"),
        fee_schedule=DECLARED_EQUITY_FEE,
    )
    planner = RuntimePlanner(
        root=root,
        binding=binding,
        slot=slot,
        calendar=calendar,
        bundle_for=bundle_for,
        submit=submit,
    )
    coordinator = _coordinator(
        root=root,
        schedule=schedule,
        environment=environment,
        clock=clock,
        code_commit=code_commit,
    )
    return OperationsComposition(
        root=root,
        binding=binding,
        slot=slot,
        run_id=run_id,
        audit=sink,
        transport=run.transport,
        adapter=adapter,
        environment=environment,
        coordinator=coordinator,
        submit=submit,
        planner=planner,
        schedule=schedule,
        calendar=calendar,
        _closers=[run.close],
    )


def _resolve_marks(
    supplied: dict[str, Decimal] | None, *, settings: Any, mapping: Any, slot: Any
) -> dict[str, Decimal]:
    """Sizing marks for this slot, read only when the slot can trade.

    An observation or decision slot submits nothing, so it does not pay for a
    price read — the same reasoning that makes the research cross-section a
    callable rather than a value. A readiness check does not trade either.

    Args:
        supplied: Marks the caller already has, if any.
        settings: Typed settings, for the database.
        mapping: The authored symbol mapping.
        slot: The slot being composed for.

    Returns:
        The marks. Empty for a slot that cannot submit.
    """
    from stock_screener.paper_operations.strategy_schedule import RunPurpose

    from .operator_inputs import stored_marks

    if supplied is not None:
        return supplied
    if getattr(slot, "purpose", None) is not RunPurpose.STRATEGY_EXECUTION:
        return {}
    return stored_marks(settings=settings, mapping=mapping, as_of=str(getattr(slot, "session", "")))


def _declared_binding(venue: Any, mapping: Any, *, binding: Any = None) -> Any:
    """Widen a venue binding's capabilities to what is actually declared.

    Two sources, in order. A `PAPER_OPERATIONS_RUNTIME_BINDING_V2` records the
    capability an operator rebound to, and that recorded declaration is
    authoritative — it is the field the environment proof compares against. For
    a historical V1-only root there is no such record, so the capability comes
    from the authored mapping, and only when *every* routable symbol supports
    it: the capability check is account-wide while fractionability is per
    instrument, so a partial claim would permit a fractional order for an
    instrument that cannot take one.

    Args:
        venue: The venue binding built from configuration, at its conservative
            default of whole shares only.
        mapping: The authored venue symbol mapping.
        binding: The active runtime binding, V1 or V2.

    Returns:
        The venue binding, widened only by a declaration. Narrowing is never
        done here and nothing is widened by inference.
    """
    from dataclasses import replace

    declared = getattr(binding, "account_capability", None)
    if isinstance(declared, dict):
        if not declared.get("fractional_quantities", False):
            return venue
        return replace(venue, capabilities=replace(venue.capabilities, fractional_quantities=True))
    symbols = tuple(mapping.entries)
    if not symbols or not all(row.fractionable for row in symbols):
        return venue
    if venue.capabilities.fractional_quantities:
        return venue
    return replace(venue, capabilities=replace(venue.capabilities, fractional_quantities=True))


def _coordinator(
    *, root: Path, schedule: Any, environment: Any, clock: Any, code_commit: str
) -> Any:
    """Build the gating coordinator over this runtime's stores."""
    from stock_screener.paper_operations import ArmingStore, PaperOperationsCoordinator

    return PaperOperationsCoordinator(
        root=root,
        clock=clock,
        schedule=schedule,
        environment=environment,
        arming=ArmingStore(root),
        code_commit=code_commit,
    )


def composition_readiness(
    *,
    settings: Any,
    root: Path,
    binding: Any,
    slot: Any,
    schedule: Any,
    calendar: Any,
    mode: Any,
    clock: Any,
    code_commit: str = "",
) -> dict[str, Any]:
    """Prove the production composition is wired, without reaching a network.

    Args:
        settings: Typed application settings.
        root: The operations root.
        binding: The persisted runtime binding.
        slot: A slot to compose for.
        schedule: The operational schedule.
        calendar: The market calendar.
        mode: The mode to compose for.
        clock: The operational clock.
        code_commit: Which build.

    Returns:
        The readiness report, or one carrying the reason it could not be built.
    """
    try:
        composition = compose_operations(
            settings=settings,
            root=root,
            binding=binding,
            slot=slot,
            schedule=schedule,
            calendar=calendar,
            mode=mode,
            clock=clock,
            code_commit=code_commit,
            # No marks: this proves the wiring, and reading prices is a
            # property of a run rather than of the composition.
            marks={},
        )
    except Exception as exc:
        from .operator_inputs import operator_input_status

        return {
            "composition_version": PRODUCTION_COMPOSITION_VERSION,
            "ready": False,
            "detail": f"{type(exc).__name__}: {exc}",
            "operator_inputs": operator_input_status(root).as_json(),
        }
    try:
        report = composition.readiness()
    finally:
        composition.close()
    return {**report, "ready": True, "detail": ""}
