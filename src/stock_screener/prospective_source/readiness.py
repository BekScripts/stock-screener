"""Six-of-six readiness, and the gate that must pass before the next capture.

R1.8 had one of six required inputs and discovered that at the decision instant.
The point of this module is that the discovery happens days earlier, in a
command anyone can run, and that the answer is conjunctive: five of six is not
"nearly ready", it is BLOCKED, because a decision packet needs all six
fingerprints or it is not a decision packet.

`readiness` is the pre-event gate. `preflight` is the lighter decision-day
check. Neither creates a decision, and both refuse to report READY on a source
whose freshness policy it cannot satisfy — the condition that cost September 30.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .envelope import admissible_for

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from .envelope import SnapshotEnvelope

__all__ = [
    "FRESHNESS_POLICY",
    "REQUIRED_INPUTS",
    "ReadinessReport",
    "assess",
    "preflight",
]

#: The six inputs a decision packet needs. All of them, or none.
REQUIRED_INPUTS: tuple[str, ...] = (
    "universe",
    "sec_availability",
    "market",
    "score_inputs",
    "tradability",
    "ranked_cross_section",
)

#: How stale a source may be when a decision is taken, in seconds. Fail-closed
#: and prospective only: §34 forbids applying this to September 30 after the
#: fact, and it is not applied there.
#:
#: The reasoning behind each number is the source's own cadence. A broker asset
#: list and a tradability verdict describe the present and are cheap to refresh,
#: so a day is generous. Prices must reach the decision session itself, which is
#: what the session bound already enforces, so the age allowance only covers the
#: gap between the close and the capture. SEC acceptance is genuinely
#: point-in-time and needs no age bound at all — a filing accepted in 2019 is
#: not stale, it is simply old.
FRESHNESS_POLICY: dict[str, float | None] = {
    "universe": 86_400.0,
    "sec_availability": None,
    "market": 86_400.0,
    "score_inputs": 86_400.0,
    "tradability": 86_400.0,
    "ranked_cross_section": 86_400.0,
}


@dataclass
class ReadinessReport:
    """Whether a future capture can proceed.

    Attributes:
        decision_instant: The instant assessed against.
        inputs: One verdict per required input.
        blockers: Every reason a capture would fail, in order.
    """

    decision_instant: datetime
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)

    @property
    def ready_count(self) -> int:
        """How many of the six required inputs are ready."""
        return sum(1 for row in self.inputs.values() if row["status"] == "READY")

    @property
    def ready(self) -> bool:
        """Whether all six are ready. Five is not nearly ready."""
        return self.ready_count == len(REQUIRED_INPUTS)

    def as_json(self) -> dict[str, Any]:
        """Return the report as a JSON-safe mapping."""
        return {
            "decision_instant": self.decision_instant.isoformat(),
            "required_inputs": list(REQUIRED_INPUTS),
            "inputs": dict(sorted(self.inputs.items())),
            "ready_count": self.ready_count,
            "total_required": len(REQUIRED_INPUTS),
            "six_of_six": self.ready,
            "result": "READY" if self.ready else "BLOCKED",
            "blockers": list(self.blockers),
            "rule": (
                "the six inputs are conjunctive. A decision packet needs all six "
                "fingerprints, so five of six is BLOCKED rather than nearly ready."
            ),
            "freshness_policy_seconds": dict(FRESHNESS_POLICY),
            "freshness_applies_prospectively_only": True,
            "september_30_not_reassessed": True,
        }


def assess(
    envelopes: Mapping[str, SnapshotEnvelope | None],
    *,
    decision_instant: datetime,
    derived: Sequence[str] = ("score_inputs", "tradability", "ranked_cross_section"),
) -> ReadinessReport:
    """Assess readiness for a decision instant.

    Args:
        envelopes: `{input: envelope}`, with None for an input no source
            supplied. A missing key is treated as unavailable rather than
            skipped, because a silently absent input is the R1.8 failure.
        decision_instant: The instant to assess against.
        derived: Inputs produced from the fetched ones rather than fetched
            directly. They are ready when their sources are.

    Returns:
        The report.
    """
    report = ReadinessReport(decision_instant=decision_instant)
    fetched = [name for name in REQUIRED_INPUTS if name not in derived]
    for name in fetched:
        envelope = envelopes.get(name)
        if envelope is None:
            report.inputs[name] = {
                "status": "BLOCKED",
                "reasons": ["SOURCE_UNAVAILABLE"],
                "fingerprint": None,
            }
            report.blockers.append(f"{name}: SOURCE_UNAVAILABLE")
            continue
        verdict = admissible_for(envelope, decision_instant, max_age=FRESHNESS_POLICY.get(name))
        report.inputs[name] = {
            "status": "READY" if verdict["admissible"] else "BLOCKED",
            "reasons": verdict["reasons"],
            "fingerprint": envelope.fingerprint(),
            "semantic_as_of": verdict["semantic_as_of"],
            "retrieved_at": verdict["retrieved_at"],
            "age_seconds": verdict["age_seconds"],
            "record_count": envelope.record_count,
            "completeness": str(envelope.completeness),
        }
        report.blockers.extend(f"{name}: {reason}" for reason in verdict["reasons"])

    sources_ready = all(report.inputs[name]["status"] == "READY" for name in fetched)
    for name in derived:
        if sources_ready:
            report.inputs[name] = {
                "status": "READY",
                "reasons": [],
                "derived_from": fetched,
                "fingerprint": None,
                "note": "derivable once its sources are admissible",
            }
        else:
            report.inputs[name] = {
                "status": "BLOCKED",
                "reasons": ["UPSTREAM_SOURCE_BLOCKED"],
                "derived_from": fetched,
                "fingerprint": None,
            }
            report.blockers.append(f"{name}: UPSTREAM_SOURCE_BLOCKED")
    return report


def preflight(
    *,
    armed: bool,
    now: datetime,
    decision_instant: datetime,
    session_is_trading: bool,
    store_writable: bool,
    adapters_available: Mapping[str, bool],
    bindings_match: bool,
) -> dict[str, Any]:
    """The lighter decision-day check, run before the instant rather than after.

    Args:
        armed: Whether the runner is armed.
        now: The current moment. Must be aware.
        decision_instant: The instant the capture is due.
        session_is_trading: Whether the decision session is a trading session.
        store_writable: Whether the prospective store accepts a write.
        adapters_available: `{adapter: reachable}`.
        bindings_match: Whether every frozen digest still matches.

    Returns:
        The preflight verdict, naming each gate.
    """
    gates = {
        "protocol_armed": armed,
        "bindings_match": bindings_match,
        "calendar_session_is_trading": session_is_trading,
        "snapshot_store_writable": store_writable,
        "adapters_available": all(adapters_available.values()),
    }
    remaining = decision_instant - now
    return {
        "gates": gates,
        "adapters": dict(sorted(adapters_available.items())),
        "time_gate": {
            "now": now.isoformat(),
            "decision_instant": decision_instant.isoformat(),
            "instant_passed": now >= decision_instant,
            "seconds_remaining": max(remaining.total_seconds(), 0.0),
            "capture_permitted_yet": now >= decision_instant,
        },
        "result": "GO" if all(gates.values()) else "NO_GO",
        "failed_gates": [name for name, ok in gates.items() if not ok],
        "creates_a_decision": False,
        "why_before_the_instant": (
            "R1.8 discovered a missing source plane after the instant had passed, "
            "when nothing could be done about it. This check is meant to run with "
            "time left to fix what it finds."
        ),
        "recommended_lead_time_hours": timedelta(hours=24).total_seconds() / 3600,
    }
