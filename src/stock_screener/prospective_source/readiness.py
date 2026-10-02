"""Six-of-six readiness, and the gate that must pass before the next capture.

R1.8 had one of six required inputs and discovered that at the decision instant.
The point of this module is that the discovery happens days earlier, in a
command anyone can run, and that the answer is conjunctive: five of six is not
"nearly ready", it is BLOCKED, because a decision packet needs all six
fingerprints or it is not a decision packet.

`readiness` is the pre-event gate. `preflight` is the lighter decision-day
check. Neither creates a decision, and both refuse to report READY on a source
whose freshness policy it cannot satisfy — the condition that cost September 30.

**What this module does and does not claim.** It answers one question: are the
fetched sources admissible for the instant, and are the derived components
therefore derivable? That is `SOURCE_AND_DERIVABILITY` readiness, and R1.11
showed why the distinction matters — a derived component reported READY here on
the strength of its sources alone, which R1.9 and R1.10 then certified as 6/6
without any producer existing. Those reports were accurate under this
definition; the definition was the problem. Derived components are now reported
as `DERIVABLE_NOT_PRODUCED`, which is not readiness, and the stronger question —
has a producer actually emitted a validated, fingerprinted artifact — belongs to
`prospective_capture.readiness` and its `CAPTURE_READY` state.
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
    "DERIVED_INPUTS",
    "FETCHED_INPUTS",
    "FRESHNESS_POLICY",
    "READINESS_KIND",
    "REQUIRED_INPUTS",
    "ReadinessReport",
    "assess",
    "preflight",
]

#: What a verdict from this module means. Never plain "readiness": see the module
#: docstring and `prospective_capture.readiness`.
READINESS_KIND = "SOURCE_AND_DERIVABILITY"

#: The six inputs a decision packet needs. All of them, or none.
REQUIRED_INPUTS: tuple[str, ...] = (
    "universe",
    "sec_availability",
    "market",
    "score_inputs",
    "tradability",
    "ranked_cross_section",
)

#: The four that are fetched. `tradability` belongs here and not among the
#: derived: the frozen capture sequence lists it as a stage-A current-only
#: source, supplied by the same broker asset list as the universe. Treating it as
#: derived is what let it be reported ready without a source of its own.
FETCHED_INPUTS: tuple[str, ...] = ("universe", "tradability", "market", "sec_availability")

#: The two that a producer must emit. Neither is derivable from the fetched
#: sources alone: no fetched source carries a reported fundamental value, which
#: is why `prospective_capture` adds a filing-observations fetch to feed them.
DERIVED_INPUTS: tuple[str, ...] = ("score_inputs", "ranked_cross_section")

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
        """How many required inputs are READY — fetched sources only."""
        return sum(1 for row in self.inputs.values() if row["status"] == "READY")

    @property
    def derivable_count(self) -> int:
        """How many derived inputs are derivable but unproduced."""
        return sum(1 for row in self.inputs.values() if row["status"] == "DERIVABLE_NOT_PRODUCED")

    @property
    def sources_ready(self) -> bool:
        """Whether every fetched source is admissible. Not capture readiness."""
        return all(self.inputs.get(name, {}).get("status") == "READY" for name in FETCHED_INPUTS)

    @property
    def ready(self) -> bool:
        """Whether every fetched source is admissible and the rest derivable.

        This is deliberately not "ready to capture". A derived input counted here
        has no artifact and no fingerprint; `prospective_capture.readiness`
        answers whether a decision can actually be produced.
        """
        return self.sources_ready and self.derivable_count == len(DERIVED_INPUTS)

    def as_json(self) -> dict[str, Any]:
        """Return the report as a JSON-safe mapping."""
        return {
            "decision_instant": self.decision_instant.isoformat(),
            "required_inputs": list(REQUIRED_INPUTS),
            "inputs": dict(sorted(self.inputs.items())),
            "readiness_kind": READINESS_KIND,
            "fetched_inputs": list(FETCHED_INPUTS),
            "derived_inputs": list(DERIVED_INPUTS),
            "ready_count": self.ready_count,
            "derivable_count": self.derivable_count,
            "total_required": len(REQUIRED_INPUTS),
            "sources_ready": self.sources_ready,
            "result": "SOURCES_READY" if self.ready else "BLOCKED",
            "is_not_capture_readiness": (
                "a derived input here has no artifact and no fingerprint. See "
                "prospective_capture.readiness for CAPTURE_READY."
            ),
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
    derived: Sequence[str] = DERIVED_INPUTS,
) -> ReadinessReport:
    """Assess readiness for a decision instant.

    Args:
        envelopes: `{input: envelope}`, with None for an input no source
            supplied. A missing key is treated as unavailable rather than
            skipped, because a silently absent input is the R1.8 failure.
        decision_instant: The instant to assess against.
        derived: Inputs a producer must emit rather than fetch. They are never
            reported READY here, only derivable, because this module cannot see
            whether a producer exists.

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
                "status": "DERIVABLE_NOT_PRODUCED",
                "reasons": ["ARTIFACT_NOT_PRODUCED"],
                "derived_from": fetched,
                "fingerprint": None,
                "note": (
                    "its sources are admissible, so it is derivable. That is not "
                    "readiness: no producer has run and there is no fingerprint."
                ),
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
