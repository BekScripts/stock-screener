"""The seam a future phase can use to hold an anchor out of local reach.

The local head anchor closes the hole P1.3 left: a truncated ledger no longer
passes as a complete one. What it cannot close is a *coordinated* rollback.
Anyone who can delete ledger records can usually also rewrite the file sitting
next to them, and a ledger and anchor rolled back together to a mutually
consistent earlier state will validate perfectly. Local files cannot establish
that they are the latest state of themselves — that is not an implementation gap
but a property of where they live.

Closing it needs an anchor recorded somewhere the ledger's writer cannot reach,
so that the two can be compared by something that trusts neither. This module is
the shape of that comparison and nothing more: a protocol, and an in-memory
implementation for tests.

**There is deliberately no network here.** No HTTP client, no socket, no SDK, no
credential. A phase that wants a remote mirror implements this protocol outside
the accounting package and injects it, which keeps the capability absent from the
core rather than merely unused — the same reasoning that keeps order submission
out of the risk engine. P1.4 ships no remote implementation and requires none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .errors import AccountingError, Code

if TYPE_CHECKING:
    from .anchor import LedgerHeadAnchor

__all__ = [
    "EXTERNAL_ANCHOR_CONTRACT",
    "ExternalAnchorRecord",
    "ExternalAnchorSink",
    "InMemoryExternalAnchor",
    "compare_external_anchor",
]

#: The mirroring contract. A future implementation satisfies this and nothing
#: in the accounting core changes.
EXTERNAL_ANCHOR_CONTRACT = "PORTFOLIO_EXTERNAL_ANCHOR_V1"


class ExternalAnchorRecord(dict[str, Any]):
    """What is mirrored: the sequence, the ledger head, the anchor fingerprint.

    Deliberately the minimum. A mirror holding the whole ledger would be a second
    copy to keep in step; a mirror holding three fields is enough to prove a local
    ledger is not older than what was last witnessed, and nothing more is needed
    for that.
    """


@runtime_checkable
class ExternalAnchorSink(Protocol):
    """Somewhere an anchor can be recorded beyond the ledger's own writer."""

    def publish(self, *, sequence: int, ledger_head: str, anchor_fingerprint: str) -> None:
        """Record a head beyond local reach.

        Args:
            sequence: The committed sequence.
            ledger_head: That record's chain hash.
            anchor_fingerprint: The local anchor's digest.
        """
        ...

    def latest(self) -> ExternalAnchorRecord | None:
        """The highest head this sink has witnessed, if any."""
        ...


class InMemoryExternalAnchor:
    """An `ExternalAnchorSink` that forgets when the process does.

    Useful for exercising the comparison and useless as a trust anchor: it shares
    the fate of whatever it is protecting. Shipped so the seam is testable, and
    named so nobody mistakes it for a guarantee.
    """

    def __init__(self) -> None:
        """Start with nothing witnessed."""
        self._latest: ExternalAnchorRecord | None = None

    def publish(self, *, sequence: int, ledger_head: str, anchor_fingerprint: str) -> None:
        """Record a head, refusing to go backward.

        Args:
            sequence: The committed sequence.
            ledger_head: That record's chain hash.
            anchor_fingerprint: The local anchor's digest.

        Raises:
            AccountingError: `LEDGER_TRUNCATION_DETECTED` if asked to witness a
                sequence below one already witnessed. A mirror that accepted a
                regression would be as rewritable as the thing it checks.
        """
        if self._latest is not None and sequence < int(self._latest["sequence"]):
            raise AccountingError(
                Code.LEDGER_TRUNCATION_DETECTED,
                f"the external anchor has witnessed sequence {self._latest['sequence']} "
                f"and was asked to witness {sequence}. A mirror that moves backward "
                "offers no more assurance than the local files it exists to check.",
            )
        self._latest = ExternalAnchorRecord(
            contract=EXTERNAL_ANCHOR_CONTRACT,
            sequence=sequence,
            ledger_head=ledger_head,
            anchor_fingerprint=anchor_fingerprint,
        )

    def latest(self) -> ExternalAnchorRecord | None:
        """The highest head witnessed."""
        return self._latest


def compare_external_anchor(
    local: LedgerHeadAnchor | None, external: ExternalAnchorRecord | None
) -> dict[str, Any]:
    """Compare a local anchor against an independently held one.

    Args:
        local: The anchor stored beside the ledger.
        external: What the independent sink last witnessed.

    Returns:
        The comparison. `rollback_detected` is the thing a local anchor alone
        cannot report.
    """
    if external is None:
        return {
            "contract": EXTERNAL_ANCHOR_CONTRACT,
            "external_available": False,
            "rollback_detected": None,
            "detail": (
                "no external anchor is configured, so a coordinated rollback of the "
                "ledger and its local anchor would not be detected"
            ),
        }
    local_sequence = local.sequence if local else 0
    external_sequence = int(external["sequence"])
    behind = local_sequence < external_sequence
    diverged = (
        not behind
        and local is not None
        and local_sequence == external_sequence
        and local.ledger_head_hash != str(external["ledger_head"])
    )
    return {
        "contract": EXTERNAL_ANCHOR_CONTRACT,
        "external_available": True,
        "local_sequence": local_sequence,
        "external_sequence": external_sequence,
        "rollback_detected": behind or diverged,
        "detail": (
            (
                f"the external anchor witnessed sequence {external_sequence} but the "
                f"local anchor is at {local_sequence}: the local ledger and anchor "
                "have been rolled back together"
            )
            if behind
            else (
                "the local and external anchors agree on sequence but not on the "
                "ledger head: the history was rewritten in place"
            )
            if diverged
            else None
        ),
    }
