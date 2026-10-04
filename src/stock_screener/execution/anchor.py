"""The journal's durable head anchor, and the limit it cannot pass.

The accounting layer learned this in P1.4 and the execution layer starts with it:
a hash chain cannot establish its own length. A journal missing its last records
is a valid shorter journal, so something outside the chain has to record how far
it got. That something is written on every committed event — not when an operator
remembers, because an integrity property that depends on remembering is not one.

    anchor ahead of journal  ->  EXECUTION_JOURNAL_TRUNCATION_DETECTED
    journal ahead of anchor  ->  EXECUTION_RECOVERY_REQUIRED (recoverable)
    same sequence, different head  ->  EXECUTION_JOURNAL_HEAD_ANCHOR_MISMATCH

The anchor never moves backward. Lowering it to match a shorter journal would
erase the only local evidence that committed records are missing, turning a
detected truncation into a clean journal.

**What this cannot do**, stated as plainly here as in the accounting layer: a
journal and its local anchor rolled back *together* to a mutually consistent
earlier state validate cleanly. Anyone able to delete journal records can rewrite
the file beside them, and local files cannot establish that they are the latest
state of themselves. Closing that needs an anchor held where the journal's writer
cannot reach — `ExternalAnchorSink` in the accounting package is the shape of it,
and this phase implements no remote sink.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .errors import Code, ExecutionError
from .journal import GENESIS_HASH
from .versions import EXECUTION_ENGINE_VERSION

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .journal import JournalRecord

__all__ = [
    "JOURNAL_ANCHOR_VERSION",
    "ExecutionJournalHeadAnchor",
    "JournalAnchorStatus",
    "classify_journal_anchor",
]

#: The anchor contract. Changing what it binds makes every existing anchor
#: unverifiable, which is a migration rather than a fix.
JOURNAL_ANCHOR_VERSION = "EXECUTION_JOURNAL_HEAD_ANCHOR_V1"


class JournalAnchorStatus(StrEnum):
    """How a stored anchor relates to the journal beside it."""

    CONSISTENT = "CONSISTENT"
    """Anchor and journal agree."""

    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    """The journal is ahead: a crash landed between record and anchor
    durability. The tail is verifiable and the anchor can be advanced over it."""

    TRUNCATION_DETECTED = "TRUNCATION_DETECTED"
    """The anchor is ahead: records it witnessed are gone."""

    HEAD_MISMATCH = "HEAD_MISMATCH"
    """Same sequence, different history. The journal was rewritten in place."""

    ANCHOR_MISSING = "ANCHOR_MISSING"
    """Records with nothing witnessing them. Completeness is unestablished."""

    GENERATION_MISMATCH = "GENERATION_MISMATCH"
    """The anchor describes a different journal lineage."""

    @property
    def is_recoverable(self) -> bool:
        """Whether ordinary recovery can resolve this without a decision."""
        return self in {
            JournalAnchorStatus.CONSISTENT,
            JournalAnchorStatus.RECOVERY_REQUIRED,
        }


@dataclass(frozen=True)
class ExecutionJournalHeadAnchor:
    """A durable statement of where an execution journal ends.

    Attributes:
        journal_generation: The lineage this describes. Two unrelated journals
            can share a sequence number, so an anchor from another is refused
            rather than compared.
        sequence: The last committed sequence.
        journal_head_hash: That record's chain hash.
        order_state_fingerprint: A digest of every order's derived state, so the
            anchor witnesses a set of orders and not merely a file length.
        engine_version: Which execution rules produced it.
        execution_plan_id: Which plan the journal is executing.
        previous_anchor_fingerprint: The anchor this replaced.
        written_at: When it was written. Operational, outside the fingerprint.
    """

    journal_generation: str
    sequence: int
    journal_head_hash: str
    order_state_fingerprint: str
    engine_version: str = EXECUTION_ENGINE_VERSION
    execution_plan_id: str = ""
    previous_anchor_fingerprint: str | None = None
    written_at: str | None = None

    def __post_init__(self) -> None:
        """Refuse an anchor that could not describe a journal.

        Raises:
            ExecutionError: `EXECUTION_ANCHOR_INVALID` on a missing generation, a
                negative sequence, or a sequence and head that disagree about
                whether the journal is empty.
        """
        if not self.journal_generation:
            raise ExecutionError(
                Code.EXECUTION_ANCHOR_INVALID,
                "an anchor must name the journal generation it describes",
            )
        if self.sequence < 0:
            raise ExecutionError(
                Code.EXECUTION_ANCHOR_INVALID,
                f"an anchor sequence may not be negative ({self.sequence})",
            )
        if self.sequence == 0 and self.journal_head_hash != GENESIS_HASH:
            raise ExecutionError(
                Code.EXECUTION_ANCHOR_INVALID,
                "an anchor at sequence 0 must bind the genesis head; a non-genesis "
                "head there describes records it also says do not exist",
            )
        if self.sequence > 0 and self.journal_head_hash == GENESIS_HASH:
            raise ExecutionError(
                Code.EXECUTION_ANCHOR_INVALID,
                f"an anchor at sequence {self.sequence} binds the genesis head, so "
                "it claims committed records with no history behind them",
            )

    def body(self) -> dict[str, Any]:
        """The fingerprinted content, without the writing clock."""
        return {
            "anchor_version": JOURNAL_ANCHOR_VERSION,
            "journal_generation": self.journal_generation,
            "sequence": self.sequence,
            "journal_head_hash": self.journal_head_hash,
            "order_state_fingerprint": self.order_state_fingerprint,
            "engine_version": self.engine_version,
            "execution_plan_id": self.execution_plan_id,
            "previous_anchor_fingerprint": self.previous_anchor_fingerprint,
        }

    def fingerprint(self) -> str:
        """The anchor's deterministic digest."""
        return hashlib.sha256(
            json.dumps(self.body(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def as_json(self) -> dict[str, Any]:
        """Return the anchor in its persisted form."""
        return {
            **self.body(),
            "anchor_fingerprint": self.fingerprint(),
            "written_at": self.written_at,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> ExecutionJournalHeadAnchor:
        """Rebuild an anchor and verify its own digest.

        Args:
            payload: The anchor as written.

        Returns:
            The anchor.

        Raises:
            ExecutionError: `EXECUTION_ANCHOR_INVALID` on an unknown version or a
                missing field; `EXECUTION_ANCHOR_FINGERPRINT_MISMATCH` if the
                stored digest does not match the content, which means the anchor
                file itself was edited.
        """
        version = str(payload.get("anchor_version", ""))
        if version != JOURNAL_ANCHOR_VERSION:
            raise ExecutionError(
                Code.EXECUTION_ANCHOR_INVALID,
                f"anchor version {version!r} is not {JOURNAL_ANCHOR_VERSION}; an "
                "anchor whose binding rules are unknown cannot establish "
                "completeness",
            )
        try:
            anchor = cls(
                journal_generation=str(payload["journal_generation"]),
                sequence=int(payload["sequence"]),
                journal_head_hash=str(payload["journal_head_hash"]),
                order_state_fingerprint=str(payload["order_state_fingerprint"]),
                engine_version=str(payload["engine_version"]),
                execution_plan_id=str(payload.get("execution_plan_id", "")),
                previous_anchor_fingerprint=payload.get("previous_anchor_fingerprint"),
                written_at=payload.get("written_at"),
            )
        except KeyError as exc:
            raise ExecutionError(
                Code.EXECUTION_ANCHOR_INVALID, f"the anchor is missing {exc.args[0]!r}"
            ) from exc
        stored = str(payload.get("anchor_fingerprint", ""))
        if stored != anchor.fingerprint():
            raise ExecutionError(
                Code.EXECUTION_ANCHOR_FINGERPRINT_MISMATCH,
                f"the anchor stores fingerprint {stored[:16]}… but its content "
                f"hashes to {anchor.fingerprint()[:16]}…; the file has been edited",
            )
        return anchor


def classify_journal_anchor(
    anchor: ExecutionJournalHeadAnchor | None,
    records: Sequence[JournalRecord],
    *,
    journal_generation: str,
    derived_state_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Decide how a stored anchor relates to the journal beside it.

    Reads and decides; writes nothing and advances nothing. The recovery policy
    lives in the store, so this decision can be tested without a filesystem.

    Args:
        anchor: The stored anchor, or None.
        records: The journal's records, assumed already chain-verified.
        journal_generation: The lineage the journal belongs to.
        derived_state_fingerprint: The digest of the state the records replay to,
            when computed. Omitted skips that comparison.

    Returns:
        The status, both sequences, and what follows.
    """
    journal_sequence = records[-1].sequence if records else 0
    journal_head = records[-1].record_hash if records else GENESIS_HASH

    def result(status: JournalAnchorStatus, detail: str | None) -> dict[str, Any]:
        return {
            "anchor_version": JOURNAL_ANCHOR_VERSION,
            "status": str(status),
            "consistent": status is JournalAnchorStatus.CONSISTENT,
            "recoverable": status.is_recoverable,
            "anchor_sequence": anchor.sequence if anchor else None,
            "anchor_head": anchor.journal_head_hash if anchor else None,
            "anchor_fingerprint": anchor.fingerprint() if anchor else None,
            "journal_sequence": journal_sequence,
            "journal_head": journal_head,
            "detail": detail,
            "coordinated_rollback_detection": "NOT_GUARANTEED_WITHOUT_EXTERNAL_ANCHOR",
        }

    if anchor is None:
        if not records:
            return result(JournalAnchorStatus.CONSISTENT, "an empty journal needs no anchor")
        return result(
            JournalAnchorStatus.ANCHOR_MISSING,
            f"the journal holds {journal_sequence} records and no anchor witnesses "
            "them, so nothing establishes that this is the whole journal. A chain "
            "verifies a truncated journal perfectly well.",
        )

    if anchor.journal_generation != journal_generation:
        return result(
            JournalAnchorStatus.GENERATION_MISMATCH,
            f"the anchor describes generation {anchor.journal_generation!r} but the "
            f"journal is {journal_generation!r}; two journals can share a sequence "
            "number, so these are not comparable",
        )

    if anchor.sequence > journal_sequence:
        return result(
            JournalAnchorStatus.TRUNCATION_DETECTED,
            f"the anchor witnessed sequence {anchor.sequence} but the journal ends "
            f"at {journal_sequence}: {anchor.sequence - journal_sequence} committed "
            "record(s) are gone. The surviving chain is internally valid, which is "
            "exactly why the anchor is needed to notice. The anchor is not moved "
            "backward to match.",
        )

    if anchor.sequence < journal_sequence:
        return result(
            JournalAnchorStatus.RECOVERY_REQUIRED,
            f"the journal holds sequence {journal_sequence} but the anchor witnessed "
            f"{anchor.sequence}. A crash between record durability and anchor "
            "durability produces exactly this; the tail is verifiable, so recovery "
            "may replay it and advance the anchor.",
        )

    if anchor.journal_head_hash != journal_head:
        return result(
            JournalAnchorStatus.HEAD_MISMATCH,
            f"both are at sequence {journal_sequence} but the anchor binds head "
            f"{anchor.journal_head_hash[:16]}… while the journal hashes to "
            f"{journal_head[:16]}…: the history was rewritten in place",
        )

    if (
        derived_state_fingerprint is not None
        and derived_state_fingerprint != anchor.order_state_fingerprint
    ):
        return result(
            JournalAnchorStatus.HEAD_MISMATCH,
            "the chain and sequence agree but the records replay to order state "
            f"{derived_state_fingerprint[:16]}… while the anchor witnessed "
            f"{anchor.order_state_fingerprint[:16]}…: the same events are being "
            "read as a different set of orders",
        )

    return result(JournalAnchorStatus.CONSISTENT, None)
