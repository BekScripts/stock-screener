"""Durable operational control state, in its own namespace.

Control state is small and load-bearing: a kill switch, an authorization, a run
lease, a journal head anchor. Each is a few hundred bytes that decide whether
this system is allowed to trade, which makes a half-written one more dangerous
than a missing one — so every write here is flushed, fsynced, atomically
renamed, and then **read back and compared** before it is treated as committed.
The read-back is the part that is easy to skip and the reason this module exists
separately: a kill switch that was written but not verified is a kill switch
that might not be there after a power loss.

The namespace is separate too. Operational records never share a root with SEC
evidence, the point-in-time research corpus, the accounting ledger or the
execution journal. Those are different trust domains with different retention
rules, and a control-state file sitting among them would eventually be swept up
by a cleanup written for the other kind.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import Code, OperationsError

__all__ = [
    "FORBIDDEN_OPERATIONS_ROOTS",
    "OPERATIONS_DIRECTORY",
    "atomic_write_verified",
    "guard_operations_root",
    "read_json",
]

#: Where operational control state lives, relative to a caller's root.
OPERATIONS_DIRECTORY = ".operations"

#: Namespaces operational state must never be written into. Each is a different
#: trust domain, and a control file among them would be swept up by a cleanup
#: written for the other kind.
FORBIDDEN_OPERATIONS_ROOTS: tuple[str, ...] = (
    ".evidence",
    "pit_research_dataset",
    "research/r1_",
    "stage9k",
    "compounder_radar.db",
    ".portfolio",
    ".execution/journal",
)


def guard_operations_root(root: Path) -> None:
    """Refuse an operational root that would contaminate another domain.

    Args:
        root: The proposed root.

    Raises:
        OperationsError: `FORBIDDEN_OPERATIONS_NAMESPACE` if the path lies in a
            research, evidence, accounting or execution-journal namespace.
    """
    lowered = str(root).replace("\\", "/").lower()
    for fragment in FORBIDDEN_OPERATIONS_ROOTS:
        if fragment in lowered:
            raise OperationsError(
                Code.FORBIDDEN_OPERATIONS_NAMESPACE,
                f"{root} lies in the {fragment!r} namespace. Operational control "
                "state is a different trust domain from research evidence, the "
                "accounting ledger and the execution journal, and mixing them "
                "means a cleanup written for one will eventually delete the other.",
            )


def atomic_write_verified(path: Path, payload: Any) -> None:
    """Write JSON durably, then read it back and confirm it is there.

    Flush, `fsync`, atomic rename, `fsync` the directory — and then re-read and
    compare. The read-back is not belt-and-braces: for a kill switch, "I wrote
    it" and "it is there" are different claims, and only the second one is worth
    anything after a power loss.

    Args:
        path: Where the record goes.
        payload: What to write.

    Raises:
        OperationsError: `OPERATIONS_PARTIAL_RECORD` if the write could not be
            made durable or does not read back equal. The temporary file is
            removed, so a failed write leaves nothing that looks like a record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise OperationsError(
            Code.OPERATIONS_PARTIAL_RECORD,
            f"{path.name} could not be written durably: {exc}. Nothing was left "
            "behind, so the control state is still whatever it was before.",
        ) from exc

    written = read_json(path)
    if written != json.loads(json.dumps(payload, sort_keys=True)):
        raise OperationsError(
            Code.OPERATIONS_PARTIAL_RECORD,
            f"{path.name} did not read back as written. A control-state record "
            "that cannot be verified is treated as absent, which fails closed.",
        )


def read_json(path: Path) -> dict[str, Any]:
    """Read one control record.

    Args:
        path: The record.

    Returns:
        Its content.

    Raises:
        OperationsError: `CONTROL_STATE_UNVERIFIABLE` if it cannot be parsed or
            is not an object. Deliberately *not* treated as "no record": a
            corrupt kill switch must fail closed, and returning an empty mapping
            here would make corruption indistinguishable from permission.
    """
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OperationsError(
            Code.CONTROL_STATE_UNVERIFIABLE,
            f"{path.name} could not be read as operational control state: {exc}. "
            "An unreadable control record is treated as the unsafe case, never as "
            "an absent one.",
        ) from exc
    if not isinstance(body, dict):
        raise OperationsError(
            Code.CONTROL_STATE_UNVERIFIABLE,
            f"{path.name} does not hold an operational control record",
        )
    return body
