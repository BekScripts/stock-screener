"""Drill evidence that can prove which system it tested.

The six V1 drill artefacts pass, were produced by injected faults, and are
honest about what they did. They are also inadmissible for any future protocol,
and the reason is worth being precise about: their complete field set is
`action, detail, drill_fingerprint, drill_type, expected, external_mutations,
injected_fault, observed, passed, performed_at, post_state, pre_state,
reconciliation, run_id`. Not one of those says *which* control plane was
exercised. No protocol fingerprint, no commit, no engine version, no policy
digest.

So they attest that a kill survived a restart somewhere, at some point, under
some build. That is a useful diagnostic and it is not evidence that *this*
protocol's control plane refuses correctly. A drill that cannot name what it
tested can certify anything, which means it certifies nothing.

`PAPER_OPERATIONS_DRILL_EVIDENCE_V2` binds all of it. The old records are not
retrofitted — adding fields to them would be inventing evidence about runs
nobody made.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .errors import Code, SoakContractError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "DRILL_EVIDENCE_V2_VERSION",
    "REQUIRED_DRILL_TYPES",
    "DrillRecordV2",
    "DrillStoreV2",
    "DrillTypeV2",
    "admissible_drills",
]

#: The drill evidence contract.
DRILL_EVIDENCE_V2_VERSION = "PAPER_OPERATIONS_DRILL_EVIDENCE_V2"


class DrillTypeV2(StrEnum):
    """The six failure paths a soak must exercise.

    Named as V2 constants rather than reusing V1's enum, so a protocol binding
    "the six drill types" binds this set and not whatever a later edit makes of
    the old one.
    """

    PERSISTENT_KILL_RESTART = "PERSISTENT_KILL_RESTART"
    DUPLICATE_SCHEDULER = "DUPLICATE_SCHEDULER"
    CRASH_RECOVERY = "CRASH_RECOVERY"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    STALE_INPUT = "STALE_INPUT"


#: Every drill a protocol may require. A protocol binds this set by value, so
#: adding a seventh type later cannot retroactively change what an armed
#: protocol required.
REQUIRED_DRILL_TYPES: tuple[DrillTypeV2, ...] = tuple(DrillTypeV2)


@dataclass(frozen=True)
class DrillRecordV2:
    """One drill, bound to the exact system it exercised.

    Attributes:
        drill_type: Which failure path.
        protocol_fingerprint: Which protocol this certifies. A drill for one
            protocol never satisfies another.
        qualifying_commit: Which build was exercised.
        operations_engine_version: Which control plane. An engine change means
            old drills stop certifying it.
        operations_policy_fingerprint: Which operational rules were in force.
        kill_policy_fingerprint: The control-state policy, for drills that
            exercise it.
        runtime_binding_fingerprint: Which deployed runtime, where relevant.
        provider_mode: `PAPER` or `INJECTED_FAULT`. Never claims a real outage.
        injected_fault: Whether the fault was simulated. Stated rather than
            implied, because an injected outage and a real one are different
            evidence and only the first is safe to arrange deliberately.
        performed_at: When.
        pre_state_fingerprint: What was true before.
        action: What was done.
        expected: What should have happened.
        observed: What did.
        post_state_fingerprint: What was true afterwards.
        external_mutations: How many mutating requests it caused.
        passed: Whether observed matched expected.
        detail: A sanitised note.
    """

    drill_type: DrillTypeV2
    protocol_fingerprint: str
    qualifying_commit: str
    operations_engine_version: str
    operations_policy_fingerprint: str
    performed_at: str
    pre_state_fingerprint: str
    action: str
    expected: str
    observed: str
    post_state_fingerprint: str
    passed: bool
    kill_policy_fingerprint: str = ""
    runtime_binding_fingerprint: str = ""
    provider_mode: str = "INJECTED_FAULT"
    injected_fault: bool = True
    external_mutations: int = 0
    detail: str = ""

    def __post_init__(self) -> None:
        """Refuse a record that could not establish what it tested.

        Raises:
            SoakContractError: `SOAK_DRILL_INADMISSIBLE` if any binding a
                reader needs to know *which* system was exercised is absent.
                Refused at construction, so an unbound record cannot reach a
                store and later be mistaken for evidence.
        """
        required = {
            "protocol_fingerprint": self.protocol_fingerprint,
            "qualifying_commit": self.qualifying_commit,
            "operations_engine_version": self.operations_engine_version,
            "operations_policy_fingerprint": self.operations_policy_fingerprint,
        }
        missing = sorted(name for name, value in required.items() if not str(value).strip())
        if missing:
            raise SoakContractError(
                Code.SOAK_DRILL_INADMISSIBLE,
                f"a drill record must bind {missing}. Without them it attests that "
                "something refused correctly somewhere, under some build — which can "
                "certify anything and therefore certifies nothing",
            )

    def body(self) -> dict[str, Any]:
        """Return the record's semantic content. Carries no credential."""
        return {
            "version": DRILL_EVIDENCE_V2_VERSION,
            "drill_type": str(self.drill_type),
            "protocol_fingerprint": self.protocol_fingerprint,
            "qualifying_commit": self.qualifying_commit,
            "operations_engine_version": self.operations_engine_version,
            "operations_policy_fingerprint": self.operations_policy_fingerprint,
            "kill_policy_fingerprint": self.kill_policy_fingerprint,
            "runtime_binding_fingerprint": self.runtime_binding_fingerprint,
            "provider_mode": self.provider_mode,
            "injected_fault": self.injected_fault,
            "pre_state_fingerprint": self.pre_state_fingerprint,
            "action": self.action,
            "expected": self.expected,
            "observed": self.observed,
            "post_state_fingerprint": self.post_state_fingerprint,
            "external_mutations": self.external_mutations,
            "passed": self.passed,
            "detail": self.detail,
        }

    def fingerprint(self) -> str:
        """Digest the drill.

        `performed_at` is excluded: it is when the record was made, not part of
        what the drill established, and including it would make an identical
        re-run a different drill.
        """
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    @property
    def drill_id(self) -> str:
        """A derived identity: this protocol, this commit, this drill type.

        Derived rather than content-addressed so a *conflicting* result for one
        drill under one protocol collides and is refused, instead of quietly
        becoming a second record that the scorer might count.
        """
        import hashlib

        digest = hashlib.sha256(
            f"{self.protocol_fingerprint}|{self.qualifying_commit}|{self.drill_type}".encode()
        ).hexdigest()
        return f"{self.drill_type}-{digest[:16]}"

    def as_json(self) -> dict[str, Any]:
        """Return the record with its id, timestamp and fingerprint."""
        return {
            **self.body(),
            "performed_at": self.performed_at,
            "drill_id": self.drill_id,
            "drill_fingerprint": self.fingerprint(),
        }

    def admissible_for(
        self,
        *,
        protocol_fingerprint: str,
        qualifying_commit: str,
        operations_engine_version: str,
        operations_policy_fingerprint: str,
    ) -> tuple[bool, str]:
        """Whether this record certifies a given protocol and build.

        Args:
            protocol_fingerprint: The protocol asking.
            qualifying_commit: Its build.
            operations_engine_version: Its control plane.
            operations_policy_fingerprint: Its operational rules.

        Returns:
            Whether it is admissible, and why not when it is not.
        """
        for label, mine, theirs in (
            ("protocol", self.protocol_fingerprint, protocol_fingerprint),
            ("qualifying commit", self.qualifying_commit, qualifying_commit),
            ("operations engine", self.operations_engine_version, operations_engine_version),
            (
                "operations policy",
                self.operations_policy_fingerprint,
                operations_policy_fingerprint,
            ),
        ):
            if mine != theirs:
                return (
                    False,
                    f"{label} differs: the drill was performed against "
                    f"{str(mine)[:16]}… and this protocol is {str(theirs)[:16]}…",
                )
        return (True, "")


@dataclass
class DrillStoreV2:
    """Where bound drill evidence lives, under a protocol's own namespace.

    Attributes:
        directory: Where records are written.
    """

    directory: Path

    def __post_init__(self) -> None:
        """Resolve the directory."""
        from pathlib import Path as _Path

        self.directory = _Path(self.directory)

    def record(self, drill: DrillRecordV2) -> DrillRecordV2:
        """Write one drill, refusing a conflicting result for the same identity.

        Args:
            drill: The record.

        Returns:
            The record.

        Raises:
            SoakContractError: `SOAK_DRILL_CONFLICT` if a record already exists
                for this protocol, commit and drill type with different
                content. Recording the identical result again is a no-op, so a
                repeated drill run is safe; a *different* result for the same
                drill is two incompatible claims about one system and neither
                can be trusted.
        """
        from stock_screener.paper_operations import atomic_write_verified

        path = self.directory / f"{drill.drill_id}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("drill_fingerprint") != drill.fingerprint():
                raise SoakContractError(
                    Code.SOAK_DRILL_CONFLICT,
                    f"{drill.drill_type} already has a different result recorded for "
                    f"this protocol and commit ({existing.get('drill_fingerprint', '')[:16]}… "
                    f"vs {drill.fingerprint()[:16]}…). Two incompatible claims about one "
                    "system means neither is evidence; investigate rather than overwrite",
                )
            return drill
        atomic_write_verified(path, drill.as_json())
        return drill

    def all(self) -> tuple[DrillRecordV2, ...]:
        """Every drill recorded here, by type."""
        if not self.directory.exists():
            return ()
        rows = [
            _from_body(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(self.directory.glob("*.json"))
        ]
        return tuple(sorted(rows, key=lambda row: str(row.drill_type)))


def _from_body(body: dict[str, Any]) -> DrillRecordV2:
    """Rebuild a drill record from its stored form.

    Args:
        body: What was written.

    Returns:
        The record.

    Raises:
        SoakContractError: `SOAK_DRILL_INADMISSIBLE` if the record is not this
            contract version. A V1 record read as a V2 one would be an unbound
            record wearing a bound record's type.
    """
    if str(body.get("version")) != DRILL_EVIDENCE_V2_VERSION:
        raise SoakContractError(
            Code.SOAK_DRILL_INADMISSIBLE,
            f"drill record is {body.get('version')!r}, expected "
            f"{DRILL_EVIDENCE_V2_VERSION!r}. A V1 record carries no protocol, commit "
            "or engine binding and is not retrofitted into one",
        )
    return DrillRecordV2(
        drill_type=DrillTypeV2(str(body["drill_type"])),
        protocol_fingerprint=str(body["protocol_fingerprint"]),
        qualifying_commit=str(body["qualifying_commit"]),
        operations_engine_version=str(body["operations_engine_version"]),
        operations_policy_fingerprint=str(body["operations_policy_fingerprint"]),
        performed_at=str(body.get("performed_at", "")),
        pre_state_fingerprint=str(body.get("pre_state_fingerprint", "")),
        action=str(body.get("action", "")),
        expected=str(body.get("expected", "")),
        observed=str(body.get("observed", "")),
        post_state_fingerprint=str(body.get("post_state_fingerprint", "")),
        passed=bool(body.get("passed", False)),
        kill_policy_fingerprint=str(body.get("kill_policy_fingerprint", "")),
        runtime_binding_fingerprint=str(body.get("runtime_binding_fingerprint", "")),
        provider_mode=str(body.get("provider_mode", "INJECTED_FAULT")),
        injected_fault=bool(body.get("injected_fault", True)),
        external_mutations=int(body.get("external_mutations", 0)),
        detail=str(body.get("detail", "")),
    )


def admissible_drills(
    records: tuple[DrillRecordV2, ...],
    *,
    protocol_fingerprint: str,
    qualifying_commit: str,
    operations_engine_version: str,
    operations_policy_fingerprint: str,
) -> tuple[tuple[DrillRecordV2, ...], tuple[str, ...]]:
    """Filter drill records to those that certify this protocol and build.

    Args:
        records: Everything recorded.
        protocol_fingerprint: The protocol asking.
        qualifying_commit: Its build.
        operations_engine_version: Its control plane.
        operations_policy_fingerprint: Its operational rules.

    Returns:
        The admissible records, and a reason for each one excluded. The reasons
        are returned rather than dropped so a scorer can say *why* a drill gate
        failed, which is the difference between "run the drills" and "your
        drills are for the wrong commit".
    """
    admitted: list[DrillRecordV2] = []
    rejected: list[str] = []
    for row in records:
        ok, why = row.admissible_for(
            protocol_fingerprint=protocol_fingerprint,
            qualifying_commit=qualifying_commit,
            operations_engine_version=operations_engine_version,
            operations_policy_fingerprint=operations_policy_fingerprint,
        )
        if ok:
            admitted.append(row)
        else:
            rejected.append(f"{row.drill_type}: {why}")
    return (tuple(admitted), tuple(rejected))
