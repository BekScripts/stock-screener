"""Generic soak protocol and activation contracts, with gates as data.

Three protocol versions have now failed for the same structural reason, and it
is worth naming precisely because the fix is not "be more careful".

V1 and V2 each baked their gate set into a hardcoded `promotion_gates`
property. That made the gate set a property of the *class* rather than of the
protocol instance, with three consequences: a protocol could declare a gate no
evaluator covered (V2 declared four), the declared set could not be compared
against the registry because it was not data, and expressing a new gate set
meant writing a new class — which is why V3 could not be instantiated at all.

So here `declared_gate_ids` is a field. It is validated against the canonical
registry at construction, it participates in the protocol fingerprint, and it
is sorted canonically so the order a caller supplies cannot change the
protocol's identity. A protocol that declares a gate nothing can evaluate now
fails to exist rather than failing to be scoreable.

The historical V1 and V2 classes are left exactly as they are. They describe
protocols that were frozen and halted, and rewriting them would rewrite
evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import Code, SoakContractError
from .gates import GateClass, gate_registry, registry_fingerprint
from .scorer import SCORER_VERSION

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "ACTIVATION_FAMILY",
    "PROTOCOL_FAMILY",
    "PaperShadowSoakActivation",
    "PaperShadowSoakProtocol",
    "canonical_gate_ids",
    "canonical_thresholds",
]

#: The family every generic protocol belongs to. The *version* varies per
#: instance; the family says which contract shape it is.
PROTOCOL_FAMILY = "PAPER_SHADOW_SOAK_PROTOCOL"

#: The activation family.
ACTIVATION_FAMILY = "PAPER_SHADOW_SOAK_ACTIVATION"


def canonical_gate_ids() -> tuple[str, ...]:
    """Every gate id the canonical registry supports, sorted.

    Returns:
        The ids. A protocol builds its declared set from here rather than
        maintaining a second list, because a second list is a thing that drifts
        — and the drift is invisible until something cannot be scored.
    """
    return tuple(sorted(gate_registry()))


def canonical_thresholds(
    *, required_sessions: int, required_drills: int, required_cycles: int = 1
) -> dict[str, int]:
    """Build the canonical threshold for every registry gate.

    Zero-tolerance gates get zero. `AT_MOST` promotion gates get zero — no
    unrecorded elapsed session, no open CRITICAL or BLOCKING incident, no
    unaccounted decision execution. `AT_LEAST` promotion gates get the counts a
    caller declares.

    Args:
        required_sessions: How many eligible sessions the soak needs.
        required_drills: How many distinct drill types must pass.
        required_cycles: How many strategy decision cycles must complete.

    Returns:
        Gate id to threshold, covering exactly the canonical registry.
    """
    from .gates import GateBound

    thresholds: dict[str, int] = {}
    for gate_id, definition in gate_registry().items():
        if (
            definition.gate_class is GateClass.ZERO_TOLERANCE
            or definition.bound is GateBound.AT_MOST
        ):
            thresholds[gate_id] = 0
        elif gate_id == "qualifying_sessions_observed":
            thresholds[gate_id] = required_sessions
        elif gate_id == "required_drills_passed":
            thresholds[gate_id] = required_drills
        else:
            thresholds[gate_id] = required_cycles
    return thresholds


@dataclass(frozen=True)
class PaperShadowSoakProtocol:
    """One soak protocol instance, with every rule held as data.

    Attributes:
        protocol_version: This instance's version, explicit rather than a class
            default — a generic contract that defaulted to a version string
            would stamp the wrong one on the next soak.
        protocol_id: A stable name for the instance.
        qualifying_commit: The exact build judged. Observations from another
            build do not count.
        declared_gate_ids: The gates, as data, canonically sorted.
        gate_thresholds: Each gate's limit, as data.
        gate_registry_fingerprint: Which registry those ids came from, so a
            changed evaluator is visible in the protocol's own digest.
        scorer_version: Which evaluator may score it.
        required_sessions: How many eligible sessions are needed.
        required_drill_types: Which drills must pass. Types only — a future
            drill record's fingerprint cannot be bound by a protocol frozen
            before the drill was run.
        observation_schema_version: Which observation shape the store holds.
            Independent of `protocol_version`: a new protocol does not imply a
            new observation schema, and conflating them is what produced a
            version-specific class per soak.
        drill_schema_version: Which drill evidence shape.
        runtime_binding_fingerprint: The deployed runtime.
        paper_account_fingerprint: Which paper account.
        strategy_id / strategy_version / strategy_fingerprint: The exact policy.
        strategy_authorization_fingerprint: The deployment grant.
        risk_policy_fingerprint: The approved risk policy.
        operations_policy_fingerprint: The operational rules. Deliberately not
            an ephemeral operator authorization: a kill drill legitimately
            rotates that, and binding it would make a required drill invalidate
            the protocol it was proving.
        calendar_version / calendar_fingerprint: The session calendar.
        schedule_version / schedule_fingerprint: The slots.
        score_availability_profile_fingerprint: The declared score timing.
        execution_timing_profile_fingerprint: The declared execution timing.
    """

    protocol_version: str
    protocol_id: str
    qualifying_commit: str
    required_sessions: int
    runtime_binding_fingerprint: str
    paper_account_fingerprint: str
    strategy_id: str
    strategy_version: str
    strategy_fingerprint: str
    strategy_authorization_fingerprint: str
    risk_policy_fingerprint: str
    operations_policy_fingerprint: str
    calendar_version: str
    calendar_fingerprint: str
    schedule_version: str
    schedule_fingerprint: str
    score_availability_profile_fingerprint: str
    execution_timing_profile_fingerprint: str
    declared_gate_ids: tuple[str, ...] = ()
    gate_thresholds: Mapping[str, int] = field(default_factory=dict)
    required_drill_types: tuple[str, ...] = ()
    observation_schema_version: str = "PAPER_SHADOW_SESSION_OBSERVATION_V2"
    drill_schema_version: str = "PAPER_OPERATIONS_DRILL_EVIDENCE_V2"
    scorer_version: str = SCORER_VERSION
    gate_registry_fingerprint: str = field(default_factory=registry_fingerprint)
    protocol_family: str = PROTOCOL_FAMILY

    def __post_init__(self) -> None:
        """Validate and canonicalise, refusing a protocol that cannot be scored.

        Raises:
            SoakContractError: `SOAK_GATE_REGISTRY_MISMATCH` for an empty,
                duplicate or unknown gate id, for a gate with no threshold, or
                when the declared set does not equal the registry in both
                directions. Refused at construction rather than at arming,
                because a protocol that cannot be scored should not be
                representable — which is the whole repair.
        """
        if not self.protocol_version.strip():
            raise SoakContractError(
                Code.SOAK_GATE_REGISTRY_MISMATCH,
                "a protocol must state its own version explicitly; a generic contract "
                "with a default version stamps the wrong one on the next soak",
            )
        ids = list(self.declared_gate_ids)
        blank = [row for row in ids if not str(row).strip()]
        if blank:
            raise SoakContractError(
                Code.SOAK_GATE_REGISTRY_MISMATCH,
                f"{len(blank)} declared gate id(s) are empty",
            )
        duplicates = sorted({row for row in ids if ids.count(row) > 1})
        if duplicates:
            raise SoakContractError(
                Code.SOAK_GATE_REGISTRY_MISMATCH,
                f"duplicate declared gate id(s) {duplicates}. They are refused rather "
                "than de-duplicated: a protocol that declares a gate twice does not "
                "mean what its author thought",
            )
        canonical = set(canonical_gate_ids())
        declared = set(ids)
        unknown = sorted(declared - canonical)
        missing = sorted(canonical - declared)
        if unknown or missing:
            raise SoakContractError(
                Code.SOAK_GATE_REGISTRY_MISMATCH,
                "the declared gate set is not the canonical registry set: "
                + (f"declared with no evaluator {unknown}; " if unknown else "")
                + (f"evaluator present but undeclared {missing}" if missing else "")
                + ". Both directions matter — a gate with no evaluator cannot be "
                "scored, and a check the scorer can perform but the protocol never "
                "declared is a safety check silently switched off",
            )
        without_threshold = sorted(declared - set(self.gate_thresholds))
        if without_threshold:
            raise SoakContractError(
                Code.SOAK_GATE_REGISTRY_MISMATCH,
                f"declared gate(s) {without_threshold} have no threshold. A gate "
                "whose limit is unstated has no rule",
            )
        # Canonical ordering: the identity of a protocol is the *set* of gates
        # it declares, so the order a caller passed them in must not reach the
        # fingerprint.
        object.__setattr__(self, "declared_gate_ids", tuple(sorted(declared)))
        object.__setattr__(
            self, "required_drill_types", tuple(sorted(set(self.required_drill_types)))
        )
        object.__setattr__(
            self,
            "gate_thresholds",
            {key: int(self.gate_thresholds[key]) for key in sorted(declared)},
        )

    @classmethod
    def canonical(
        cls,
        *,
        protocol_version: str,
        protocol_id: str,
        qualifying_commit: str,
        required_sessions: int,
        required_drill_types: Iterable[str],
        **bindings: Any,
    ) -> PaperShadowSoakProtocol:
        """Build a protocol declaring exactly the canonical registry gates.

        This is how a future soak should be constructed: the gate set comes
        from the registry rather than from a list somebody maintains, so the
        two cannot drift apart.

        Args:
            protocol_version: This instance's version.
            protocol_id: A stable name.
            qualifying_commit: The build judged.
            required_sessions: How many eligible sessions are needed.
            required_drill_types: Which drills must pass.
            **bindings: The remaining fingerprints the protocol binds.

        Returns:
            The protocol.
        """
        drills = tuple(sorted(set(map(str, required_drill_types))))
        return cls(
            protocol_version=protocol_version,
            protocol_id=protocol_id,
            qualifying_commit=qualifying_commit,
            required_sessions=required_sessions,
            declared_gate_ids=canonical_gate_ids(),
            gate_thresholds=canonical_thresholds(
                required_sessions=required_sessions, required_drills=len(drills)
            ),
            required_drill_types=drills,
            **bindings,
        )

    def body(self) -> dict[str, Any]:
        """Return the protocol's semantic content, canonically ordered."""
        return {
            "protocol_family": self.protocol_family,
            "protocol_version": self.protocol_version,
            "protocol_id": self.protocol_id,
            "qualifying_commit": self.qualifying_commit,
            "scorer_version": self.scorer_version,
            "gate_registry_fingerprint": self.gate_registry_fingerprint,
            "declared_gate_ids": list(self.declared_gate_ids),
            "gate_thresholds": dict(self.gate_thresholds),
            "required_sessions": self.required_sessions,
            "required_drill_types": list(self.required_drill_types),
            "observation_schema_version": self.observation_schema_version,
            "drill_schema_version": self.drill_schema_version,
            "runtime_binding_fingerprint": self.runtime_binding_fingerprint,
            "paper_account_fingerprint": self.paper_account_fingerprint,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_fingerprint": self.strategy_fingerprint,
            "strategy_authorization_fingerprint": self.strategy_authorization_fingerprint,
            "risk_policy_fingerprint": self.risk_policy_fingerprint,
            "operations_policy_fingerprint": self.operations_policy_fingerprint,
            "calendar_version": self.calendar_version,
            "calendar_fingerprint": self.calendar_fingerprint,
            "schedule_version": self.schedule_version,
            "schedule_fingerprint": self.schedule_fingerprint,
            "score_availability_profile_fingerprint": (self.score_availability_profile_fingerprint),
            "execution_timing_profile_fingerprint": (self.execution_timing_profile_fingerprint),
            "prospective_only": True,
            "backfill": "NONE",
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """Digest the protocol. Invariant to the order gates were supplied in."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the protocol with its fingerprint."""
        return {**self.body(), "protocol_fingerprint": self.fingerprint()}

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> PaperShadowSoakProtocol:
        """Rebuild a protocol from its stored form.

        Args:
            body: What was written.

        Returns:
            The protocol.

        Raises:
            SoakContractError: `SOAK_GATE_REGISTRY_MISMATCH` if the record is
                not this contract family, or if what it declares no longer
                corresponds to the registry — which is the honest outcome when
                a stored protocol outlives the evaluators it was written
                against.
        """
        if str(body.get("protocol_family")) != PROTOCOL_FAMILY:
            raise SoakContractError(
                Code.SOAK_GATE_REGISTRY_MISMATCH,
                f"protocol family is {body.get('protocol_family')!r}, expected "
                f"{PROTOCOL_FAMILY!r}. Historical V1 and V2 protocols are read by "
                "their own classes and are never reinterpreted as this contract",
            )
        return cls(
            protocol_version=str(body["protocol_version"]),
            protocol_id=str(body["protocol_id"]),
            qualifying_commit=str(body["qualifying_commit"]),
            required_sessions=int(body["required_sessions"]),
            declared_gate_ids=tuple(str(row) for row in body.get("declared_gate_ids", ())),
            gate_thresholds={
                str(key): int(value) for key, value in dict(body.get("gate_thresholds", {})).items()
            },
            required_drill_types=tuple(str(row) for row in body.get("required_drill_types", ())),
            runtime_binding_fingerprint=str(body["runtime_binding_fingerprint"]),
            paper_account_fingerprint=str(body["paper_account_fingerprint"]),
            strategy_id=str(body["strategy_id"]),
            strategy_version=str(body["strategy_version"]),
            strategy_fingerprint=str(body["strategy_fingerprint"]),
            strategy_authorization_fingerprint=str(body["strategy_authorization_fingerprint"]),
            risk_policy_fingerprint=str(body["risk_policy_fingerprint"]),
            operations_policy_fingerprint=str(body["operations_policy_fingerprint"]),
            calendar_version=str(body["calendar_version"]),
            calendar_fingerprint=str(body["calendar_fingerprint"]),
            schedule_version=str(body["schedule_version"]),
            schedule_fingerprint=str(body["schedule_fingerprint"]),
            score_availability_profile_fingerprint=str(
                body["score_availability_profile_fingerprint"]
            ),
            execution_timing_profile_fingerprint=str(body["execution_timing_profile_fingerprint"]),
            observation_schema_version=str(
                body.get("observation_schema_version", "PAPER_SHADOW_SESSION_OBSERVATION_V2")
            ),
            drill_schema_version=str(
                body.get("drill_schema_version", "PAPER_OPERATIONS_DRILL_EVIDENCE_V2")
            ),
            scorer_version=str(body.get("scorer_version", SCORER_VERSION)),
            gate_registry_fingerprint=str(body.get("gate_registry_fingerprint", "")),
        )


@dataclass(frozen=True)
class PaperShadowSoakActivation:
    """When a soak began observing, and what it bound.

    Attributes:
        activation_version: This instance's version, as data. A generic
            activation that defaulted to `..._V2` would stamp a V3 soak with a
            V2 contract, which is precisely the defect this replaces.
        protocol_version: The protocol it activates. Checked against the
            protocol itself, so a V3 protocol cannot be activated by a V2
            activation.
        protocol_fingerprint: The protocol instance. The immutable identity —
            a version string alone is not one.
        qualifying_commit: The build judged.
        activated_at: The prospective boundary.
        window: The eligible sessions this soak is scored on.
        decision_sessions: Scheduled strategy decisions inside the window.
        execution_sessions: Their following execution sessions.
        starting_*: Heads and verdicts at activation.
    """

    activation_version: str
    protocol_version: str
    protocol_fingerprint: str
    qualifying_commit: str
    activated_at: str
    required_sessions: int
    window: tuple[str, ...] = ()
    decision_sessions: tuple[str, ...] = ()
    execution_sessions: tuple[str, ...] = ()
    scorer_version: str = SCORER_VERSION
    gate_registry_fingerprint: str = field(default_factory=registry_fingerprint)
    runtime_binding_fingerprint: str = ""
    paper_account_fingerprint: str = ""
    calendar_fingerprint: str = ""
    schedule_fingerprint: str = ""
    strategy_fingerprint: str = ""
    risk_policy_fingerprint: str = ""
    operations_policy_fingerprint: str = ""
    score_availability_profile_fingerprint: str = ""
    execution_timing_profile_fingerprint: str = ""
    starting_operations_head: str = ""
    starting_execution_head: str = ""
    starting_accounting_head: str = ""
    starting_reconciliation: str = ""
    starting_health: str = ""
    activation_family: str = ACTIVATION_FAMILY

    def __post_init__(self) -> None:
        """Refuse an activation that states no version of its own.

        Raises:
            SoakContractError: `SOAK_ACTIVATION_VERSION_MISMATCH`.
        """
        if not self.activation_version.strip() or not self.protocol_version.strip():
            raise SoakContractError(
                Code.SOAK_ACTIVATION_VERSION_MISMATCH,
                "an activation must state its own version and the protocol version it "
                "activates; a default would stamp the wrong contract on the next soak",
            )

    @property
    def first_eligible_session(self) -> str:
        """The first session that may produce evidence."""
        return self.window[0] if self.window else ""

    @property
    def complete_strategy_cycle(self) -> bool:
        """Whether a decision and its following execution are both in the window."""
        return bool(set(self.decision_sessions) and set(self.execution_sessions) & set(self.window))

    def require_matches(self, protocol: PaperShadowSoakProtocol) -> None:
        """Refuse unless this activation belongs to that exact protocol.

        Args:
            protocol: The frozen protocol.

        Raises:
            SoakContractError: `SOAK_ACTIVATION_VERSION_MISMATCH` on a version
                or fingerprint disagreement. The fingerprint is the real check;
                the version is compared too so the failure names the obvious
                case rather than only the cryptographic one.
        """
        if self.protocol_version != protocol.protocol_version:
            raise SoakContractError(
                Code.SOAK_ACTIVATION_VERSION_MISMATCH,
                f"this activation declares protocol {self.protocol_version!r} and the "
                f"frozen protocol is {protocol.protocol_version!r}",
            )
        if self.protocol_fingerprint != protocol.fingerprint():
            raise SoakContractError(
                Code.SOAK_ACTIVATION_VERSION_MISMATCH,
                f"this activation binds protocol {self.protocol_fingerprint[:16]}… and "
                f"the frozen protocol is {protocol.fingerprint()[:16]}…",
            )

    def body(self) -> dict[str, Any]:
        """Return the activation's semantic content. Carries no credential."""
        return {
            "activation_family": self.activation_family,
            "activation_version": self.activation_version,
            "protocol_version": self.protocol_version,
            "protocol_fingerprint": self.protocol_fingerprint,
            "qualifying_commit": self.qualifying_commit,
            "activated_at": self.activated_at,
            "required_sessions": self.required_sessions,
            "window": list(self.window),
            "first_eligible_session": self.first_eligible_session,
            "decision_sessions": list(self.decision_sessions),
            "execution_sessions": list(self.execution_sessions),
            "complete_strategy_cycle": self.complete_strategy_cycle,
            "scorer_version": self.scorer_version,
            "gate_registry_fingerprint": self.gate_registry_fingerprint,
            "runtime_binding_fingerprint": self.runtime_binding_fingerprint,
            "paper_account_fingerprint": self.paper_account_fingerprint,
            "calendar_fingerprint": self.calendar_fingerprint,
            "schedule_fingerprint": self.schedule_fingerprint,
            "strategy_fingerprint": self.strategy_fingerprint,
            "risk_policy_fingerprint": self.risk_policy_fingerprint,
            "operations_policy_fingerprint": self.operations_policy_fingerprint,
            "score_availability_profile_fingerprint": (self.score_availability_profile_fingerprint),
            "execution_timing_profile_fingerprint": (self.execution_timing_profile_fingerprint),
            "starting_operations_head": self.starting_operations_head,
            "starting_execution_head": self.starting_execution_head,
            "starting_accounting_head": self.starting_accounting_head,
            "starting_reconciliation": self.starting_reconciliation,
            "starting_health": self.starting_health,
            "counts_historical_evidence": False,
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """Digest the activation."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the activation with its fingerprint."""
        return {**self.body(), "activation_fingerprint": self.fingerprint()}

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> PaperShadowSoakActivation:
        """Rebuild an activation from its stored form.

        Args:
            body: What was written.

        Returns:
            The activation.

        Raises:
            SoakContractError: `SOAK_ACTIVATION_VERSION_MISMATCH` if the record
                is not this contract family.
        """
        if str(body.get("activation_family")) != ACTIVATION_FAMILY:
            raise SoakContractError(
                Code.SOAK_ACTIVATION_VERSION_MISMATCH,
                f"activation family is {body.get('activation_family')!r}, expected "
                f"{ACTIVATION_FAMILY!r}",
            )
        return cls(
            activation_version=str(body["activation_version"]),
            protocol_version=str(body["protocol_version"]),
            protocol_fingerprint=str(body["protocol_fingerprint"]),
            qualifying_commit=str(body["qualifying_commit"]),
            activated_at=str(body["activated_at"]),
            required_sessions=int(body["required_sessions"]),
            window=tuple(str(row) for row in body.get("window", ())),
            decision_sessions=tuple(str(row) for row in body.get("decision_sessions", ())),
            execution_sessions=tuple(str(row) for row in body.get("execution_sessions", ())),
            scorer_version=str(body.get("scorer_version", SCORER_VERSION)),
            gate_registry_fingerprint=str(body.get("gate_registry_fingerprint", "")),
            runtime_binding_fingerprint=str(body.get("runtime_binding_fingerprint", "")),
            paper_account_fingerprint=str(body.get("paper_account_fingerprint", "")),
            calendar_fingerprint=str(body.get("calendar_fingerprint", "")),
            schedule_fingerprint=str(body.get("schedule_fingerprint", "")),
            strategy_fingerprint=str(body.get("strategy_fingerprint", "")),
            risk_policy_fingerprint=str(body.get("risk_policy_fingerprint", "")),
            operations_policy_fingerprint=str(body.get("operations_policy_fingerprint", "")),
            score_availability_profile_fingerprint=str(
                body.get("score_availability_profile_fingerprint", "")
            ),
            execution_timing_profile_fingerprint=str(
                body.get("execution_timing_profile_fingerprint", "")
            ),
            starting_operations_head=str(body.get("starting_operations_head", "")),
            starting_execution_head=str(body.get("starting_execution_head", "")),
            starting_accounting_head=str(body.get("starting_accounting_head", "")),
            starting_reconciliation=str(body.get("starting_reconciliation", "")),
            starting_health=str(body.get("starting_health", "")),
        )
