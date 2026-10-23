"""`PAPER_SHADOW_SESSION_OBSERVATION_V3` — an observation that binds its evidence.

The V2 observation carried safety numbers as its own integer fields:
`live_requests`, `orders_submitted`, and a dozen more that were never written
because nothing derived them. Fourteen gates read fields it did not have and
reported clean.

So this schema carries no safety integers at all. It carries one
`SESSION_GATE_EVIDENCE_V1` object — derived at finalization from the session's
own immutable records, with a status per metric and the source fingerprints
behind each one — plus the references needed to re-derive it. A reader asking
"how many kill bypasses did this session have" gets an answer that knows whether
it was measured.

The schema version is deliberately independent of the protocol version. How a
session is recorded may be revised without redefining what the soak requires of
it; conversely a new protocol does not imply a new observation shape. The two
are bound separately, and the evidence contract fingerprint inside the evidence
is what ties a session's numbers to the way they were measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .session_evidence import SessionGateEvidence

__all__ = [
    "SESSION_OBSERVATION_V3_VERSION",
    "DerivedSessionObservation",
    "observation_from_body",
]

#: The observation schema version.
SESSION_OBSERVATION_V3_VERSION = "PAPER_SHADOW_SESSION_OBSERVATION_V3"


@dataclass(frozen=True)
class DerivedSessionObservation:
    """One eligible session, recorded with its derived gate evidence.

    Attributes:
        protocol_fingerprint: Which protocol instance it belongs to.
        session: The market session.
        qualifying_commit: The build it was gathered under.
        observed_at: When it was finalised.
        run_ids: The operational runs it was derived from.
        gate_evidence: The session's derived safety metrics, with provenance.
        classification: Why this session counts, or does not.
        early_close: Whether the session closed early.
        superseded_by: The observation that corrects this one, or `None` while
            this one stands. `None` rather than `""` because the store's
            liveness test is `is None`, and a record that reads as superseded
            by the empty string is a record the scorer never sees. Outside the
            fingerprinted body: marking a record superseded must not change its
            own content-addressed identity, or the file orphans itself.
    """

    protocol_fingerprint: str
    session: str
    qualifying_commit: str
    observed_at: str
    run_ids: tuple[str, ...]
    gate_evidence: SessionGateEvidence
    classification: str = "QUALIFYING"
    early_close: bool = False
    superseded_by: str | None = None
    version: str = field(default=SESSION_OBSERVATION_V3_VERSION)

    def __post_init__(self) -> None:
        """Refuse an observation whose evidence is for another session.

        Raises:
            SoakContractError: `SOAK_SESSION_EVIDENCE_INCOMPLETE` if the
                evidence describes a different session. Binding one session's
                numbers to another's record is worse than having none.
        """
        from .errors import Code, SoakContractError

        if self.gate_evidence.session != self.session:
            raise SoakContractError(
                Code.SOAK_SESSION_EVIDENCE_INCOMPLETE,
                f"this observation is for {self.session} and its evidence was derived "
                f"for {self.gate_evidence.session}",
            )

    def body(self) -> dict[str, Any]:
        """The observation's semantic content, for fingerprinting."""
        return {
            "version": self.version,
            "protocol_fingerprint": self.protocol_fingerprint,
            "session": self.session,
            "qualifying_commit": self.qualifying_commit,
            "run_ids": list(self.run_ids),
            "classification": self.classification,
            "early_close": self.early_close,
            "session_gate_evidence_fingerprint": self.gate_evidence.fingerprint(),
            "evidence_contract_fingerprint": (self.gate_evidence.evidence_contract_fingerprint),
        }

    def fingerprint(self) -> str:
        """A content digest. Excludes `observed_at` and `superseded_by`."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    @property
    def observation_id(self) -> str:
        """The content-addressed id this observation is stored under."""
        return f"{self.session}-{self.fingerprint()[:16]}"

    @property
    def complete(self) -> bool:
        """Whether every session-scoped gate was measurable."""
        return self.gate_evidence.complete

    def as_json(self) -> dict[str, Any]:
        """Return the observation as a JSON-safe mapping."""
        return {
            **self.body(),
            "observed_at": self.observed_at,
            "superseded_by": self.superseded_by,
            "observation_id": self.observation_id,
            "observation_fingerprint": self.fingerprint(),
            "gate_evidence": self.gate_evidence.as_json(),
        }


def observation_from_body(body: dict[str, Any]) -> DerivedSessionObservation:
    """Rebuild a V3 observation from its stored body.

    Args:
        body: The stored mapping.

    Returns:
        The observation, with its evidence rebuilt as derived metrics.

    Raises:
        SoakContractError: `SOAK_SESSION_EVIDENCE_INCOMPLETE` when the stored
            record carries no gate evidence. A V3 observation without its
            evidence is not readable as a clean session.
    """
    from .derivations import DerivedMetric, GateScope, MetricStatus
    from .errors import Code, SoakContractError

    stored = body.get("gate_evidence")
    if not isinstance(stored, dict):
        raise SoakContractError(
            Code.SOAK_SESSION_EVIDENCE_INCOMPLETE,
            f"the stored observation for {body.get('session')!r} carries no gate "
            "evidence, so none of its safety gates can be read from it",
        )
    metrics = {
        str(row["gate_id"]): DerivedMetric(
            gate_id=str(row["gate_id"]),
            status=MetricStatus(str(row["status"])),
            value=row["value"],
            derivation_id=str(row["derivation_id"]),
            derivation_version=str(row["derivation_version"]),
            scope=GateScope(str(row["scope"])),
            source_schemas=tuple(row.get("source_schemas", ())),
            source_identities=tuple(row.get("source_identities", ())),
            source_fingerprints=tuple(
                (str(pair[0]), str(pair[1])) for pair in row.get("source_fingerprints", ())
            ),
            session=str(row.get("session", "")),
            protocol_fingerprint=str(row.get("protocol_fingerprint", "")),
            qualifying_commit=str(row.get("qualifying_commit", "")),
            detail=tuple(row.get("detail", ())),
        )
        for row in stored.get("metrics", ())
    }
    evidence = SessionGateEvidence(
        session=str(stored.get("session", "")),
        protocol_fingerprint=str(stored.get("protocol_fingerprint", "")),
        qualifying_commit=str(stored.get("qualifying_commit", "")),
        run_ids=tuple(stored.get("run_ids", ())),
        metrics=metrics,
        source_reads=tuple(stored.get("source_reads", ())),
        evidence_contract_fingerprint=str(stored.get("evidence_contract_fingerprint", "")),
        finalized_at=str(stored.get("finalized_at", "")),
    )
    return DerivedSessionObservation(
        protocol_fingerprint=str(body.get("protocol_fingerprint", "")),
        session=str(body.get("session", "")),
        qualifying_commit=str(body.get("qualifying_commit", "")),
        observed_at=str(body.get("observed_at", "")),
        run_ids=tuple(body.get("run_ids", ())),
        gate_evidence=evidence,
        classification=str(body.get("classification", "QUALIFYING")),
        early_close=bool(body.get("early_close", False)),
        superseded_by=body.get("superseded_by") or None,
    )
