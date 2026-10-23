"""One soak store, parameterised by namespace, for every future protocol.

Three soaks produced three store classes — `SoakStore`, `SoakV2Store`, and the
`SoakV3Store` that was never written because the pattern finally blocked. Each
hardcoded its own directory name and typed its writes to its own protocol
class, so a new protocol version required a new store even when the storage
shape had not changed at all.

`VersionedSoakStore` takes the namespace as data and the artefacts through the
generic contracts. A new soak version is then a new namespace and a new
protocol instance, not a new class.

The namespace is validated rather than trusted. It becomes a directory under
the operations root, so an unchecked one is a path traversal waiting to happen
— and a soak that wrote its evidence outside its own namespace could overwrite
a halted protocol's records, which are the only history of why it halted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .contracts import PaperShadowSoakActivation, PaperShadowSoakProtocol
from .drills_v2 import DrillRecordV2, DrillStoreV2, admissible_drills
from .errors import Code, SoakContractError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "RESERVED_NAMESPACES",
    "SOAK_ROOT_DIRECTORY",
    "SoakEvidenceView",
    "VersionedSoakStore",
    "validate_namespace",
]

#: Where soak evidence lives under the operations root.
SOAK_ROOT_DIRECTORY = "soak"

#: Namespaces belonging to halted historical protocols. A new soak may not be
#: created in one: its writes would land among the only records explaining why
#: that protocol halted. V1 occupies the soak root itself, which is why the
#: empty namespace is reserved rather than merely invalid.
RESERVED_NAMESPACES: frozenset[str] = frozenset({"", "v1", "v2", "drills", "observations"})


def validate_namespace(namespace: str) -> str:
    """Check a namespace can only ever name a directory inside the soak root.

    Args:
        namespace: The candidate.

    Returns:
        The namespace, unchanged.

    Raises:
        SoakContractError: `SOAK_NAMESPACE_INVALID` for anything empty,
            absolute, containing a separator or a parent reference, or
            colliding with a historical protocol's namespace.
    """
    candidate = namespace.strip()
    if not candidate:
        raise SoakContractError(
            Code.SOAK_NAMESPACE_INVALID,
            "a soak namespace must be named explicitly; the empty namespace is the "
            "V1 protocol's own location",
        )
    if candidate != namespace:
        raise SoakContractError(
            Code.SOAK_NAMESPACE_INVALID,
            f"{namespace!r} has leading or trailing whitespace, which would make two "
            "namespaces that look identical resolve differently",
        )
    forbidden = ("/", "\\", "..", ":", "\0")
    hit = [row for row in forbidden if row in candidate]
    if hit:
        raise SoakContractError(
            Code.SOAK_NAMESPACE_INVALID,
            f"{namespace!r} contains {hit}. A namespace names one directory under the "
            "soak root and nothing else: a traversal here would let a new soak write "
            "over a halted protocol's evidence, which is the only record of why it "
            "halted",
        )
    if candidate.startswith((".", "-")):
        raise SoakContractError(
            Code.SOAK_NAMESPACE_INVALID,
            f"{namespace!r} begins with {candidate[0]!r}, which is reserved",
        )
    if candidate.lower() in RESERVED_NAMESPACES:
        raise SoakContractError(
            Code.SOAK_NAMESPACE_INVALID,
            f"{namespace!r} is a historical protocol's namespace. A repaired soak gets "
            "a new one; it never reuses the namespace of a protocol that halted",
        )
    return candidate


@dataclass
class VersionedSoakStore:
    """Soak evidence for one protocol instance, in its own namespace.

    Attributes:
        root: The operations root.
        namespace: This soak's directory under the soak root, validated.
    """

    root: Path
    namespace: str

    def __post_init__(self) -> None:
        """Guard the root, validate the namespace and resolve the paths.

        Raises:
            OperationsError: `FORBIDDEN_OPERATIONS_NAMESPACE` if the root lies
                somewhere operational state must never be written.
            SoakContractError: `SOAK_NAMESPACE_INVALID` for an unsafe namespace.
        """
        from pathlib import Path as _Path

        from stock_screener.paper_operations import guard_operations_root

        self.root = _Path(self.root)
        guard_operations_root(self.root)
        self.namespace = validate_namespace(self.namespace)
        self.directory = self.root / SOAK_ROOT_DIRECTORY / self.namespace
        self.protocol_path = self.directory / "protocol.json"
        self.activation_path = self.directory / "activation.json"
        self.halt_path = self.directory / "halt.json"
        self.observations_directory = self.directory / "observations"
        self.drills = DrillStoreV2(self.directory / "drills")

    # -- protocol ---------------------------------------------------------

    def freeze_protocol(self, protocol: PaperShadowSoakProtocol) -> Path:
        """Write the protocol once, refusing to replace a different one.

        Args:
            protocol: The rules. The generic contract, not a version class.

        Returns:
            Where it was written.

        Raises:
            SoakContractError: `SOAK_PROTOCOL_ALREADY_FROZEN` for a different
                protocol. Re-freezing the identical one is a no-op, so a
                retried arming is safe.
        """
        from stock_screener.paper_operations import atomic_write_verified

        existing = self.protocol()
        if existing is not None and existing.fingerprint() != protocol.fingerprint():
            raise SoakContractError(
                Code.SOAK_PROTOCOL_ALREADY_FROZEN,
                f"namespace {self.namespace!r} already holds protocol "
                f"{existing.fingerprint()[:16]}… and this one is "
                f"{protocol.fingerprint()[:16]}…. The rules are frozen before the first "
                "observation and never edited; a changed protocol is a new instance in "
                "a new namespace",
            )
        atomic_write_verified(self.protocol_path, protocol.as_json())
        return self.protocol_path

    def protocol(self) -> PaperShadowSoakProtocol | None:
        """Read the frozen protocol, or `None`."""
        if not self.protocol_path.exists():
            return None
        return PaperShadowSoakProtocol.from_body(
            json.loads(self.protocol_path.read_text(encoding="utf-8"))
        )

    # -- activation -------------------------------------------------------

    def activate(self, activation: PaperShadowSoakActivation) -> Path:
        """Record activation once, against an already-frozen matching protocol.

        Args:
            activation: The activation. The generic contract.

        Returns:
            Where it was written.

        Raises:
            SoakContractError: `SOAK_ACTIVATION_ORPHANED` without a frozen
                protocol — an activation naming a protocol nobody froze binds
                nothing; `SOAK_ACTIVATION_VERSION_MISMATCH` if the two
                disagree; `SOAK_ALREADY_ACTIVATED` for a second, different
                activation, because the prospective boundary is set once.
        """
        from stock_screener.paper_operations import atomic_write_verified

        protocol = self.protocol()
        if protocol is None:
            raise SoakContractError(
                Code.SOAK_ACTIVATION_ORPHANED,
                f"namespace {self.namespace!r} holds no frozen protocol, so there is "
                "nothing for this activation to bind. Freeze the protocol first",
            )
        activation.require_matches(protocol)
        existing = self.activation()
        if existing is not None and existing.fingerprint() != activation.fingerprint():
            raise SoakContractError(
                Code.SOAK_ALREADY_ACTIVATED,
                f"this soak was already activated at {existing.activated_at}. The "
                "prospective boundary is set once; moving it would let a session that "
                "has already closed become eligible",
            )
        atomic_write_verified(self.activation_path, activation.as_json())
        return self.activation_path

    def activation(self) -> PaperShadowSoakActivation | None:
        """Read the activation, or `None`."""
        if not self.activation_path.exists():
            return None
        return PaperShadowSoakActivation.from_body(
            json.loads(self.activation_path.read_text(encoding="utf-8"))
        )

    # -- halt -------------------------------------------------------------

    def halt(self, *, reason: str, classification: str, at: str, detail: str = "") -> Path:
        """Record a halt. Terminal: this protocol can never pass.

        Args:
            reason: A stable reason code.
            classification: What kind of defect.
            at: When, ISO-8601.
            detail: A sanitised note.

        Returns:
            Where it was written.
        """
        from stock_screener.execution import fingerprint_of
        from stock_screener.paper_operations import atomic_write_verified

        protocol = self.protocol()
        body = {
            "version": "PAPER_SHADOW_SOAK_HALT_V1",
            "namespace": self.namespace,
            "protocol_version": protocol.protocol_version if protocol else "",
            "protocol_fingerprint": protocol.fingerprint() if protocol else "",
            "status": "HALTED",
            "reason": reason,
            "classification": classification,
            "halted_at": at,
            "qualifying_observations": len(self.counted_sessions()),
            "resumable": False,
            "fingerprint_reusable": False,
            "detail": detail,
        }
        atomic_write_verified(self.halt_path, {**body, "halt_fingerprint": fingerprint_of(body)})
        return self.halt_path

    def state(self) -> str:
        """The soak's state, failing safe.

        Returns:
            `HALTED` when a halt record exists, `RUNNING` once anything has
            been observed, `ARMED` after activation, `NOT_STARTED` before it.
            A halt is checked first and is never overridden by later evidence.
        """
        if self.halt_path.exists():
            return "HALTED"
        if self.activation() is None:
            return "NOT_STARTED"
        return "RUNNING" if self.observations() else "ARMED"

    # -- observations -----------------------------------------------------

    def record_observation(self, observation: Any) -> Any:
        """Record one session observation, bound to this protocol and build.

        Args:
            observation: A `PaperShadowSessionObservationV2`. The observation
                schema is deliberately reused rather than re-versioned: a new
                protocol does not imply a new observation shape, and
                conflating the two is what produced a class per soak.

        Returns:
            The observation.

        Raises:
            SoakContractError: `SOAK_DRILL_INADMISSIBLE` reused as the generic
                "this evidence is not for this soak" refusal when the protocol
                or commit differs, and `SOAK_CORRECTION_AMBIGUOUS` when a
                different live observation already exists for the session.
        """
        from stock_screener.paper_operations import atomic_write_verified

        activation = self.activation()
        if activation is None:
            raise SoakContractError(
                Code.SOAK_ACTIVATION_ORPHANED,
                "this soak is not activated, so nothing can be observed against it",
            )
        if str(getattr(observation, "protocol_fingerprint", "")) != (
            activation.protocol_fingerprint
        ):
            raise SoakContractError(
                Code.SOAK_DRILL_INADMISSIBLE,
                "this observation binds another protocol instance",
            )
        if str(getattr(observation, "qualifying_commit", "")) != activation.qualifying_commit:
            raise SoakContractError(
                Code.SOAK_DRILL_INADMISSIBLE,
                f"this observation was made under build "
                f"{str(getattr(observation, 'qualifying_commit', ''))[:12]} and the soak "
                f"judges {activation.qualifying_commit[:12]}",
            )
        session = str(observation.session)
        if session < activation.first_eligible_session:
            raise SoakContractError(
                Code.SOAK_RETROSPECTIVE_OBSERVATION,
                f"session {session} precedes the first eligible session "
                f"{activation.first_eligible_session}; no historical evidence counts",
            )
        live = [
            row
            for row in self.observations_for(session)
            if getattr(row, "superseded_by", None) is None
            and row.fingerprint() != observation.fingerprint()
        ]
        if live:
            raise SoakContractError(
                Code.SOAK_CORRECTION_AMBIGUOUS,
                f"session {session} already holds a different live observation. It is "
                "not overwritten: a correction supersedes explicitly and both records "
                "are kept",
            )
        atomic_write_verified(
            self.observations_directory / f"{observation.observation_id}.json",
            observation.as_json(),
        )
        return observation

    def supersede(self, original: Any, correction: Any, *, reason: str) -> Any:
        """Correct a session's observation, keeping both records.

        Args:
            original: The observation being corrected.
            correction: Its replacement.
            reason: Why.

        Returns:
            The correction.

        Raises:
            SoakContractError: `SOAK_CORRECTION_AMBIGUOUS` if the two describe
                different sessions or are identical.
        """
        import dataclasses

        from stock_screener.paper_operations import atomic_write_verified

        if str(original.session) != str(correction.session):
            raise SoakContractError(
                Code.SOAK_CORRECTION_AMBIGUOUS,
                f"a correction is for one session: {original.session} and "
                f"{correction.session} are different days",
            )
        if original.fingerprint() == correction.fingerprint():
            raise SoakContractError(
                Code.SOAK_CORRECTION_AMBIGUOUS,
                "the correction is identical to what is recorded; nothing to correct",
            )
        marked = dataclasses.replace(original, superseded_by=correction.observation_id)
        atomic_write_verified(
            self.observations_directory / f"{original.observation_id}.json",
            {**marked.as_json(), "superseded_reason": reason},
        )
        atomic_write_verified(
            self.observations_directory / f"{correction.observation_id}.json",
            {**correction.as_json(), "supersedes": original.observation_id},
        )
        return correction

    def observations(self) -> tuple[Any, ...]:
        """Every observation recorded, by session.

        Dispatched on the stored record's own `version`, so one store can hold
        the V2 shape and the V3 shape that binds derived gate evidence. The
        alternative — assuming a schema — is how a record written under one
        contract comes to be read under another.

        Raises:
            SoakContractError: `SOAK_STORE_INCOMPATIBLE` for a stored
                observation whose schema this build does not know. Guessing
                would mean reading safety numbers out of a record whose shape
                nobody has described.
        """
        if not self.observations_directory.exists():
            return ()
        rows = [
            self._observation(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(self.observations_directory.glob("*.json"))
        ]
        return tuple(sorted(rows, key=lambda row: (row.session, row.observation_id)))

    @staticmethod
    def _observation(body: dict[str, Any]) -> Any:
        """Rebuild one stored observation under its declared schema."""
        from .errors import Code, SoakContractError
        from .observations import SESSION_OBSERVATION_V3_VERSION, observation_from_body

        version = str(body.get("version", ""))
        if version == SESSION_OBSERVATION_V3_VERSION:
            return observation_from_body(body)
        if version == "PAPER_SHADOW_SESSION_OBSERVATION_V2":
            from stock_screener.paper_runtime.soak_v2 import _observation_from_body

            return _observation_from_body(body)
        raise SoakContractError(
            Code.SOAK_STORE_INCOMPATIBLE,
            f"a stored observation declares schema {version!r}, which this build does "
            "not know how to read. It is refused rather than guessed at",
        )

    def observations_for(self, session: str) -> tuple[Any, ...]:
        """Every observation for one session, corrections included."""
        return tuple(row for row in self.observations() if str(row.session) == session)

    def live_observations(self) -> tuple[Any, ...]:
        """Observations that have not been superseded.

        Raises:
            SoakContractError: `SOAK_CORRECTION_AMBIGUOUS` if a session holds
                more than one live observation. Refused rather than resolved by
                taking the newest file, because picking arbitrarily between two
                competing accounts of one session is how a soak scores evidence
                nobody agreed on.
        """
        live = [row for row in self.observations() if getattr(row, "superseded_by", None) is None]
        sessions = [str(row.session) for row in live]
        contested = sorted({row for row in sessions if sessions.count(row) > 1})
        if contested:
            raise SoakContractError(
                Code.SOAK_CORRECTION_AMBIGUOUS,
                f"session(s) {contested} hold more than one live observation. Scoring "
                "is refused rather than choosing between them",
            )
        return tuple(live)

    def counted_sessions(self) -> tuple[str, ...]:
        """Each eligible session counted at most once, however many runs it held."""
        if not self.observations_directory.exists():
            return ()
        return tuple(sorted({str(row.session) for row in self.live_observations()}))

    # -- drills -----------------------------------------------------------

    def record_drill(self, drill: DrillRecordV2) -> DrillRecordV2:
        """Record one bound drill in this namespace."""
        return self.drills.record(drill)

    def drill_records(self) -> tuple[Any, ...]:
        """Drill evidence admissible for this soak's protocol and build.

        Returns:
            Only the records that bind this protocol, this commit, this
            operations engine and this operations policy. Inadmissible records
            are excluded here rather than at scoring time, so the drill gate
            counts evidence that actually certifies this system.
        """
        protocol = self.protocol()
        if protocol is None:
            return ()
        from stock_screener.paper_operations import PAPER_OPERATIONS_ENGINE_VERSION

        admitted, _ = admissible_drills(
            self.drills.all(),
            protocol_fingerprint=protocol.fingerprint(),
            qualifying_commit=protocol.qualifying_commit,
            operations_engine_version=PAPER_OPERATIONS_ENGINE_VERSION,
            operations_policy_fingerprint=protocol.operations_policy_fingerprint,
        )
        return admitted

    def drill_rejections(self) -> tuple[str, ...]:
        """Why any recorded drill is not admissible for this soak."""
        protocol = self.protocol()
        if protocol is None:
            return ()
        from stock_screener.paper_operations import PAPER_OPERATIONS_ENGINE_VERSION

        _, rejected = admissible_drills(
            self.drills.all(),
            protocol_fingerprint=protocol.fingerprint(),
            qualifying_commit=protocol.qualifying_commit,
            operations_engine_version=PAPER_OPERATIONS_ENGINE_VERSION,
            operations_policy_fingerprint=protocol.operations_policy_fingerprint,
        )
        return rejected

    # -- the scorer's evidence interface ----------------------------------

    def open_incidents(self) -> tuple[Any, ...]:
        """Incidents still open on this operations root."""
        from stock_screener.paper_operations import IncidentStore

        return tuple(IncidentStore(self.root).open_incidents())

    def eligible_elapsed_sessions(self) -> tuple[str, ...]:
        """Sessions in the frozen window whose close has passed.

        Returns:
            The elapsed sessions. Derived from the activation's own window, so
            the denominator cannot drift from the one that was frozen.
        """
        from datetime import UTC, datetime

        activation = self.activation()
        if activation is None:
            return ()
        from stock_screener.paper_runtime.operational_calendar import session_calendar

        rows = session_calendar()
        now = datetime.now(UTC)
        return tuple(
            session
            for session in activation.window
            if session in rows and rows[session].closes_at() <= now
        )

    def soak_state(self) -> str:
        """The soak's state, for the scorer."""
        return self.state()


#: The generic store satisfies the scorer's evidence interface directly, so no
#: adapter is needed. Named for readers looking for one.
SoakEvidenceView = VersionedSoakStore
