"""`SESSION_GATE_EVIDENCE_V1` — one session's derived safety metrics.

Built at finalization from the immutable records that session left behind, and
built *only* that way. There is deliberately no constructor taking metric
values, no `set_metric`, and no keyword argument that lets a caller say how many
times the kill switch was bypassed. The whole class of defect this phase exists
to remove is a safety number that something other than the records decided.

Two consequences are worth stating plainly.

**Finalization is a read of history, not a question to the venue.** A session is
finalised from the journal, ledger, audit and reconciliation records captured
while it ran. Asking the broker today what was true three weeks ago would
produce a number with a confident timestamp and no relationship to the session
it claims to describe.

**An incomplete session is never clean.** If one required source cannot be read,
the evidence is marked `SESSION_GATE_EVIDENCE_INCOMPLETE` and carries the
missing metrics as missing. Writing a tidy observation with zeros in it is how
fourteen gates came to certify a system nobody had measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .derivations import DerivationContext, GateScope, MetricStatus, derivation_registry
from .evidence_contract import evidence_contract

if TYPE_CHECKING:
    from .derivations import DerivedMetric
    from .evidence_contract import SoakGateEvidenceContract
    from .sources import OperationalRecords

__all__ = [
    "SESSION_GATE_EVIDENCE_VERSION",
    "SessionGateEvidence",
    "derive_session_evidence",
]

#: The session evidence schema. Independent of the protocol version: how a
#: session is measured may be revised without redefining what the soak requires.
SESSION_GATE_EVIDENCE_VERSION = "SESSION_GATE_EVIDENCE_V1"


@dataclass(frozen=True)
class SessionGateEvidence:
    """Every session-scoped gate metric for one session, with provenance.

    Attributes:
        session: Which market session.
        protocol_fingerprint: Which protocol it was gathered under.
        qualifying_commit: Which build.
        run_ids: The operational runs it was derived from.
        metrics: One derived metric per session-scoped gate, by gate id.
        source_reads: What each authoritative source yielded.
        evidence_contract_fingerprint: How these gates were measured.
        finalized_at: When the derivation ran.
    """

    session: str
    protocol_fingerprint: str
    qualifying_commit: str
    run_ids: tuple[str, ...]
    metrics: dict[str, DerivedMetric]
    source_reads: tuple[dict[str, Any], ...]
    evidence_contract_fingerprint: str
    finalized_at: str = ""

    @property
    def incomplete(self) -> tuple[str, ...]:
        """Gates whose evidence could not be derived. Empty is the clean case."""
        return tuple(
            sorted(
                gate_id
                for gate_id, metric in self.metrics.items()
                if metric.status is MetricStatus.EVIDENCE_MISSING
            )
        )

    @property
    def complete(self) -> bool:
        """Whether every session-scoped gate was measurable."""
        return not self.incomplete

    def require_complete(self) -> None:
        """Refuse to treat an incomplete session as an observation.

        Raises:
            SoakContractError: `SOAK_SESSION_EVIDENCE_INCOMPLETE` naming the
                gates that could not be derived.
        """
        from .errors import Code, SoakContractError

        if self.incomplete:
            reasons = "; ".join(
                f"{gate_id}: {'; '.join(self.metrics[gate_id].detail)}"
                for gate_id in self.incomplete
            )
            raise SoakContractError(
                Code.SOAK_SESSION_EVIDENCE_INCOMPLETE,
                f"session {self.session} cannot be finalised: {reasons}. A session "
                "whose safety evidence is missing is not recorded as a clean one",
            )

    def body(self) -> dict[str, Any]:
        """The evidence's semantic content, for fingerprinting."""
        return {
            "version": SESSION_GATE_EVIDENCE_VERSION,
            "session": self.session,
            "protocol_fingerprint": self.protocol_fingerprint,
            "qualifying_commit": self.qualifying_commit,
            "run_ids": list(self.run_ids),
            "evidence_contract_fingerprint": self.evidence_contract_fingerprint,
            "metrics": [self.metrics[gate_id].as_json() for gate_id in sorted(self.metrics)],
            "source_reads": list(self.source_reads),
            "complete": self.complete,
            "incomplete_gates": list(self.incomplete),
        }

    def fingerprint(self) -> str:
        """A digest binding every metric to the records behind it."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the evidence as a JSON-safe mapping."""
        return {
            **self.body(),
            "finalized_at": self.finalized_at,
            "session_gate_evidence_fingerprint": self.fingerprint(),
        }

    def summary_lines(self) -> tuple[str, ...]:
        """Render the evidence for a terminal."""
        lines = [
            f"session            : {self.session}",
            f"schema             : {SESSION_GATE_EVIDENCE_VERSION}",
            f"evidence contract  : {self.evidence_contract_fingerprint[:16]}…",
            f"runs               : {', '.join(self.run_ids) or '-'}",
            f"complete           : {self.complete}",
        ]
        for gate_id in sorted(self.metrics):
            metric = self.metrics[gate_id]
            value = "—" if metric.value is None else str(metric.value)
            lines.append(f"    {gate_id:42} {value:>5}  {metric.status}")
        return tuple(lines)


def derive_session_evidence(
    records: OperationalRecords,
    *,
    session: str,
    run_ids: tuple[str, ...],
    protocol_fingerprint: str = "",
    qualifying_commit: str = "",
    finalized_at: str = "",
    contract: SoakGateEvidenceContract | None = None,
) -> SessionGateEvidence:
    """Derive every session-scoped gate metric for one session.

    The only way to obtain a `SessionGateEvidence`. Each metric comes from the
    derivation the evidence contract binds to its gate, reading the sources that
    derivation declares.

    Args:
        records: The immutable operational records.
        session: Which session.
        run_ids: The operational runs belonging to it. Supplied rather than
            inferred, because which runs belong to a session is a scheduling
            fact and guessing it from timestamps would silently fold a
            neighbouring session's runs into this one.
        protocol_fingerprint: Which protocol, for provenance.
        qualifying_commit: Which build, for provenance.
        finalized_at: When this ran.
        contract: The evidence contract. Defaults to this build's.

    Returns:
        The session's derived evidence, complete or explicitly not.
    """
    bound = contract if contract is not None else evidence_contract()
    derivations = derivation_registry()
    context = DerivationContext(
        records=records,
        session=session,
        run_ids=run_ids,
        protocol_fingerprint=protocol_fingerprint,
        qualifying_commit=qualifying_commit,
    )
    metrics: dict[str, DerivedMetric] = {}
    for gate_id in bound.gates_in(GateScope.SESSION):
        binding = bound.bindings[gate_id]
        metrics[gate_id] = derivations[binding.derivation_id].run(context, gate_id)
    return SessionGateEvidence(
        session=session,
        protocol_fingerprint=protocol_fingerprint,
        qualifying_commit=qualifying_commit,
        run_ids=run_ids,
        metrics=metrics,
        source_reads=tuple(read.as_json() for read in records.all_reads()),
        evidence_contract_fingerprint=bound.fingerprint(),
        finalized_at=finalized_at,
    )
