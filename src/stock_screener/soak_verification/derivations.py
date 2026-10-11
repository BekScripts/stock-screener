"""Deterministic derivations: authoritative records in, one gate metric out.

The rule this module enforces is the one P2.4R3 halted on. A safety metric is
*derived* from immutable records written while operating. It is never supplied
by a caller, and it is never defaulted when the records are missing.

So every derivation returns a `DerivedMetric` with a status, and there are three
statuses rather than two, because three distinct things can be true:

    OBSERVED_ZERO       the records exist, were read, and nothing happened
    OBSERVED_VALUE      the records exist and show a count
    EVIDENCE_MISSING    the records needed to answer do not exist

Only the first two are scoreable. The third is an integrity failure that the
scorer turns into a halt, because "no evidence that the kill switch was
bypassed" and "evidence that it was not" are different sentences and only one of
them is safe to promote on.

Each derivation declares the source schemas it needs, and the preflight refuses
one that names a schema the source registry does not describe. Each carries its
own version, so changing how an invariant is measured changes the evidence
contract fingerprint and forces a protocol restart rather than silently
re-measuring a soak already in flight.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .sources import SOURCE_SCHEMAS

if TYPE_CHECKING:
    from collections.abc import Callable

    from .sources import OperationalRecords

__all__ = [
    "DERIVATION_REGISTRY_VERSION",
    "Derivation",
    "DerivationContext",
    "DerivedMetric",
    "GateScope",
    "MetricStatus",
    "derivation_registry",
    "derivation_registry_fingerprint",
]

#: The derivation registry contract.
DERIVATION_REGISTRY_VERSION = "SOAK_GATE_DERIVATION_REGISTRY_V1"


class GateScope(StrEnum):
    """What a gate's metric is measured over.

    Not every safety invariant is a per-session count, and forcing them all
    into a session observation is what made `duplicate_counted_sessions`
    nonsense: a gate about counting sessions twice cannot be answered by
    looking at one session.
    """

    SESSION = "SESSION"
    """Measured within one market session, then summed across sessions."""

    SOAK = "SOAK"
    """Measured once over the whole soak."""

    DRILL = "DRILL"
    """Measured over bound drill evidence."""

    COMPLETION = "COMPLETION"
    """Measured at completion, over what is still outstanding."""


class MetricStatus(StrEnum):
    """Whether a derived number is a fact, and what kind."""

    OBSERVED_ZERO = "OBSERVED_ZERO"
    """The sources were read and nothing happened. Scoreable."""

    OBSERVED_VALUE = "OBSERVED_VALUE"
    """The sources were read and show a count. Scoreable."""

    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    """A required source is absent or does not verify. Not scoreable, and never
    a zero: this is the status whose absence let fourteen gates report clean."""

    @property
    def scoreable(self) -> bool:
        """Whether a verdict may be built on this."""
        return self is not MetricStatus.EVIDENCE_MISSING


@dataclass(frozen=True)
class DerivationContext:
    """Everything a derivation is allowed to look at.

    Attributes:
        records: The authoritative operational records.
        session: The session being derived, for a `SESSION`-scoped derivation.
        run_ids: The operational runs belonging to that session.
        protocol_fingerprint: Which protocol, where one is bound.
        qualifying_commit: Which build, where one is bound.
        eligible_sessions: The protocol's eligible sessions, for SOAK scope.
        observations: Live session observations, for SOAK scope.
        required_drills: Which drill types the protocol requires.
        required_sessions: How many sessions the protocol needs.
        operations_policy_fingerprint: The operational rules the protocol bound.
            Supplied rather than read off an observation: drill admissibility
            turns on it, and scraping it from whatever record happened to carry
            it is how a drill for another policy comes to certify this one.
    """

    records: OperationalRecords
    session: str = ""
    run_ids: tuple[str, ...] = ()
    protocol_fingerprint: str = ""
    qualifying_commit: str = ""
    eligible_sessions: tuple[str, ...] = ()
    observations: tuple[Any, ...] = ()
    required_drills: tuple[str, ...] = ()
    required_sessions: int = 0
    operations_policy_fingerprint: str = ""


@dataclass(frozen=True)
class DerivedMetric:
    """One gate's number, with everything needed to audit where it came from.

    Attributes:
        gate_id: Which gate.
        status: Whether it is a fact, and what kind.
        value: The count. `None` when the evidence is missing — deliberately
            not zero, so a caller that ignores `status` gets a type error
            rather than a clean soak.
        derivation_id: Which derivation produced it.
        derivation_version: Which version of it.
        scope: What it was measured over.
        source_schemas: Which sources it required.
        source_identities: The record identities read.
        source_fingerprints: Each source's digest, by schema.
        session: The session, when session-scoped.
        protocol_fingerprint: The protocol, when bound.
        qualifying_commit: The build, when bound.
        detail: What was found, for a reader.
    """

    gate_id: str
    status: MetricStatus
    value: int | None
    derivation_id: str
    derivation_version: str
    scope: GateScope
    source_schemas: tuple[str, ...] = ()
    source_identities: tuple[str, ...] = ()
    source_fingerprints: tuple[tuple[str, str], ...] = ()
    session: str = ""
    protocol_fingerprint: str = ""
    qualifying_commit: str = ""
    detail: tuple[str, ...] = ()

    def body(self) -> dict[str, Any]:
        """The metric's semantic content, for fingerprinting."""
        return {
            "gate_id": self.gate_id,
            "status": str(self.status),
            "value": self.value,
            "derivation_id": self.derivation_id,
            "derivation_version": self.derivation_version,
            "scope": str(self.scope),
            "source_schemas": list(self.source_schemas),
            "source_identities": list(self.source_identities),
            "source_fingerprints": [list(row) for row in self.source_fingerprints],
            "session": self.session,
            "protocol_fingerprint": self.protocol_fingerprint,
            "qualifying_commit": self.qualifying_commit,
        }

    def fingerprint(self) -> str:
        """A digest binding the value to the evidence behind it."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the metric as a JSON-safe mapping."""
        return {
            **self.body(),
            "detail": list(self.detail),
            "metric_fingerprint": self.fingerprint(),
        }

    def require_value(self) -> int:
        """The count, refusing to invent one.

        Returns:
            The observed count.

        Raises:
            SoakContractError: `SOAK_GATE_EVIDENCE_MISSING` when the sources
                could not answer. This is the method the scorer calls, and it
                raising is what makes missing evidence a halt instead of a
                zero.
        """
        from .errors import Code, SoakContractError

        if self.value is None or not self.status.scoreable:
            raise SoakContractError(
                Code.SOAK_GATE_EVIDENCE_MISSING,
                f"{self.gate_id}: {'; '.join(self.detail) or 'a required source is absent'}. "
                "Missing evidence is not an observation of zero, and a soak is not "
                "promoted on the absence of a record nobody wrote",
            )
        return int(self.value)


@dataclass(frozen=True)
class Derivation:
    """One deterministic mapping from authoritative records to a gate metric.

    Attributes:
        derivation_id: Stable id. Recorded in the evidence contract, so a
            changed derivation is visible as a fingerprint change.
        version: This derivation's version.
        scope: What it measures over.
        gates: The gate ids it can answer.
        required_schemas: Source schemas it reads. The preflight refuses any
            name the source registry does not describe.
        rule: What it means, in a sentence.
        derive: The implementation.
    """

    derivation_id: str
    version: str
    scope: GateScope
    gates: tuple[str, ...]
    required_schemas: tuple[str, ...]
    rule: str
    derive: Callable[[DerivationContext, str], DerivedMetric]

    def body(self) -> dict[str, Any]:
        """The derivation's semantic content, for fingerprinting."""
        return {
            "derivation_id": self.derivation_id,
            "version": self.version,
            "scope": str(self.scope),
            "gates": list(self.gates),
            "required_schemas": list(self.required_schemas),
            "rule": self.rule,
        }

    def run(self, context: DerivationContext, gate_id: str) -> DerivedMetric:
        """Derive one gate's metric, failing closed on an unusable source.

        The source check happens here rather than in each implementation, so a
        new derivation cannot forget it.

        Args:
            context: What it may read.
            gate_id: Which gate to answer.

        Returns:
            The metric, with `EVIDENCE_MISSING` when a required source is
            absent or does not verify.
        """
        missing = []
        prints: list[tuple[str, str]] = []
        for schema_id in self.required_schemas:
            read = _read_for(context, schema_id)
            if read is None or not read.usable:
                reason = "absent" if read is None or not read.present else "does not verify"
                missing.append(f"{schema_id} {reason}")
            else:
                prints.append((schema_id, read.fingerprint))
        if missing:
            return DerivedMetric(
                gate_id=gate_id,
                status=MetricStatus.EVIDENCE_MISSING,
                value=None,
                derivation_id=self.derivation_id,
                derivation_version=self.version,
                scope=self.scope,
                source_schemas=self.required_schemas,
                source_fingerprints=tuple(prints),
                session=context.session,
                protocol_fingerprint=context.protocol_fingerprint,
                qualifying_commit=context.qualifying_commit,
                detail=tuple(missing),
            )
        return self.derive(context, gate_id)

    def observed(
        self,
        context: DerivationContext,
        gate_id: str,
        value: int,
        *,
        detail: tuple[str, ...] = (),
        identities: tuple[str, ...] = (),
    ) -> DerivedMetric:
        """Build a scoreable metric, choosing the status from the value.

        Args:
            context: The context it was derived from.
            gate_id: Which gate.
            value: The count.
            detail: What was found.
            identities: The record identities behind it.

        Returns:
            The metric.
        """
        return DerivedMetric(
            gate_id=gate_id,
            status=MetricStatus.OBSERVED_ZERO if value == 0 else MetricStatus.OBSERVED_VALUE,
            value=value,
            derivation_id=self.derivation_id,
            derivation_version=self.version,
            scope=self.scope,
            source_schemas=self.required_schemas,
            source_identities=identities,
            source_fingerprints=tuple(
                (schema_id, read.fingerprint)
                for schema_id in self.required_schemas
                if (read := _read_for(context, schema_id)) is not None
            ),
            session=context.session,
            protocol_fingerprint=context.protocol_fingerprint,
            qualifying_commit=context.qualifying_commit,
            detail=detail,
        )


def _read_for(context: DerivationContext, schema_id: str) -> Any:
    """The source read for a schema id, or None when nothing reads it."""
    readers = {
        "OPERATIONS_RUN_JOURNAL": context.records.operations_journal,
        "EXECUTION_JOURNAL": context.records.execution_journal,
        "ACCOUNTING_LEDGER": context.records.accounting_ledger,
        "RECONCILIATION_RECORDS": context.records.reconciliations,
        "STRATEGY_DECISIONS": context.records.decisions,
        "RISK_TRANSFORMATIONS": context.records.transformations,
        "SUBMISSION_INTENTS": context.records.submission_intents,
        "EXTERNAL_REQUEST_AUDIT": context.records.request_audit,
        "INCIDENT_HISTORY": context.records.incidents,
        "DRILL_RECORDS": context.records.drills,
    }
    reader = readers.get(schema_id)
    return reader() if reader is not None else None


def _evidence(entry: Any) -> dict[str, str]:
    """One journal entry's evidence pairs as a mapping."""
    return {str(key): str(value) for key, value in getattr(entry, "evidence", ())}


def derivation_registry() -> dict[str, Derivation]:
    """Every derivation, by id.

    Returns:
        The registry. The evidence contract maps each gate to one of these, and
        the preflight refuses a gate whose derivation is absent.
    """
    from . import _derivation_impl as impl

    return impl.build()


def derivation_registry_fingerprint() -> str:
    """A digest over every derivation's semantic content."""
    from stock_screener.execution import fingerprint_of

    registry = derivation_registry()
    return fingerprint_of(
        {
            "version": DERIVATION_REGISTRY_VERSION,
            "derivations": [registry[key].body() for key in sorted(registry)],
            "source_schemas": {key: SOURCE_SCHEMAS[key].version for key in sorted(SOURCE_SCHEMAS)},
        }
    )
