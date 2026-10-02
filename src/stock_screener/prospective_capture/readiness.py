"""What readiness means once a capture executor exists.

R1.11 certified an armed window as 6/6 READY and then found no way to produce a
decision packet. The reason was not a lie in the report; it was a definition.
`prospective_source.readiness` marked three of the six required inputs READY
whenever their upstream sources were admissible, recording a null fingerprint
and the note "derivable once its sources are admissible". That is a claim about
derivability, and derivability is not capability.

This module replaces that definition for capture purposes. A component is
`DERIVED_READY` only when a producer has actually run and emitted a validated,
fingerprinted artifact; `SOURCE_READY` is reserved for a fetched source that is
temporally admissible. Nothing reaches `CAPTURE_READY` on a promise.

The older, weaker reading is not rewritten or disowned — R1.9 and R1.10 reported
honestly against the definition they had, and `SOURCE_AND_DERIVABILITY` names it
so the two can be told apart rather than conflated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from stock_screener.prospective_source import REQUIRED_INPUTS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "DERIVED_COMPONENTS",
    "READINESS_KIND",
    "SOURCE_COMPONENTS",
    "CaptureReadiness",
    "ComponentState",
    "ReadinessKind",
    "assess_capture_readiness",
    "require_six_fingerprints",
]

#: What this module's verdicts mean, as against the weaker earlier reading.
READINESS_KIND = "EXECUTABLE_CAPTURE_READINESS"


class ReadinessKind(StrEnum):
    """Which definition of readiness a report was produced under."""

    SOURCE_AND_DERIVABILITY = "SOURCE_AND_DERIVABILITY"
    """R1.9/R1.10: sources admissible, derived components merely derivable."""

    EXECUTABLE_CAPTURE_READINESS = "EXECUTABLE_CAPTURE_READINESS"
    """R1.12 onward: every required artifact actually produced and fingerprinted."""


class ComponentState(StrEnum):
    """Where one required decision component stands."""

    SOURCE_READY = "SOURCE_READY"
    """A fetched source, retrieved and temporally admissible for the instant."""

    DERIVED_READY = "DERIVED_READY"
    """A producer ran, emitted the artifact, and it validated with a fingerprint."""

    BLOCKED = "BLOCKED"
    """Anything else. A component that is merely derivable is BLOCKED."""


#: The four fetched sources. `tradability` belongs here, not among the derived:
#: the frozen capture sequence lists it as a stage-A current-only source, and the
#: broker asset list is what supplies it.
SOURCE_COMPONENTS: tuple[str, ...] = ("universe", "tradability", "market", "sec_availability")

#: The two components a producer must actually emit.
DERIVED_COMPONENTS: tuple[str, ...] = ("score_inputs", "ranked_cross_section")


@dataclass(frozen=True)
class Component:
    """One required component's verdict.

    Attributes:
        name: The component.
        state: Where it stands.
        fingerprint: Its artifact digest. None is never `*_READY`.
        reasons: Why it is blocked, where it is.
        produced_by: The producer that emitted it, for a derived component.
        validated: Whether the artifact passed its contract check.
    """

    name: str
    state: ComponentState
    fingerprint: str | None = None
    reasons: tuple[str, ...] = ()
    produced_by: str | None = None
    validated: bool = False

    @property
    def ready(self) -> bool:
        """Whether this component may take part in a decision."""
        return self.state in (ComponentState.SOURCE_READY, ComponentState.DERIVED_READY)

    def as_json(self) -> dict[str, Any]:
        """Return the verdict as a JSON-safe mapping."""
        return {
            "name": self.name,
            "state": str(self.state),
            "fingerprint": self.fingerprint,
            "reasons": list(self.reasons),
            "produced_by": self.produced_by,
            "validated": self.validated,
            "ready": self.ready,
        }


class IncompleteDecisionInputsError(RuntimeError):
    """Raised when the six-fingerprint gate refuses to let a decision proceed."""


@dataclass
class CaptureReadiness:
    """Whether a capture can actually run, rather than whether it could in principle.

    Attributes:
        decision_instant: The instant assessed against.
        components: One verdict per required component.
        executors: Which stage executors exist, by stage.
        store_healthy: Whether the decision store accepts and returns a write.
        calendar_authoritative: Whether the calendar binding matched.
        activation_matches: Whether the activation still binds.
        arm_matches: Whether the arm still binds.
    """

    decision_instant: str
    components: dict[str, Component] = field(default_factory=dict)
    executors: dict[str, bool] = field(default_factory=dict)
    store_healthy: bool = False
    calendar_authoritative: bool = False
    activation_matches: bool = False
    arm_matches: bool = False

    @property
    def blockers(self) -> list[str]:
        """Every reason a capture would not proceed, in a stable order."""
        found: list[str] = []
        for name in REQUIRED_INPUTS:
            component = self.components.get(name)
            if component is None:
                found.append(f"{name}: COMPONENT_ABSENT")
                continue
            if not component.ready:
                found.extend(f"{name}: {reason}" for reason in component.reasons or ("BLOCKED",))
            if component.ready and component.fingerprint is None:
                found.append(f"{name}: READY_WITH_NULL_FINGERPRINT")
        found.extend(
            f"executor_missing: {stage}"
            for stage, present in sorted(self.executors.items())
            if not present
        )
        for label, ok in (
            ("snapshot_store_unhealthy", self.store_healthy),
            ("calendar_not_authoritative", self.calendar_authoritative),
            ("activation_mismatch", self.activation_matches),
            ("arm_mismatch", self.arm_matches),
        ):
            if not ok:
                found.append(label)
        return found

    @property
    def fingerprints(self) -> dict[str, str | None]:
        """Each required component's artifact digest."""
        return {
            name: (self.components[name].fingerprint if name in self.components else None)
            for name in REQUIRED_INPUTS
        }

    @property
    def non_null_fingerprints(self) -> int:
        """How many of the six required fingerprints are present."""
        return sum(1 for digest in self.fingerprints.values() if digest)

    @property
    def capture_ready(self) -> bool:
        """Whether every condition for an actual capture holds."""
        return not self.blockers and self.non_null_fingerprints == len(REQUIRED_INPUTS)

    def as_json(self) -> dict[str, Any]:
        """Return the report as a JSON-safe mapping."""
        return {
            "readiness_kind": str(ReadinessKind.EXECUTABLE_CAPTURE_READINESS),
            "decision_instant": self.decision_instant,
            "required_inputs": list(REQUIRED_INPUTS),
            "source_components": list(SOURCE_COMPONENTS),
            "derived_components": list(DERIVED_COMPONENTS),
            "components": {
                name: component.as_json() for name, component in sorted(self.components.items())
            },
            "executors": dict(sorted(self.executors.items())),
            "store_healthy": self.store_healthy,
            "calendar_authoritative": self.calendar_authoritative,
            "activation_matches": self.activation_matches,
            "arm_matches": self.arm_matches,
            "required_fingerprint_count": len(REQUIRED_INPUTS),
            "non_null_fingerprint_count": self.non_null_fingerprints,
            "state": "CAPTURE_READY" if self.capture_ready else "NOT_CAPTURE_READY",
            "blockers": self.blockers,
            "rule": (
                "a derived component reaches DERIVED_READY only once a producer has "
                "emitted and validated its artifact. 'derivable once its sources are "
                "admissible' is BLOCKED, and no component is ready with a null "
                "fingerprint."
            ),
            "supersedes": (
                "the SOURCE_AND_DERIVABILITY reading used by R1.9 and R1.10, whose "
                "reports remain accurate under the definition they were made with"
            ),
        }


def require_six_fingerprints(
    fingerprints: Mapping[str, str | None], *, decision_session: str
) -> dict[str, str]:
    """Refuse to proceed unless all six required fingerprints are present.

    The gate immediately before packet construction. It is deliberately a hard
    failure rather than a warning: a packet carrying five fingerprints is not a
    degraded decision packet, it is not one at all.

    Args:
        fingerprints: `{component: digest}`, with None where nothing was emitted.
        decision_session: The session the fingerprints must belong to, for the
            failure message.

    Returns:
        The six digests, with None eliminated, keyed by component.

    Raises:
        IncompleteDecisionInputsError: If any required component is absent or null.
    """
    missing = [name for name in REQUIRED_INPUTS if not fingerprints.get(name)]
    if missing:
        raise IncompleteDecisionInputsError(
            f"DECISION_INPUT_PACKAGE_INCOMPLETE for {decision_session}: "
            f"{len(REQUIRED_INPUTS) - len(missing)} of {len(REQUIRED_INPUTS)} required "
            f"fingerprints present; missing {sorted(missing)}. No packet is written."
        )
    return {name: str(fingerprints[name]) for name in REQUIRED_INPUTS}


def assess_capture_readiness(
    *,
    decision_instant: str,
    sources: Mapping[str, Mapping[str, Any]],
    derived: Mapping[str, Mapping[str, Any]],
    executors: Mapping[str, bool],
    store_healthy: bool,
    calendar_authoritative: bool,
    activation_matches: bool,
    arm_matches: bool,
    required: Sequence[str] = REQUIRED_INPUTS,
) -> CaptureReadiness:
    """Assess whether a capture can actually run.

    Args:
        decision_instant: The instant, ISO with offset.
        sources: `{component: {"admissible": bool, "fingerprint": str | None,
            "reasons": [...]}}` for each fetched source.
        derived: `{component: {"produced": bool, "validated": bool,
            "fingerprint": str | None, "producer": str, "reasons": [...]}}`.
        executors: `{stage: exists}`.
        store_healthy: Whether the decision store round-trips.
        calendar_authoritative: Whether the calendar binding matched.
        activation_matches: Whether the activation still binds.
        arm_matches: Whether the arm still binds.
        required: The components that must all be ready.

    Returns:
        The report. `capture_ready` is conjunctive over everything.
    """
    report = CaptureReadiness(
        decision_instant=decision_instant,
        executors=dict(executors),
        store_healthy=store_healthy,
        calendar_authoritative=calendar_authoritative,
        activation_matches=activation_matches,
        arm_matches=arm_matches,
    )
    for name in required:
        if name in SOURCE_COMPONENTS:
            row = sources.get(name) or {}
            digest = row.get("fingerprint")
            reasons = tuple(str(reason) for reason in row.get("reasons") or ())
            admissible = bool(row.get("admissible")) and bool(digest)
            if not row:
                reasons = ("SOURCE_UNAVAILABLE",)
            elif bool(row.get("admissible")) and not digest:
                reasons = (*reasons, "SOURCE_FINGERPRINT_MISSING")
            report.components[name] = Component(
                name=name,
                state=ComponentState.SOURCE_READY if admissible else ComponentState.BLOCKED,
                fingerprint=str(digest) if digest else None,
                reasons=() if admissible else (reasons or ("SOURCE_NOT_ADMISSIBLE",)),
                validated=admissible,
            )
            continue
        row = derived.get(name) or {}
        digest = row.get("fingerprint")
        produced, validated = bool(row.get("produced")), bool(row.get("validated"))
        reasons = tuple(str(reason) for reason in row.get("reasons") or ())
        if not row:
            reasons = ("PRODUCER_NOT_IMPLEMENTED",)
        elif not produced:
            reasons = (*reasons, "ARTIFACT_NOT_PRODUCED")
        elif not validated:
            reasons = (*reasons, "ARTIFACT_DID_NOT_VALIDATE")
        elif not digest:
            reasons = (*reasons, "ARTIFACT_FINGERPRINT_MISSING")
        ready = produced and validated and bool(digest)
        report.components[name] = Component(
            name=name,
            state=ComponentState.DERIVED_READY if ready else ComponentState.BLOCKED,
            fingerprint=str(digest) if digest else None,
            reasons=() if ready else reasons,
            produced_by=str(row.get("producer")) if row.get("producer") else None,
            validated=validated,
        )
    return report
