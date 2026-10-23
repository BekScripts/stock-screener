"""Decision and execution packets, and the rule that keeps them apart.

The separation is the whole point. A decision packet records what was knowable
at 16:00 on the quarter-end session and what each strategy chose from it; an
execution packet records what the next session's open was. Merging them would
put a price the decision could not have seen inside the record of that decision,
and the fact that it arrived later is exactly what makes it inadmissible.

So an execution packet links to its decision by digest and never touches it, a
correction is a third kind of record that links to both and changes neither, and
`finalise` refuses to write a packet whose key already exists with different
bytes. An idempotent retry is fine; a rewrite is a protocol conflict.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from stock_screener.prospective_protocol import (
    CANDIDATE_ID,
    PROSPECTIVE_CONTRACT_ID,
    PROTOCOL_ID,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = [
    "PacketConflictError",
    "correction_record",
    "decision_packet",
    "execution_packet",
    "finalise",
    "packet_digest",
]

#: Fields no decision packet may ever carry.
FORBIDDEN_DECISION_FIELDS: frozenset[str] = frozenset(
    {
        "forward_return",
        "total_return",
        "execution_price",
        "reference_open",
        "next_session_open",
        "performance",
        "pnl",
        "realised_return",
    }
)


class PacketConflictError(RuntimeError):
    """Raised when a finalised packet would be rewritten with different bytes."""


def packet_digest(body: Mapping[str, Any]) -> str:
    """Digest a packet the way every other record in this project is digested.

    Args:
        body: The packet.

    Returns:
        The SHA-256 of its canonical JSON.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _identities(
    keys: Any, ranks: Mapping[str, int], scores: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Render identities in the packet schema's shape."""
    rendered: list[dict[str, Any]] = []
    for entry in keys:
        kind, identity_id = (entry[0], int(entry[1]))
        label = f"{kind}:{identity_id}"
        rendered.append(
            {
                "kind": kind,
                "identity_id": identity_id,
                "rank": ranks.get(label),
                "score": scores.get(label),
            }
        )
    return rendered


def decision_packet(
    *,
    packet_key: str,
    decision_timestamp: str,
    benchmark_session: str,
    execution_session: str,
    code_commit: str,
    runner_armed_at: str,
    protocol_version: str,
    fingerprints: Mapping[str, str],
    targets: Mapping[str, Any],
    holdings_before: Any,
    forced_exits: Any,
    observation_kind: str = "PROSPECTIVE",
    labels: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Build a decision packet conforming to the frozen schema.

    Args:
        packet_key: The decision session's ISO date.
        decision_timestamp: The decision instant with offset.
        benchmark_session: The quarter-end session.
        execution_session: The first session strictly after it.
        code_commit: The immutable commit the runner is bound to.
        runner_armed_at: When the runner was armed.
        protocol_version: The protocol's version string.
        fingerprints: The five source fingerprints.
        targets: The output of `paired_targets`.
        holdings_before: What H2 held going in.
        forced_exits: Incumbents the contract removed, with causes.
        observation_kind: `PROSPECTIVE` or `RETROSPECTIVE`.
        labels: Mandatory labels for a retrospective packet.
        notes: Anything worth recording.

    Returns:
        The packet.

    Raises:
        ValueError: If a forbidden outcome field would be written, or a
            retrospective packet lacks its labels.
    """
    ranks = dict(targets.get("ranks") or {})
    scores = dict(targets.get("scores") or {})
    if observation_kind == "RETROSPECTIVE":
        required = {"RETROSPECTIVE", "NOT_PROSPECTIVE_CONFIRMATION"}
        if not required.issubset(set(labels or ())):
            raise ValueError(
                "a retrospective packet must carry RETROSPECTIVE and NOT_PROSPECTIVE_CONFIRMATION"
            )
    body: dict[str, Any] = {
        "packet_key": packet_key,
        "decision_timestamp": decision_timestamp,
        "benchmark_session": benchmark_session,
        "execution_session": execution_session,
        "candidate_id": CANDIDATE_ID,
        "protocol_id": PROTOCOL_ID,
        "protocol_version": protocol_version,
        "contract_id": PROSPECTIVE_CONTRACT_ID,
        "code_commit": code_commit,
        "runner_armed_at": runner_armed_at,
        "fingerprints": dict(fingerprints),
        "target_count": int(targets["target_count"]),
        "cross_section_size": int(targets["cross_section_size"]),
        "target_count_function": str(targets["target_count_function"]),
        "comparator_target_set": _identities(targets["comparator_target_set"], ranks, scores),
        "h2_target_set": _identities(targets["h2_target_set"], ranks, scores),
        "h2_holdings_before_decision": _identities(holdings_before, ranks, scores),
        "forced_exits": list(forced_exits),
        "retained_incumbents": _identities(
            list(targets["h2_retained_in_band"]) + list(targets["h2_retained_in_decile"]),
            ranks,
            scores,
        ),
        "new_entrants": _identities(targets["h2_admitted"], ranks, scores),
        "unpriced": [],
        "observation_kind": observation_kind,
    }
    if labels:
        body["labels"] = sorted(labels)
    if notes:
        body["notes"] = list(notes)
    offending = FORBIDDEN_DECISION_FIELDS & set(body)
    if offending:
        raise ValueError(f"a decision packet may not carry {sorted(offending)}")
    return body


def execution_packet(
    *,
    packet_key: str,
    decision_packet_sha256: str,
    execution_session: str,
    captured_at: str,
    reference_opens: Mapping[str, float | None],
    unpriced: list[dict[str, Any]],
    source: Mapping[str, Any],
    capture_status: str,
) -> dict[str, Any]:
    """Build an execution packet, which links to its decision and never edits it.

    Args:
        packet_key: The decision session's ISO date, the pair's shared key.
        decision_packet_sha256: The decision this prices.
        execution_session: The session whose open is the reference.
        captured_at: When the capture ran.
        reference_opens: `{identity: open}`, None where unpriced.
        unpriced: Identities with no valid reference open.
        source: The price provider's descriptor.
        capture_status: `COMPLETE`, `INCOMPLETE` or `UNAVAILABLE`.

    Returns:
        The packet.
    """
    return {
        "packet_key": packet_key,
        "decision_packet_sha256": decision_packet_sha256,
        "execution_session": execution_session,
        "captured_at": captured_at,
        "candidate_id": CANDIDATE_ID,
        "protocol_id": PROTOCOL_ID,
        "reference_opens": dict(sorted(reference_opens.items())),
        "unpriced": list(unpriced),
        "source": dict(source),
        "capture_status": capture_status,
        "modifies_decision_packet": False,
        "note": (
            "the reference open is what the decision would have executed at. It "
            "is recorded here and never written into the decision packet, which "
            "could not have known it."
        ),
    }


def correction_record(
    *,
    affected_packet_sha256: str,
    discovered_at: str,
    corrected_fact: str,
    reason: str,
    original_value: Any,
    new_value: Any,
    material: bool,
) -> dict[str, Any]:
    """Record a correction that changes nothing already frozen.

    Args:
        affected_packet_sha256: The packet the correction bears on.
        discovered_at: When it was found.
        corrected_fact: What the source corrected.
        reason: Why the correction was issued.
        original_value: What the packet used.
        new_value: What the source now says, where known.
        material: Whether it means the decision used information that was not
            actually valid at the decision instant.

    Returns:
        The correction record.
    """
    return {
        "affected_packet_sha256": affected_packet_sha256,
        "discovered_at": discovered_at,
        "corrected_fact": corrected_fact,
        "reason": reason,
        "original_value": original_value,
        "new_value": new_value,
        "rewrote_frozen_packet": False,
        "material": material,
        "consequence": (
            "PROSPECTIVE_TEST_INVALIDATED_BY_DEFECT"
            if material
            else "recorded; ordinary input to any later decision"
        ),
    }


@dataclass(frozen=True)
class Finalised:
    """The result of writing a packet.

    Attributes:
        path: Where it landed.
        sha256: Its digest.
        written: False when an identical packet already existed.
    """

    path: Path
    sha256: str
    written: bool


def finalise(path: Path, body: Mapping[str, Any]) -> Finalised:
    """Write a packet once, or accept an identical retry.

    Args:
        path: Where the packet belongs.
        body: The packet.

    Returns:
        The digest and whether anything was written.

    Raises:
        PacketConflictError: If the key exists with different bytes.
    """
    digest = packet_digest(body)
    canonical = json.dumps(body, indent=2, sort_keys=True) + "\n"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if packet_digest(existing) == digest:
            return Finalised(path=path, sha256=digest, written=False)
        raise PacketConflictError(
            f"{path.name} already exists with digest {packet_digest(existing)}; "
            f"refusing to overwrite it with {digest}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical, encoding="utf-8")
    return Finalised(path=path, sha256=digest, written=True)
