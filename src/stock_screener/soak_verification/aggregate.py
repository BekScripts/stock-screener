"""Resolving one gate id to one derived metric, at the right scope.

The scorer asks "what is `kill_bypasses`?" and this answers, by looking up how
the evidence contract says that gate is measured and then measuring it:

- a `SESSION` gate is summed across the sessions' own finalised evidence;
- a `SOAK`, `DRILL` or `COMPLETION` gate is derived once, here, over the whole
  soak's records.

The session case is where the care is. Each observation carries the
`SESSION_GATE_EVIDENCE_V1` that was derived when that session was finalised —
one structured, fingerprinted object with provenance, not a scatter of integer
fields. Summing it is therefore reading evidence, not reading attributes, and
the difference matters: an observation that carries *no* session evidence is
missing evidence rather than a clean session, and an observation one of whose
metrics was unmeasurable makes the whole aggregate unmeasurable. A soak is not
promoted on sessions nobody could measure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .derivations import (
    DerivationContext,
    DerivedMetric,
    GateScope,
    MetricStatus,
    derivation_registry,
)
from .evidence_contract import evidence_contract

if TYPE_CHECKING:
    from .evidence_contract import SoakGateEvidenceContract
    from .sources import OperationalRecords

__all__ = ["DerivedSoakEvidence", "evidence_for_store", "session_evidence_of"]


def evidence_for_store(store: Any, *, root: Any = None) -> DerivedSoakEvidence:
    """Build the evidence a scorer reads, from a soak store and its records.

    The protocol and activation the store already holds supply the bindings, so
    a caller cannot score a soak against a protocol it was not run under.

    Args:
        store: The soak store, holding the frozen protocol and activation.
        root: The operations root the records live under. Defaults to the
            store's own root.

    Returns:
        The evidence.
    """
    from .sources import OperationalRecords

    protocol = store.protocol()
    activation = store.activation()
    base = root if root is not None else store.root
    eligible: tuple[str, ...] = ()
    if activation is not None:
        eligible = tuple(getattr(activation, "window", ()) or ())
    return DerivedSoakEvidence(
        records=OperationalRecords(base, soak_root=base / "soak"),
        store=store,
        protocol_fingerprint="" if protocol is None else protocol.fingerprint(),
        qualifying_commit="" if protocol is None else protocol.qualifying_commit,
        eligible_sessions=eligible,
        required_drills=(
            () if protocol is None else tuple(str(row) for row in protocol.required_drill_types)
        ),
        required_sessions=0 if protocol is None else protocol.required_sessions,
        operations_policy_fingerprint=(
            "" if protocol is None else protocol.operations_policy_fingerprint
        ),
    )


def session_evidence_of(observation: Any) -> Any:
    """The finalised session evidence an observation carries, or None.

    Read through one named accessor rather than by poking at the observation,
    so an observation schema that does not bind derived evidence is detected
    as such instead of silently contributing zeros.
    """
    return getattr(observation, "gate_evidence", None)


@dataclass
class DerivedSoakEvidence:
    """Every gate's metric for one soak, derived at its declared scope.

    Attributes:
        records: The authoritative operational records.
        store: The soak store, for observations, state and drill rejections.
        protocol_fingerprint: Which protocol.
        qualifying_commit: Which build.
        eligible_sessions: The sessions the activation declared eligible.
        required_drills: Which drill types the protocol requires.
        required_sessions: How many sessions it needs.
        operations_policy_fingerprint: The operational rules it bound, for
            drill admissibility.
        contract: How each gate is measured. Defaults to this build's.
    """

    records: OperationalRecords
    store: Any = None
    protocol_fingerprint: str = ""
    qualifying_commit: str = ""
    eligible_sessions: tuple[str, ...] = ()
    required_drills: tuple[str, ...] = ()
    required_sessions: int = 0
    operations_policy_fingerprint: str = ""
    contract: SoakGateEvidenceContract | None = None
    _resolved: dict[str, DerivedMetric] = field(default_factory=dict, repr=False)

    @property
    def bound_contract(self) -> SoakGateEvidenceContract:
        """The evidence contract in force."""
        if self.contract is None:
            self.contract = evidence_contract()
        return self.contract

    def observations(self) -> tuple[Any, ...]:
        """Live session observations, or none when there is no store."""
        if self.store is None:
            return ()
        return tuple(self.store.live_observations())

    def counted_sessions(self) -> tuple[str, ...]:
        """Each eligible session counted at most once."""
        if self.store is None:
            return ()
        return tuple(self.store.counted_sessions())

    def soak_state(self) -> str:
        """The soak's recorded state."""
        return "" if self.store is None else str(self.store.soak_state())

    def drill_rejections(self) -> tuple[str, ...]:
        """Why any drill evidence was inadmissible."""
        reader = getattr(self.store, "drill_rejections", None)
        return tuple(reader()) if reader is not None else ()

    def _context(self) -> DerivationContext:
        """The context a soak-scoped derivation reads."""
        return DerivationContext(
            records=self.records,
            protocol_fingerprint=self.protocol_fingerprint,
            qualifying_commit=self.qualifying_commit,
            eligible_sessions=self.eligible_sessions,
            observations=self.observations(),
            required_drills=self.required_drills,
            required_sessions=self.required_sessions,
            operations_policy_fingerprint=self.operations_policy_fingerprint,
        )

    def metric_for(self, gate_id: str) -> DerivedMetric:
        """The derived metric for one gate.

        Args:
            gate_id: The gate.

        Returns:
            Its metric, with `EVIDENCE_MISSING` where the records could not
            answer.

        Raises:
            SoakContractError: `SOAK_GATE_DERIVATION_MISSING` for a gate the
                evidence contract does not bind.
        """
        if gate_id in self._resolved:
            return self._resolved[gate_id]
        scope = self.bound_contract.scope_of(gate_id)
        metric = (
            self._session_sum(gate_id) if scope is GateScope.SESSION else self._derive_once(gate_id)
        )
        self._resolved[gate_id] = metric
        return metric

    def _derive_once(self, gate_id: str) -> DerivedMetric:
        """Derive a soak-, drill- or completion-scoped gate."""
        binding = self.bound_contract.bindings[gate_id]
        derivation = derivation_registry()[binding.derivation_id]
        return derivation.run(self._context(), gate_id)

    def _session_sum(self, gate_id: str) -> DerivedMetric:
        """Sum one session-scoped gate across the sessions' own evidence."""
        binding = self.bound_contract.bindings[gate_id]
        rows = self.observations()
        total = 0
        sessions: list[str] = []
        prints: list[tuple[str, str]] = []
        detail: list[str] = []
        unmeasured: list[str] = []
        for observation in rows:
            session = str(getattr(observation, "session", ""))
            sessions.append(session)
            evidence = session_evidence_of(observation)
            if evidence is None:
                unmeasured.append(
                    f"session {session} carries no {binding.derivation_id} evidence; its "
                    "observation schema does not bind derived gate evidence"
                )
                continue
            metric = evidence.metrics.get(gate_id)
            if metric is None:
                unmeasured.append(f"session {session} evidence does not include {gate_id}")
                continue
            if metric.status is MetricStatus.EVIDENCE_MISSING:
                unmeasured.append(f"session {session}: {'; '.join(metric.detail)}")
                continue
            total += int(metric.value or 0)
            detail += [f"{session}: {line}" for line in metric.detail]
            prints.append((session, metric.fingerprint()))
        if unmeasured:
            return DerivedMetric(
                gate_id=gate_id,
                status=MetricStatus.EVIDENCE_MISSING,
                value=None,
                derivation_id=binding.derivation_id,
                derivation_version=binding.derivation_version,
                scope=GateScope.SESSION,
                source_schemas=binding.required_schemas,
                protocol_fingerprint=self.protocol_fingerprint,
                qualifying_commit=self.qualifying_commit,
                detail=tuple(unmeasured),
            )
        return DerivedMetric(
            gate_id=gate_id,
            status=MetricStatus.OBSERVED_ZERO if total == 0 else MetricStatus.OBSERVED_VALUE,
            value=total,
            derivation_id=binding.derivation_id,
            derivation_version=binding.derivation_version,
            scope=GateScope.SESSION,
            source_schemas=binding.required_schemas,
            source_identities=tuple(sessions),
            source_fingerprints=tuple(prints),
            protocol_fingerprint=self.protocol_fingerprint,
            qualifying_commit=self.qualifying_commit,
            detail=tuple(detail),
        )

    def metrics(self, gate_ids: tuple[str, ...]) -> dict[str, DerivedMetric]:
        """Resolve several gates at once.

        Args:
            gate_ids: Which gates.

        Returns:
            Their metrics, by gate id.
        """
        return {gate_id: self.metric_for(gate_id) for gate_id in gate_ids}
