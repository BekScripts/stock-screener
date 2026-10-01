"""The snapshot envelope, and the invariant R1.8 was lost for want of.

Every prospective source payload is wrapped in an envelope carrying two
timestamps that are easy to conflate and must never be: `retrieved_at`, when the
bytes arrived, and `semantic_as_of`, the latest moment any fact inside them was
knowable. R1.8 failed because the newest obtainable data was five weeks older
than the decision instant, and the only honest response was to refuse. The
dishonest response — stamping late-fetched or stale data with the decision's
as-of — is what `admissible_for` exists to make impossible.

The rule is one-directional and deliberately unforgiving. A payload retrieved
after a decision instant is admissible when every fact in it was knowable by
that instant; a payload whose contents postdate the instant never is, however it
was obtained. Lateness of retrieval is fine. Lateness of information is not.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003 - used in a runtime isinstance-style guard
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Completeness",
    "SnapshotEnvelope",
    "StaleSourceError",
    "TemporalClass",
    "admissible_for",
    "content_digest",
    "request_fingerprint",
]


class Completeness(StrEnum):
    """What a source managed to supply. `COMPLETE` is the only usable state."""

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNAVAILABLE = "UNAVAILABLE"
    AMBIGUOUS = "AMBIGUOUS"


class TemporalClass(StrEnum):
    """What a source can say about a moment other than now.

    The distinction decides whether a snapshot may be taken after the decision
    instant, and it is the thing R1.10 exists to close. A source carrying event
    times can be read whenever, because each fact says when it became knowable.
    A session-bounded source can be read after the close, because the session is
    the fact. A source that only describes the present can say nothing about an
    earlier instant at all — so it must be read *before* the instant, or its
    snapshot is evidence about the wrong moment.
    """

    #: Every fact carries its own availability timestamp. SEC acceptance.
    EVENT_TIME = "SOURCE_HAS_EVENT_TIME"

    #: Facts belong to a named session. A completed daily bar.
    SESSION_BOUNDED = "SESSION_BOUNDED"

    #: The payload describes the present and nothing else. A broker asset list.
    CURRENT_AT_RETRIEVAL = "CURRENT_AT_RETRIEVAL_ONLY"


class StaleSourceError(RuntimeError):
    """Raised when a snapshot cannot honestly serve a decision instant."""


def content_digest(payload: Any) -> str:
    """Digest a payload by its canonical JSON.

    Args:
        payload: Any JSON-serialisable structure.

    Returns:
        The SHA-256 of its canonical serialisation.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def request_fingerprint(source: str, operation: str, parameters: Mapping[str, Any]) -> str:
    """Digest what was asked of a source, so the request is reproducible.

    Args:
        source: The provider's name.
        operation: The operation invoked.
        parameters: The arguments it was called with.

    Returns:
        A digest over the three together.
    """
    return content_digest(
        {"source": source, "operation": operation, "parameters": dict(parameters)}
    )


@dataclass(frozen=True)
class SnapshotEnvelope:
    """One prospective source payload, with everything needed to trust it later.

    Attributes:
        source_type: What kind of input this is — universe, sec, market and so on.
        source_name: The provider.
        source_version: The API or endpoint version.
        operation: The operation invoked.
        retrieved_at: When the bytes arrived. Aware.
        semantic_as_of: The latest moment any fact inside was knowable. Aware.
        decision_session: The session this snapshot is for, where applicable.
        request: The request fingerprint.
        content_sha256: The payload's digest.
        normalizer_version: Which normalisation produced the records.
        record_count: How many records the payload yielded.
        min_source_timestamp: Earliest fact timestamp, where the source gives one.
        max_source_timestamp: Latest fact timestamp. This is what bounds the snapshot.
        completeness: Whether the source delivered what was asked.
        temporal_class: What the source can say about a moment other than now.
            The default is the most restrictive, so a source that forgets to
            declare itself is treated as current-only rather than trusted.
        effective_at: A provider-supplied timestamp establishing when the state
            described was in force. Only this can let a current-only source be
            read after the instant, and only when the provider supplies it.
        ambiguity: Anything the source could not disambiguate.
        notes: Anything else worth recording.
    """

    source_type: str
    source_name: str
    source_version: str
    operation: str
    retrieved_at: datetime
    semantic_as_of: datetime
    content_sha256: str
    request: str
    normalizer_version: str
    record_count: int
    completeness: Completeness
    temporal_class: TemporalClass = TemporalClass.CURRENT_AT_RETRIEVAL
    effective_at: datetime | None = None
    decision_session: str | None = None
    min_source_timestamp: str | None = None
    max_source_timestamp: str | None = None
    ambiguity: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Refuse a naive timestamp, which cannot be compared to an instant."""
        for name in ("retrieved_at", "semantic_as_of"):
            moment = getattr(self, name)
            if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
                raise StaleSourceError(
                    f"{name} must be timezone-aware; a naive timestamp cannot be "
                    "compared to a decision instant"
                )

    @property
    def retrieved_after_semantic_time(self) -> bool:
        """Whether the bytes arrived after the facts were knowable.

        This is the ordinary case for a snapshot taken after a session's close,
        and it is not a problem. It is recorded so nobody later assumes the two
        timestamps are the same.
        """
        return self.retrieved_at > self.semantic_as_of

    def as_json(self) -> dict[str, Any]:
        """Return the envelope as a JSON-safe mapping."""
        return {
            "source_type": self.source_type,
            "source_name": self.source_name,
            "source_version": self.source_version,
            "operation": self.operation,
            "retrieved_at": self.retrieved_at.isoformat(),
            "semantic_as_of": self.semantic_as_of.isoformat(),
            "retrieved_after_semantic_time": self.retrieved_after_semantic_time,
            "decision_session": self.decision_session,
            "request_fingerprint": self.request,
            "content_sha256": self.content_sha256,
            "normalizer_version": self.normalizer_version,
            "record_count": self.record_count,
            "min_source_timestamp": self.min_source_timestamp,
            "max_source_timestamp": self.max_source_timestamp,
            "completeness": str(self.completeness),
            "temporal_class": str(self.temporal_class),
            "effective_at": None if self.effective_at is None else self.effective_at.isoformat(),
            "ambiguity": list(self.ambiguity),
            "notes": list(self.notes),
        }

    def fingerprint(self) -> str:
        """Digest the envelope itself, for the decision packet."""
        return content_digest(self.as_json())


def admissible_for(
    envelope: SnapshotEnvelope, decision_instant: datetime, *, max_age: float | None = None
) -> dict[str, Any]:
    """Decide whether a snapshot may serve a decision, and say why not.

    Three things are checked, in the order that matters. The snapshot must be
    complete, because a partial universe is a different universe. Its facts must
    not postdate the instant, which is the leakage rule. And where a freshness
    policy applies, its facts must not be so much older than the instant that
    they describe a different world — the condition R1.8 failed on.

    Args:
        envelope: The snapshot.
        decision_instant: The moment the decision is taken. Must be aware.
        max_age: The greatest permitted gap, in seconds, between the snapshot's
            semantic time and the instant. None means no freshness requirement,
            which is correct for a source that is genuinely point-in-time.

    Returns:
        The verdict, with a reason for every failure.

    Raises:
        StaleSourceError: If the decision instant is naive.
    """
    if decision_instant.tzinfo is None:
        raise StaleSourceError("the decision instant must be timezone-aware")
    reasons: list[str] = []
    if envelope.completeness is not Completeness.COMPLETE:
        reasons.append(f"SOURCE_{envelope.completeness}")
    if envelope.semantic_as_of > decision_instant:
        reasons.append("SEMANTIC_AS_OF_AFTER_DECISION_INSTANT")

    # A current-only source describes the present and nothing else, so reading it
    # after the instant produces evidence about the wrong moment. Retrieval time
    # is therefore load-bearing for this class and only this class: an event-time
    # or session-bounded payload may be fetched whenever, because each fact
    # already says when it was true.
    #
    # The single escape is a provider-supplied effective time. It must come from
    # the provider — an effective time we assign ourselves would just be the
    # retrojection this rule exists to forbid, wearing a field name.
    late_current_only = False
    if envelope.temporal_class is TemporalClass.CURRENT_AT_RETRIEVAL:
        if envelope.effective_at is not None:
            if envelope.effective_at > decision_instant:
                reasons.append("PROVIDER_EFFECTIVE_TIME_AFTER_DECISION_INSTANT")
        elif envelope.retrieved_at > decision_instant:
            late_current_only = True
            reasons.append("CURRENT_ONLY_SOURCE_TOO_LATE")

    age = (decision_instant - envelope.semantic_as_of).total_seconds()
    if max_age is not None and age > max_age:
        reasons.append("SOURCE_TOO_STALE_FOR_DECISION")
    return {
        "source_type": envelope.source_type,
        "temporal_class": str(envelope.temporal_class),
        "admissible": not reasons,
        "reasons": reasons,
        "semantic_as_of": envelope.semantic_as_of.isoformat(),
        "decision_instant": decision_instant.isoformat(),
        "age_seconds": age,
        "max_age_seconds": max_age,
        "retrieved_at": envelope.retrieved_at.isoformat(),
        "retrieved_after_semantic_time": envelope.retrieved_after_semantic_time,
        "effective_at": (
            None if envelope.effective_at is None else envelope.effective_at.isoformat()
        ),
        "retrieval_time_is_load_bearing": (
            envelope.temporal_class is TemporalClass.CURRENT_AT_RETRIEVAL
            and envelope.effective_at is None
        ),
        "current_only_read_too_late": late_current_only,
        "rule": (
            "late retrieval is permitted; late information is not. A payload whose "
            "facts postdate the instant is never admissible, however it was "
            "obtained. A current-only payload must additionally be retrieved at or "
            "before the instant, because it describes the present and can say "
            "nothing about an earlier moment — unless the provider itself supplies "
            "an effective time that does."
        ),
    }
