"""The durable head anchor: an assertion that the committed tail is complete.

P1.3 observed the hole and did not close it. A hash chain proves its records are
internally consistent, but a ledger with its last three records deleted is still
a perfectly consistent chain — just a shorter one. P1.3's answer was a
checkpoint, which works and depends on somebody remembering to write one. An
integrity guarantee that holds only when an operator remembered to schedule
something is not a guarantee; it is a habit.

So the anchor is mandatory. Every committed event updates it, and an event
without a corresponding valid anchor is not considered committed — it is
`RECOVERY_REQUIRED`. The anchor records where the ledger *should* end, so a
ledger that ends earlier is detected rather than accepted:

    anchor says 101, ledger ends at 100  ->  LEDGER_TRUNCATION_DETECTED
    anchor says 100, ledger ends at 101  ->  RECOVERY_REQUIRED (recoverable)
    same sequence, different head        ->  LEDGER_HEAD_ANCHOR_MISMATCH

The anchor never moves backward. A ledger that has lost records is a question for
a person, and rewriting the anchor down to match would answer it by forgetting it.

**What this does not do.** The anchor is a local file beside the ledger it
describes. Anyone able to truncate the ledger is generally able to rewrite the
anchor too, and a ledger and anchor rolled back *together* to a mutually
consistent earlier state will validate cleanly. That is a real limit, not a
detail: local files cannot establish that they are the latest state of
themselves. Detecting it needs an anchor held somewhere the ledger's writer
cannot reach, which `ExternalAnchorSink` exists to make possible later and which
P1.4 deliberately does not implement.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .errors import AccountingError, Code
from .ledger import GENESIS_HASH

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .ledger import LedgerRecord
    from .state import PortfolioState

__all__ = [
    "ANCHOR_VERSION",
    "AnchorStatus",
    "LedgerHeadAnchor",
    "classify_anchor",
    "state_fingerprint",
]

#: The anchor contract. Changing what it binds would make every existing anchor
#: unverifiable, which is a migration rather than a fix.
ANCHOR_VERSION = "PORTFOLIO_LEDGER_HEAD_ANCHOR_V1"


class AnchorStatus(StrEnum):
    """How a stored anchor relates to the ledger beside it."""

    CONSISTENT = "CONSISTENT"
    """Anchor and ledger agree on sequence, head and state."""

    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    """The ledger is ahead. A crash landed between event and anchor durability;
    the tail is verifiable and the anchor can be advanced over it."""

    LEDGER_TRUNCATION_DETECTED = "LEDGER_TRUNCATION_DETECTED"
    """The anchor is ahead. Records the anchor witnessed are gone."""

    LEDGER_HEAD_ANCHOR_MISMATCH = "LEDGER_HEAD_ANCHOR_MISMATCH"
    """Same sequence, different history. The ledger was rewritten."""

    ANCHOR_MISSING = "ANCHOR_MISSING"
    """A ledger with records and no anchor. Completeness is unestablished."""

    STATE_FINGERPRINT_MISMATCH = "STATE_FINGERPRINT_MISMATCH"
    """Chain and sequence agree but the replayed state does not match what the
    anchor witnessed — the events parse to a different portfolio."""

    GENERATION_MISMATCH = "GENERATION_MISMATCH"
    """The anchor describes a different ledger lineage entirely."""

    @property
    def is_recoverable(self) -> bool:
        """Whether ordinary recovery can resolve this without a decision."""
        return self in {AnchorStatus.CONSISTENT, AnchorStatus.RECOVERY_REQUIRED}


#: Keys excluded from the state digest. The ledger's position in history is
#: already bound by the chain, and `identity` carries the display symbol — which
#: the ledger deliberately does not persist, so including it would make an anchor
#: fail against the very ledger it was written for. The symbol-free `label` stays.
_UNFINGERPRINTED: frozenset[str] = frozenset(
    {"ledger_sequence", "ledger_head_hash", "last_effective_at", "identity"}
)


def state_fingerprint(state: PortfolioState) -> str:
    """Digest the accounting content of a replayed state.

    Binds what the events mean rather than how they are stored, so an anchor
    witnesses a portfolio and not a file layout. Two things are therefore left
    out. The ledger's own position in history is excluded because the chain
    already binds it, and including it would move the digest for reasons
    unrelated to the figures. The display symbol is excluded because identity is
    `(kind, identity_id)` and a ticker is a dated display attribute the ledger
    does not persist — an anchor written from an in-memory ledger would otherwise
    never match the same ledger read back from disk.

    Args:
        state: The replayed state.

    Returns:
        SHA-256 over the state's accounting figures.
    """

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: strip(inner) for key, inner in value.items() if key not in _UNFINGERPRINTED
            }
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    return hashlib.sha256(
        json.dumps(
            strip(state.as_json()), sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class LedgerHeadAnchor:
    """A durable statement of where a ledger ends.

    Attributes:
        ledger_generation: The ledger lineage this describes. An anchor from a
            different lineage is refused rather than compared, because two
            unrelated ledgers can legitimately share a sequence number.
        sequence: The last committed sequence.
        ledger_head_hash: That record's chain hash.
        state_fingerprint: The digest of the state those records replay to.
        engine_version: Which accounting rules produced that state.
        previous_anchor_fingerprint: The anchor this one replaced, so the
            anchors form their own append-only trail. Useful for seeing how the
            head advanced; it does not help against a coordinated rollback,
            which would rewrite the trail too.
        written_at: When the anchor was written. Operational, and outside the
            fingerprint — a digest that moved with the clock could never
            establish that two anchors agree.
    """

    ledger_generation: str
    sequence: int
    ledger_head_hash: str
    state_fingerprint: str
    engine_version: str
    previous_anchor_fingerprint: str | None = None
    written_at: str | None = None

    def __post_init__(self) -> None:
        """Refuse an anchor that could not describe a ledger.

        Raises:
            AccountingError: `ANCHOR_INVALID` on a missing generation, a
                negative sequence, or a sequence and head that disagree about
                whether the ledger is empty.
        """
        if not self.ledger_generation:
            raise AccountingError(
                Code.ANCHOR_INVALID, "an anchor must name the ledger generation it describes"
            )
        if self.sequence < 0:
            raise AccountingError(
                Code.ANCHOR_INVALID, f"an anchor sequence may not be negative ({self.sequence})"
            )
        if self.sequence == 0 and self.ledger_head_hash != GENESIS_HASH:
            raise AccountingError(
                Code.ANCHOR_INVALID,
                "an anchor at sequence 0 must bind the genesis head; a non-genesis "
                "head at sequence 0 describes records it also says do not exist",
            )
        if self.sequence > 0 and self.ledger_head_hash == GENESIS_HASH:
            raise AccountingError(
                Code.ANCHOR_INVALID,
                f"an anchor at sequence {self.sequence} binds the genesis head, so it "
                "claims committed records with no history behind them",
            )

    def fingerprint(self) -> str:
        """The anchor's deterministic digest."""
        return hashlib.sha256(
            json.dumps(self.body(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def body(self) -> dict[str, Any]:
        """The fingerprinted content, without the writing clock."""
        return {
            "anchor_version": ANCHOR_VERSION,
            "ledger_generation": self.ledger_generation,
            "sequence": self.sequence,
            "ledger_head_hash": self.ledger_head_hash,
            "state_fingerprint": self.state_fingerprint,
            "engine_version": self.engine_version,
            "previous_anchor_fingerprint": self.previous_anchor_fingerprint,
        }

    def as_json(self) -> dict[str, Any]:
        """Return the anchor in its persisted form."""
        return {
            **self.body(),
            "anchor_fingerprint": self.fingerprint(),
            "written_at": self.written_at,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> LedgerHeadAnchor:
        """Rebuild an anchor and verify its own digest.

        Args:
            payload: The anchor as written.

        Returns:
            The anchor.

        Raises:
            AccountingError: `ANCHOR_INVALID` if a required field is absent or
                the version is unknown; `ANCHOR_FINGERPRINT_MISMATCH` if the
                stored digest does not match the content, which means the anchor
                file itself was edited.
        """
        version = str(payload.get("anchor_version", ""))
        if version != ANCHOR_VERSION:
            raise AccountingError(
                Code.ANCHOR_INVALID,
                f"anchor version {version!r} is not {ANCHOR_VERSION}; an anchor whose "
                "binding rules are unknown cannot establish completeness",
            )
        try:
            anchor = cls(
                ledger_generation=str(payload["ledger_generation"]),
                sequence=int(payload["sequence"]),
                ledger_head_hash=str(payload["ledger_head_hash"]),
                state_fingerprint=str(payload["state_fingerprint"]),
                engine_version=str(payload["engine_version"]),
                previous_anchor_fingerprint=payload.get("previous_anchor_fingerprint"),
                written_at=payload.get("written_at"),
            )
        except KeyError as exc:
            raise AccountingError(
                Code.ANCHOR_INVALID, f"the anchor is missing {exc.args[0]!r}"
            ) from exc
        stored = str(payload.get("anchor_fingerprint", ""))
        if stored != anchor.fingerprint():
            raise AccountingError(
                Code.ANCHOR_FINGERPRINT_MISMATCH,
                f"the anchor stores fingerprint {stored[:16]}… but its content hashes "
                f"to {anchor.fingerprint()[:16]}…; the anchor file has been edited",
            )
        return anchor


def classify_anchor(
    anchor: LedgerHeadAnchor | None,
    records: Sequence[LedgerRecord],
    *,
    ledger_generation: str,
    replayed_state_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Decide how a stored anchor relates to the ledger beside it.

    Classification is deliberately separate from acting on it: this function
    reads and decides, and nothing here writes a file or advances anything. The
    recovery policy lives in the store, so the decision can be tested without a
    filesystem and inspected without risking a mutation.

    Args:
        anchor: The stored anchor, or None if there is none.
        records: The ledger's records, in order. Assumed already chain-verified;
            a broken chain is a different and earlier failure.
        ledger_generation: The lineage the ledger belongs to.
        replayed_state_fingerprint: The digest of the state the records replay
            to, when it has been computed. Omitted skips the state comparison.

    Returns:
        The status, the two sequences, and what follows.
    """
    ledger_sequence = records[-1].sequence if records else 0
    ledger_head = records[-1].record_hash if records else GENESIS_HASH

    if anchor is None:
        status = AnchorStatus.CONSISTENT if not records else AnchorStatus.ANCHOR_MISSING
        return _result(
            status,
            anchor=None,
            ledger_sequence=ledger_sequence,
            ledger_head=ledger_head,
            detail=(
                "an empty ledger needs no anchor"
                if not records
                else (
                    f"the ledger holds {ledger_sequence} records and no anchor "
                    "witnesses them, so nothing establishes that this is the whole "
                    "ledger. A chain verifies a truncated ledger perfectly well."
                )
            ),
        )

    if anchor.ledger_generation != ledger_generation:
        return _result(
            AnchorStatus.GENERATION_MISMATCH,
            anchor=anchor,
            ledger_sequence=ledger_sequence,
            ledger_head=ledger_head,
            detail=(
                f"the anchor describes generation {anchor.ledger_generation!r} but the "
                f"ledger is {ledger_generation!r}. Two unrelated ledgers can share a "
                "sequence number, so these are not comparable."
            ),
        )

    if anchor.sequence > ledger_sequence:
        return _result(
            AnchorStatus.LEDGER_TRUNCATION_DETECTED,
            anchor=anchor,
            ledger_sequence=ledger_sequence,
            ledger_head=ledger_head,
            detail=(
                f"the anchor witnessed sequence {anchor.sequence} but the ledger ends "
                f"at {ledger_sequence}: {anchor.sequence - ledger_sequence} committed "
                "record(s) are gone. The surviving chain is internally valid, which is "
                "exactly why the anchor is needed to notice. The anchor is not moved "
                "backward to match."
            ),
        )

    if anchor.sequence < ledger_sequence:
        return _result(
            AnchorStatus.RECOVERY_REQUIRED,
            anchor=anchor,
            ledger_sequence=ledger_sequence,
            ledger_head=ledger_head,
            detail=(
                f"the ledger holds sequence {ledger_sequence} but the anchor witnessed "
                f"{anchor.sequence}. A crash between event durability and anchor "
                "durability produces exactly this. The tail is verifiable, so recovery "
                "may replay it and advance the anchor; no event is re-applied."
            ),
        )

    if anchor.ledger_head_hash != ledger_head:
        return _result(
            AnchorStatus.LEDGER_HEAD_ANCHOR_MISMATCH,
            anchor=anchor,
            ledger_sequence=ledger_sequence,
            ledger_head=ledger_head,
            detail=(
                f"both are at sequence {ledger_sequence} but the anchor binds head "
                f"{anchor.ledger_head_hash[:16]}… while the ledger hashes to "
                f"{ledger_head[:16]}…: the history was rewritten in place"
            ),
        )

    if (
        replayed_state_fingerprint is not None
        and replayed_state_fingerprint != anchor.state_fingerprint
    ):
        return _result(
            AnchorStatus.STATE_FINGERPRINT_MISMATCH,
            anchor=anchor,
            ledger_sequence=ledger_sequence,
            ledger_head=ledger_head,
            detail=(
                "the chain and sequence agree but the records replay to state "
                f"{replayed_state_fingerprint[:16]}… while the anchor witnessed "
                f"{anchor.state_fingerprint[:16]}…. The same events are being read as "
                "a different portfolio, which usually means the accounting version "
                "differs from the one the anchor was written under "
                f"({anchor.engine_version})."
            ),
        )

    return _result(
        AnchorStatus.CONSISTENT,
        anchor=anchor,
        ledger_sequence=ledger_sequence,
        ledger_head=ledger_head,
        detail=None,
    )


def _result(
    status: AnchorStatus,
    *,
    anchor: LedgerHeadAnchor | None,
    ledger_sequence: int,
    ledger_head: str,
    detail: str | None,
) -> dict[str, Any]:
    """Shape a classification result."""
    return {
        "anchor_version": ANCHOR_VERSION,
        "status": str(status),
        "consistent": status is AnchorStatus.CONSISTENT,
        "recoverable": status.is_recoverable,
        "anchor_sequence": anchor.sequence if anchor else None,
        "anchor_head": anchor.ledger_head_hash if anchor else None,
        "anchor_fingerprint": anchor.fingerprint() if anchor else None,
        "ledger_sequence": ledger_sequence,
        "ledger_head": ledger_head,
        "detail": detail,
        "coordinated_rollback_detection": "NOT_GUARANTEED_WITHOUT_EXTERNAL_ANCHOR",
    }
