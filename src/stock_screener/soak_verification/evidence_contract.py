"""`SOAK_GATE_EVIDENCE_CONTRACT_V1` — how each gate is measured, as data.

A protocol already binds *what* must be true: twenty-four gates and their
thresholds. It said nothing about *how* each one would be measured, and that gap
is what let V2 be frozen with fifteen unscoreable invariants. This contract
closes it. For every gate it records the scope, the derivation and version that
answers it, and the source schemas that derivation reads — and it fingerprints
the whole mapping together with the gate registry and the observability contract
it depends on.

The point of the fingerprint is drift. If somebody changes how
`kill_bypasses` is measured, the contract fingerprint moves, and a soak whose
protocol bound the old one must restart rather than silently finish under a new
definition of the same word. Changing a measurement mid-soak is not a bug fix;
it is a different experiment wearing the old experiment's name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .derivations import (
    DERIVATION_REGISTRY_VERSION,
    GateScope,
    derivation_registry,
    derivation_registry_fingerprint,
)
from .errors import Code, SoakContractError
from .gates import GATE_REGISTRY_VERSION, gate_registry, registry_fingerprint
from .sources import SOURCE_SCHEMA_VERSION, SOURCE_SCHEMAS, known_source_schema

__all__ = [
    "EVIDENCE_CONTRACT_VERSION",
    "GateEvidenceBinding",
    "SoakGateEvidenceContract",
    "evidence_contract",
    "evidence_contract_fingerprint",
    "require_evidence_contract",
]

#: The evidence contract version.
EVIDENCE_CONTRACT_VERSION = "SOAK_GATE_EVIDENCE_CONTRACT_V1"


@dataclass(frozen=True)
class GateEvidenceBinding:
    """How one gate is measured.

    Attributes:
        gate_id: Which gate.
        scope: What its metric is measured over.
        derivation_id: Which derivation answers it.
        derivation_version: Which version of that derivation.
        required_schemas: The source schemas it reads.
        missing_evidence: What happens when a required source is absent.
    """

    gate_id: str
    scope: GateScope
    derivation_id: str
    derivation_version: str
    required_schemas: tuple[str, ...]
    missing_evidence: str = "EVIDENCE_MISSING_HALTS_SCORING"

    def body(self) -> dict[str, Any]:
        """The binding's semantic content."""
        return {
            "gate_id": self.gate_id,
            "scope": str(self.scope),
            "derivation_id": self.derivation_id,
            "derivation_version": self.derivation_version,
            "required_schemas": list(self.required_schemas),
            "source_schema_versions": [
                SOURCE_SCHEMAS[name].version for name in self.required_schemas
            ],
            "missing_evidence": self.missing_evidence,
        }


@dataclass(frozen=True)
class SoakGateEvidenceContract:
    """The whole mapping, fingerprinted.

    Attributes:
        bindings: One per gate, by gate id.
        gate_registry_fingerprint: The registry this was built against.
        observability_fingerprint: The operational event vocabulary the
            derivations depend on. Bound because a derivation reading
            `ORDER_SUBMITTED` is only valid while something emits it.
        derivation_registry_fingerprint: The derivations themselves.
    """

    bindings: dict[str, GateEvidenceBinding]
    gate_registry_fingerprint: str
    observability_fingerprint: str
    derivation_registry_fingerprint: str

    def body(self) -> dict[str, Any]:
        """The contract's semantic content, for fingerprinting."""
        return {
            "version": EVIDENCE_CONTRACT_VERSION,
            "gate_registry_version": GATE_REGISTRY_VERSION,
            "gate_registry_fingerprint": self.gate_registry_fingerprint,
            "observability_contract": "PAPER_OPERATIONS_OBSERVABILITY_V1",
            "observability_fingerprint": self.observability_fingerprint,
            "derivation_registry_version": DERIVATION_REGISTRY_VERSION,
            "derivation_registry_fingerprint": self.derivation_registry_fingerprint,
            "source_schema_version": SOURCE_SCHEMA_VERSION,
            "bindings": [self.bindings[key].body() for key in sorted(self.bindings)],
        }

    def fingerprint(self) -> str:
        """A digest over the whole mapping."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the contract as a JSON-safe mapping."""
        return {**self.body(), "evidence_contract_fingerprint": self.fingerprint()}

    def scope_of(self, gate_id: str) -> GateScope:
        """Which scope a gate is measured in.

        Args:
            gate_id: The gate.

        Returns:
            Its scope.

        Raises:
            SoakContractError: `SOAK_GATE_DERIVATION_MISSING` for a gate this
                contract does not bind.
        """
        if gate_id not in self.bindings:
            raise SoakContractError(
                Code.SOAK_GATE_DERIVATION_MISSING,
                f"{gate_id} has no evidence binding, so there is no deterministic way "
                "to measure it. A gate nobody can measure is not a gate",
            )
        return self.bindings[gate_id].scope

    def gates_in(self, scope: GateScope) -> tuple[str, ...]:
        """Every gate measured in one scope."""
        return tuple(sorted(key for key, row in self.bindings.items() if row.scope is scope))


def evidence_contract() -> SoakGateEvidenceContract:
    """Build the contract from the registries in this build.

    Returns:
        The contract.

    Raises:
        SoakContractError: `SOAK_GATE_DERIVATION_MISSING` if a registered gate
            has no derivation, `SOAK_EVALUATOR_UNDECLARED` if a derivation
            names a gate the registry does not have, or
            `SOAK_SOURCE_SCHEMA_UNKNOWN` if a derivation reads a source nobody
            has described. Each is checked here rather than at scoring time,
            because the whole purpose of a contract is to fail before the soak
            rather than during it.
    """
    from stock_screener.paper_runtime import observability_fingerprint

    gates = gate_registry()
    derivations = derivation_registry()
    by_gate: dict[str, str] = {}
    for derivation in derivations.values():
        for schema_id in derivation.required_schemas:
            if not known_source_schema(schema_id):
                raise SoakContractError(
                    Code.SOAK_SOURCE_SCHEMA_UNKNOWN,
                    f"{derivation.derivation_id} reads source {schema_id!r}, which the "
                    "source registry does not describe. An undescribed source cannot "
                    "be audited and must not back a safety claim",
                )
        for gate_id in derivation.gates:
            if gate_id not in gates:
                raise SoakContractError(
                    Code.SOAK_EVALUATOR_UNDECLARED,
                    f"{derivation.derivation_id} claims to measure {gate_id!r}, which "
                    "the gate registry does not declare",
                )
            if gate_id in by_gate:
                raise SoakContractError(
                    Code.SOAK_GATE_DERIVATION_MISSING,
                    f"{gate_id!r} is claimed by both {by_gate[gate_id]!r} and "
                    f"{derivation.derivation_id!r}. One gate, one derivation: two "
                    "would let the same invariant be measured two ways",
                )
            by_gate[gate_id] = derivation.derivation_id

    missing = sorted(set(gates) - set(by_gate))
    if missing:
        raise SoakContractError(
            Code.SOAK_GATE_DERIVATION_MISSING,
            f"{len(missing)} declared gate(s) have no derivation: {missing}. This is "
            "the P2.4R3 halt condition and it is refused here rather than discovered "
            "when a soak is scored",
        )

    bindings = {
        gate_id: GateEvidenceBinding(
            gate_id=gate_id,
            scope=derivations[by_gate[gate_id]].scope,
            derivation_id=by_gate[gate_id],
            derivation_version=derivations[by_gate[gate_id]].version,
            required_schemas=derivations[by_gate[gate_id]].required_schemas,
        )
        for gate_id in sorted(gates)
    }
    return SoakGateEvidenceContract(
        bindings=bindings,
        gate_registry_fingerprint=registry_fingerprint(),
        observability_fingerprint=observability_fingerprint(),
        derivation_registry_fingerprint=derivation_registry_fingerprint(),
    )


def evidence_contract_fingerprint() -> str:
    """The contract fingerprint a protocol binds."""
    return evidence_contract().fingerprint()


def require_evidence_contract(bound: str) -> None:
    """Refuse a soak whose bound contract is not the one in this build.

    Args:
        bound: The fingerprint the protocol bound.

    Raises:
        SoakContractError: `SOAK_EVIDENCE_CONTRACT_MISMATCH` when they differ.
            Scoring a soak under a changed measurement is how a definition
            quietly replaces the one that was frozen.
    """
    current = evidence_contract_fingerprint()
    if bound != current:
        raise SoakContractError(
            Code.SOAK_EVIDENCE_CONTRACT_MISMATCH,
            f"this soak was frozen against evidence contract {bound[:16]}… and this "
            f"build measures its gates as {current[:16]}…. How a safety invariant is "
            "measured is part of the experiment, so the soak restarts rather than "
            "finishing under a different definition",
        )
