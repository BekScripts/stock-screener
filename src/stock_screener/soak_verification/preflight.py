"""The pre-arming self-check: proof that a protocol can be scored at all.

This is the module whose absence sealed an unscoreable soak. V2 declared
twenty-two gates, was frozen, tagged, armed against a real paper account, and
could not be evaluated by anything — and nothing objected, because a protocol
is just data and data does not fail to load.

So arming now has a prerequisite that is itself mechanical. The preflight does
not ask whether the protocol looks reasonable. It instantiates the real scorer
against the real store and proves four things by construction:

1. every declared gate has exactly one evaluator, and every evaluator the
   scorer knows about is declared — a check missing in either direction is a
   safety check silently switched off;
2. the store can actually supply every kind of evidence the gates read, by
   scoring an empty store and getting a verdict instead of an `AttributeError`;
3. every drill type the protocol requires can be recorded and evaluated;
4. all four verdicts are reachable under this protocol — including `A`, because
   a protocol whose success state is mechanically impossible is worse than no
   protocol: it collects twenty sessions of evidence toward a conclusion
   nothing can draw.

There is no `--force` and no `--skip-preflight`. An escape hatch here would
re-create exactly the failure this exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .drills_v2 import DRILL_EVIDENCE_V2_VERSION, DrillTypeV2
from .errors import Code, SoakContractError
from .gates import GATE_REGISTRY_VERSION, registry_fingerprint
from .scorer import SCORER_VERSION, SoakVerdictV2, score_soak

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "PREFLIGHT_VERSION",
    "SoakPreflightReport",
    "run_soak_preflight",
]

#: The preflight artefact contract.
PREFLIGHT_VERSION = "PAPER_SHADOW_SOAK_PREFLIGHT_V1"


@dataclass(frozen=True)
class SoakPreflightReport:
    """What the self-check established.

    Attributes:
        protocol_fingerprint: Which protocol was checked.
        qualifying_commit: Which build.
        passed: Whether arming may proceed.
        checks: Each check and its result.
        failures: Why it refused, where it did.
    """

    protocol_fingerprint: str
    qualifying_commit: str
    passed: bool
    checks: tuple[tuple[str, bool, str], ...] = ()
    failures: tuple[str, ...] = ()
    declared_gates: tuple[str, ...] = ()
    supported_drill_types: tuple[str, ...] = ()
    reachable_verdicts: tuple[str, ...] = ()
    limitations: tuple[str, ...] = field(
        default_factory=lambda: (
            "A reachability proof uses synthetic evidence. It establishes that the "
            "protocol's verdicts are mechanically obtainable, not that the real soak "
            "will obtain them.",
            "It cannot establish that a gate's evaluator measures what its name "
            "claims. That is what the scorer's own tests are for.",
        )
    )

    def body(self) -> dict[str, Any]:
        """Return the artefact's semantic content."""
        return {
            "version": PREFLIGHT_VERSION,
            "scorer_version": SCORER_VERSION,
            "gate_registry_version": GATE_REGISTRY_VERSION,
            "gate_registry_fingerprint": registry_fingerprint(),
            "drill_evidence_version": DRILL_EVIDENCE_V2_VERSION,
            "protocol_fingerprint": self.protocol_fingerprint,
            "qualifying_commit": self.qualifying_commit,
            "declared_gates": list(self.declared_gates),
            "supported_drill_types": list(self.supported_drill_types),
            "reachable_verdicts": list(self.reachable_verdicts),
            "checks": [
                {"check": name, "passed": ok, "detail": detail} for name, ok, detail in self.checks
            ],
            "passed": self.passed,
            "failures": list(self.failures),
            "escape_hatch": "NONE — there is no force, skip or ignore option",
            "limitations": list(self.limitations),
        }

    def fingerprint(self) -> str:
        """Digest the artefact."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the artefact with its fingerprint."""
        return {**self.body(), "preflight_fingerprint": self.fingerprint()}

    def summary_lines(self) -> tuple[str, ...]:
        """Render the report for a terminal."""
        lines = [
            f"preflight          : {PREFLIGHT_VERSION}",
            f"scorer             : {SCORER_VERSION}",
            f"gate registry      : {registry_fingerprint()[:16]}…",
            f"declared gates     : {len(self.declared_gates)}",
            f"reachable verdicts : {', '.join(self.reachable_verdicts) or 'none'}",
        ]
        lines += [
            f"    [{'pass' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
            for name, ok, detail in self.checks
        ]
        lines.append(f"result             : {'PASSED' if self.passed else 'REFUSED'}")
        return tuple(lines)

    def require(self) -> None:
        """Refuse arming unless the self-check passed.

        Raises:
            SoakContractError: `SOAK_PREFLIGHT_FAILED`.
        """
        if not self.passed:
            raise SoakContractError(
                Code.SOAK_PREFLIGHT_FAILED,
                "the protocol cannot be armed: "
                + "; ".join(self.failures)
                + ". There is no force option — arming an unscoreable protocol is the "
                "failure this check exists to prevent",
            )


def run_soak_preflight(
    *,
    protocol_fingerprint: str,
    qualifying_commit: str,
    declared_gates: dict[str, int],
    required_sessions: int,
    required_drill_types: Sequence[str],
    operations_engine_version: str,
    operations_policy_fingerprint: str,
    protocol: Any = None,
    activation: Any = None,
) -> SoakPreflightReport:
    """Prove a protocol can be scored, before it is frozen and armed.

    Args:
        protocol_fingerprint: The protocol being checked.
        qualifying_commit: The build it judges.
        declared_gates: Its gates, id to threshold.
        required_sessions: How many sessions it requires.
        required_drill_types: Which drills it requires.
        operations_engine_version: The control plane it binds.
        operations_policy_fingerprint: The operational rules it binds.
        protocol: The protocol object, needed to prove a verdict reachable.
            Reachability is established by writing the operational records a
            soak would leave behind and running the real derivations over them,
            which needs the protocol a session would be bound to. Without it
            reachability cannot be established and is reported as such rather
            than asserted from injected metrics.
        activation: Its activation, where one exists.

    Returns:
        The report. `require()` turns a refusal into an exception; the report
        itself is returned either way so a caller can print what failed.
    """
    checks: list[tuple[str, bool, str]] = []
    failures: list[str] = []
    reachable: list[str] = []

    # 1. Gate/evaluator bijection, both directions.
    try:
        from .gates import require_bijection

        require_bijection(declared_gates)
        checks.append(("gate_evaluator_bijection", True, f"{len(declared_gates)} gates"))
    except SoakContractError as exc:
        checks.append(("gate_evaluator_bijection", False, str(exc)))
        failures.append(str(exc))

    # 2. Drill support, before anything depends on it.
    registry_types = {str(row) for row in DrillTypeV2}
    unsupported = sorted(set(map(str, required_drill_types)) - registry_types)
    if unsupported:
        detail = f"unsupported drill types {unsupported}"
        checks.append(("drill_types_supported", False, detail))
        failures.append(detail)
    else:
        checks.append(("drill_types_supported", True, f"{len(required_drill_types)} types"))

    # 3 and 4. Scoreability and verdict reachability, through the real
    # derivation path. The fixtures write the operational records a soak would
    # leave behind — journal entries, request audit, reconciliation, decision
    # and execution-journal records — and the real derivations read them.
    #
    # This replaces fixtures that set `live_requests=1` on a synthetic
    # observation and watched the scorer say FAIL. That proved the comparison
    # operator worked. It proved nothing about whether anything could ever
    # *observe* a live request, which is the actual question, and fourteen
    # gates were unobservable while those fixtures passed.
    if not failures:
        if protocol is None:
            detail = (
                "verdict reachability needs the protocol object: it is established by "
                "writing real operational records and deriving from them, and there is "
                "no longer a path that asserts a verdict from injected metrics"
            )
            checks.append(("verdict_reachability_uses_real_derivation", False, detail))
            failures.append(detail)
        else:
            for name, builder_kwargs in (
                (SoakVerdictV2.IN_PROGRESS, {"sessions": 0}),
                (SoakVerdictV2.PASSED, {}),
                (SoakVerdictV2.FAILED, {"breach": "TWO_REMOTE_ORDERS"}),
                (SoakVerdictV2.HALTED, {"halt": True}),
                (SoakVerdictV2.EVIDENCE_INCOMPLETE, {"remove_source": "RECONCILIATION_RECORDS"}),
            ):
                try:
                    score = _reachability_score(
                        protocol=protocol,
                        activation=activation,
                        declared_gates=declared_gates,
                        required_sessions=required_sessions,
                        **builder_kwargs,
                    )
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc}"
                    checks.append((f"verdict_reachable_{name}", False, detail))
                    failures.append(detail)
                    continue
                ok = score.verdict == name
                checks.append((f"verdict_reachable_{name}", ok, f"got {score.verdict}"))
                if ok:
                    reachable.append(name)
                else:
                    failures.append(
                        f"{name} is not mechanically reachable under this protocol; "
                        f"records built to produce it scored {score.verdict}"
                    )

    return SoakPreflightReport(
        protocol_fingerprint=protocol_fingerprint,
        qualifying_commit=qualifying_commit,
        passed=not failures,
        checks=tuple(checks),
        failures=tuple(failures),
        declared_gates=tuple(sorted(declared_gates)),
        supported_drill_types=tuple(sorted(registry_types)),
        reachable_verdicts=tuple(reachable),
    )


def _reachability_score(
    *,
    protocol: Any,
    activation: Any,
    declared_gates: dict[str, int],
    required_sessions: int,
    sessions: int | None = None,
    breach: str | None = None,
    omit_drills: bool = False,
    halt: bool = False,
    remove_source: str | None = None,
) -> Any:
    """Score a soak built from real records, in a scratch root.

    The root is temporary and discarded: a reachability proof must not leave
    anything behind that a real soak could later read as evidence.
    """
    import tempfile
    from pathlib import Path as _Path

    from .synthetic import build_soak_fixture

    count = required_sessions if sessions is None else sessions
    with tempfile.TemporaryDirectory(prefix="soak-preflight-") as scratch:
        fixture = build_soak_fixture(
            _Path(scratch) / ".operations",
            protocol=protocol,
            activation=activation if activation is not None else _activation_for(protocol),
            sessions=count,
            breach=breach,
            omit_drills=omit_drills,
            remove_source=remove_source,
        )
        if halt:
            fixture.store.halt(
                reason="reachability proof",
                classification="PREFLIGHT_REACHABILITY",
                at="1970-01-01T00:00:00+00:00",
            )
        return score_soak(
            fixture.evidence,
            declared_gates=declared_gates,
            required_sessions=required_sessions,
        )


def _activation_for(protocol: Any) -> Any:
    """A matching activation, for a protocol being checked before activation."""
    from .contracts import PaperShadowSoakActivation

    return PaperShadowSoakActivation(
        activation_version=protocol.protocol_version,
        protocol_version=protocol.protocol_version,
        protocol_fingerprint=protocol.fingerprint(),
        qualifying_commit=protocol.qualifying_commit,
        activated_at="1970-01-01T00:00:00+00:00",
        required_sessions=protocol.required_sessions,
        runtime_binding_fingerprint=protocol.runtime_binding_fingerprint,
        paper_account_fingerprint=protocol.paper_account_fingerprint,
        calendar_fingerprint=protocol.calendar_fingerprint,
        schedule_fingerprint=protocol.schedule_fingerprint,
        strategy_fingerprint=protocol.strategy_fingerprint,
        risk_policy_fingerprint=protocol.risk_policy_fingerprint,
        operations_policy_fingerprint=protocol.operations_policy_fingerprint,
        score_availability_profile_fingerprint=(protocol.score_availability_profile_fingerprint),
        execution_timing_profile_fingerprint=protocol.execution_timing_profile_fingerprint,
    )


def preflight_protocol(
    protocol: Any,
    *,
    activation: Any = None,
    store: Any = None,
) -> SoakPreflightReport:
    """Run the self-check against the exact protocol that will be frozen.

    The protocol itself is evaluated, not a reduced descriptor of it. A
    descriptor would omit some field, and the omitted field is the one that
    turns out to matter — V2's whole defect was a gate set that looked fine
    summarised and could not be scored in full.

    Args:
        protocol: The generic `PaperShadowSoakProtocol` to be frozen.
        activation: The activation to be written, when one is ready. Checked
            against the protocol so a version mismatch is caught before
            anything is written rather than after.
        store: The exact store, with its real namespace, to prove an empty one
            is scoreable through the interface the scorer will actually use.

    Returns:
        The report.
    """
    from .contracts import canonical_gate_ids
    from .gates import registry_fingerprint as _registry

    report = run_soak_preflight(
        protocol=protocol,
        activation=activation,
        protocol_fingerprint=protocol.fingerprint(),
        qualifying_commit=protocol.qualifying_commit,
        declared_gates=dict(protocol.gate_thresholds),
        required_sessions=protocol.required_sessions,
        required_drill_types=list(protocol.required_drill_types),
        operations_engine_version=_operations_engine_version(),
        operations_policy_fingerprint=protocol.operations_policy_fingerprint,
    )
    checks = list(report.checks)
    failures = list(report.failures)

    # The protocol's own registry binding must be the registry in force. A
    # protocol frozen against an older registry would be scored by evaluators
    # it never declared.
    if protocol.gate_registry_fingerprint != _registry():
        detail = (
            f"the protocol binds registry {protocol.gate_registry_fingerprint[:16]}… "
            f"and the registry in force is {_registry()[:16]}…"
        )
        checks.append(("gate_registry_fingerprint_matches", False, detail))
        failures.append(detail)
    else:
        checks.append(("gate_registry_fingerprint_matches", True, ""))

    # Every gate must have a derivation, every derivation a known source, and
    # every scope must be one the model measures. This replaces the old check
    # that the observation schema carried each gate's named fields: gates no
    # longer read fields off observations, they read derived metrics, and the
    # thing that can now be silently absent is the derivation rather than the
    # field. Same defect, one layer up, so the same check moves with it.
    from .derivations import GateScope, derivation_registry
    from .errors import SoakContractError as _ContractError
    from .evidence_contract import evidence_contract as _contract
    from .sources import SOURCE_SCHEMAS, known_source_schema

    try:
        contract = _contract()
    except _ContractError as exc:
        detail = str(exc)
        checks.append(("every_gate_has_a_derivation", False, detail))
        failures.append(detail)
    else:
        checks.append(
            (
                "every_gate_has_a_derivation",
                True,
                f"{len(contract.bindings)} gate(s) bound to "
                f"{len({row.derivation_id for row in contract.bindings.values()})} derivation(s)",
            )
        )
        checks.append(
            (
                "evidence_contract_fingerprint",
                True,
                contract.fingerprint()[:16] + "…",
            )
        )
        checks.append(
            (
                "observability_contract_bound",
                bool(contract.observability_fingerprint),
                contract.observability_fingerprint[:16] + "…",
            )
        )

        unknown = sorted(
            {
                schema_id
                for row in derivation_registry().values()
                for schema_id in row.required_schemas
                if not known_source_schema(schema_id)
            }
        )
        if unknown:
            detail = f"derivations read undescribed sources: {unknown}"
            checks.append(("all_source_schemas_known", False, detail))
            failures.append(detail)
        else:
            checks.append(("all_source_schemas_known", True, f"{len(SOURCE_SCHEMAS)} schema(s)"))

        scopes = {row.scope for row in contract.bindings.values()}
        stray = sorted(str(row) for row in scopes - set(GateScope))
        if stray:
            detail = f"gates bound to scopes the model does not measure: {stray}"
            checks.append(("all_gate_scopes_valid", False, detail))
            failures.append(detail)
        else:
            checks.append(
                (
                    "all_gate_scopes_valid",
                    True,
                    ", ".join(f"{scope!s}={len(contract.gates_in(scope))}" for scope in GateScope),
                )
            )

    if set(protocol.declared_gate_ids) == set(canonical_gate_ids()):
        checks.append(
            ("declared_gates_equal_registry", True, f"{len(protocol.declared_gate_ids)} gates")
        )
    else:
        detail = "the declared gate set is not the canonical registry set"
        checks.append(("declared_gates_equal_registry", False, detail))
        failures.append(detail)

    # The activation schema must be able to carry what this protocol needs,
    # and must agree with it about which protocol it activates.
    if activation is not None:
        try:
            activation.require_matches(protocol)
            checks.append(("activation_contract_matches", True, activation.activation_version))
        except SoakContractError as exc:
            checks.append(("activation_contract_matches", False, str(exc)))
            failures.append(str(exc))

    # The real store, with its real namespace, must be scoreable while empty.
    if store is not None:
        try:
            from .aggregate import evidence_for_store

            empty = score_soak(
                evidence_for_store(store),
                declared_gates=dict(protocol.gate_thresholds),
                required_sessions=protocol.required_sessions,
            )
            ok = empty.verdict == SoakVerdictV2.IN_PROGRESS
            checks.append(("real_store_scores_in_progress", ok, f"verdict {empty.verdict}"))
            if not ok:
                failures.append(
                    f"the real store scored {empty.verdict} while empty, expected in-progress"
                )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            checks.append(("real_store_scores_in_progress", False, detail))
            failures.append(f"the real store is not scoreable — {detail}")

    return SoakPreflightReport(
        protocol_fingerprint=protocol.fingerprint(),
        qualifying_commit=protocol.qualifying_commit,
        passed=not failures,
        checks=tuple(checks),
        failures=tuple(failures),
        declared_gates=tuple(sorted(protocol.declared_gate_ids)),
        supported_drill_types=report.supported_drill_types,
        reachable_verdicts=report.reachable_verdicts,
    )


def _operations_engine_version() -> str:
    """The control plane version in force."""
    from stock_screener.paper_operations import PAPER_OPERATIONS_ENGINE_VERSION

    return PAPER_OPERATIONS_ENGINE_VERSION


def _observation_schema_fields(schema_version: str) -> frozenset[str] | None:
    """The fields a declared observation schema provides.

    Args:
        schema_version: The schema the protocol binds.

    Returns:
        Its field names, or `None` when the schema is unknown — which is itself
        a refusal, because a schema nothing can inspect cannot be checked
        against the gates.
    """
    if schema_version == "PAPER_SHADOW_SESSION_OBSERVATION_V2":
        from stock_screener.paper_runtime.soak_v2 import PaperShadowSessionObservationV2

        return frozenset(PaperShadowSessionObservationV2.__dataclass_fields__)
    return None
