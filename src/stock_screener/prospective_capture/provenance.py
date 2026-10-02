"""Proving every fundamental value traces to a filing that was public at T.

The prospective plane's whole claim is that a decision used only what was
knowable at its instant. For prices that claim is cheap — a bar belongs to a
session. For reported fundamentals it is not, because a company's figures are
restated, and the convenient way to read them is the one that destroys the
evidence: a merged statement resolves each period to its most recently filed
value and cannot say which filing supplied it.

So the claim is audited rather than asserted. Every admitted observation must
carry an accession, that accession must appear in the acceptance snapshot, and
its acceptance timestamp must not postdate the instant. An observation failing
any of those is untraceable and must not have reached a metric.

`audit` is deliberately computed from the artifacts themselves rather than from
the fetcher's own counters. A fetcher that filtered incorrectly would report
having filtered correctly; the artifacts cannot.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .score_inputs import ScoreInputSet

log = structlog.get_logger(__name__)

__all__ = [
    "PROVENANCE_AUDIT_ID",
    "ProvenanceViolationError",
    "audit",
]

#: The audit's identity, recorded in the artifact.
PROVENANCE_AUDIT_ID = "PROSPECTIVE_FUNDAMENTAL_PROVENANCE_V1"


class ProvenanceViolationError(RuntimeError):
    """Raised when a fundamental value cannot prove it was public at the instant."""


def audit(
    inputs: ScoreInputSet,
    *,
    observation_records: Sequence[Mapping[str, Any]],
    acceptance_records: Sequence[Mapping[str, Any]],
    decision_instant: datetime,
    source_operation: str,
    merged_operations_used: Sequence[str] = (),
    strict: bool = True,
) -> dict[str, Any]:
    """Audit the provenance of every fundamental value behind a score input set.

    Args:
        inputs: The score-input artifact whose provenance is being proven.
        observation_records: The admitted observations the inputs were built from.
        acceptance_records: The acceptance snapshot, which dates each accession.
        decision_instant: The cutoff. Must be aware.
        source_operation: The operation the observations came from.
        merged_operations_used: Any merged or current-statement operation invoked.
            Non-empty is a violation under `strict`.
        strict: Raise on violation rather than only reporting.

    Returns:
        The audit, with a count for every way a value could fail to be traceable.

    Raises:
        ProvenanceViolationError: Under `strict`, if any value is untraceable,
            postdates the instant, or came from a merged statement.
    """
    accepted_at: dict[str, datetime] = {}
    for filing in acceptance_records:
        accession, stamp = filing.get("accession"), filing.get("accepted_at")
        if accession and stamp:
            accepted_at[str(accession)] = datetime.fromisoformat(str(stamp))

    untraceable: list[dict[str, Any]] = []
    post_cutoff: list[dict[str, Any]] = []
    seen: set[str] = set()
    stamps: list[datetime] = []
    for row in observation_records:
        accession = row.get("accession")
        if not accession:
            untraceable.append({"reason": "NO_ACCESSION", "concept": row.get("concept")})
            continue
        accession = str(accession)
        seen.add(accession)
        if not row.get("filed_date"):
            untraceable.append({"reason": "NO_FILED_DATE", "accession": accession})
            continue
        moment = accepted_at.get(accession)
        if moment is None:
            untraceable.append(
                {"reason": "ACCESSION_NOT_IN_ACCEPTANCE_SNAPSHOT", "accession": accession}
            )
            continue
        stamps.append(moment)
        if moment > decision_instant:
            post_cutoff.append({"accession": accession, "accepted_at": moment.isoformat()})

    # Provenance is complete for an identity when every accession it used is
    # present, dated, and dated at or before the instant.
    complete = 0
    incomplete: list[dict[str, Any]] = []
    for entry in inputs.inputs:
        if entry.metrics is None:
            continue
        accessions = [str(a) for a in entry.provenance.get("accessions") or ()]
        dated = [a for a in accessions if a in accepted_at]
        late = [a for a in dated if accepted_at[a] > decision_instant]
        if accessions and len(dated) == len(accessions) and not late:
            complete += 1
        else:
            incomplete.append(
                {
                    "kind": entry.kind,
                    "identity_id": entry.identity_id,
                    "accessions": len(accessions),
                    "dated": len(dated),
                    "late": len(late),
                }
            )

    scorable = inputs.scorable
    report = {
        "audit_id": PROVENANCE_AUDIT_ID,
        "producer": "prospective_capture.score_inputs.build_score_inputs",
        "source_operation": source_operation,
        "temporal_class": "SOURCE_HAS_EVENT_TIME",
        "decision_instant": decision_instant.isoformat(),
        "as_of": inputs.decision_instant,
        "observation_count": len(observation_records),
        "unique_accessions": len(seen),
        "accessions_dated_by_acceptance_snapshot": len(seen & set(accepted_at)),
        "min_accepted_at": min(stamps).isoformat() if stamps else None,
        "max_accepted_at": max(stamps).isoformat() if stamps else None,
        "post_cutoff_observations_rejected": len(post_cutoff),
        "post_cutoff_detail": post_cutoff[:10],
        "untraceable_values": len(untraceable),
        "untraceable_detail": untraceable[:10],
        "merged_current_operations_used": list(merged_operations_used),
        "score_inputs": len(inputs.inputs),
        "scorable_score_inputs": scorable,
        "score_inputs_with_complete_accession_provenance": complete,
        "score_inputs_with_incomplete_provenance": incomplete,
        "accession_scoped": True,
        "acceptance_time_enforced": True,
        "computed_from_artifacts_not_fetcher_counters": True,
    }
    violations = {
        "untraceable_values": len(untraceable),
        "post_cutoff_observations": len(post_cutoff),
        "merged_current_operations": len(merged_operations_used),
        "score_inputs_missing_provenance": len(incomplete),
    }
    report["violations"] = violations
    report["clean"] = not any(violations.values())
    if strict and not report["clean"]:
        raise ProvenanceViolationError(
            f"fundamental provenance is not complete: {violations}. A value that "
            "cannot prove it was public at the decision instant must not reach a "
            "metric."
        )
    log.info(
        "prospective.provenance",
        observations=len(observation_records),
        accessions=len(seen),
        complete=complete,
        clean=report["clean"],
    )
    return report
