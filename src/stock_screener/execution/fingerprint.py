"""One canonical digest for every execution artefact.

Execution artefacts bind to each other by fingerprint: an order names the plan
that authorised it, a fill names the order, a report names the plan and the book.
Those bindings are only worth anything if the digest is stable — so it is
computed one way, here, over a canonical JSON form with `Decimal` rendered as its
exact string.

Wall clocks stay out of digest bodies. A fingerprint that moved with the time of
day could never establish that two artefacts describe the same thing, which is
the only question a fingerprint exists to answer.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from .errors import Code, ExecutionError

__all__ = ["canonical", "fingerprint_of"]


def canonical(value: Any) -> Any:
    """Reduce a value to its canonical, order-stable form.

    Args:
        value: Anything JSON-representable, plus `Decimal`.

    Returns:
        The canonical form.

    Raises:
        ExecutionError: If the value contains a set. A set's iteration order is
            not part of its value, so a digest over one would depend on insertion
            history rather than on content.
    """
    if isinstance(value, set | frozenset):
        raise ExecutionError(
            Code.EXECUTION_EVENT_INVALID,
            "a set cannot be fingerprinted: its order is not part of its value",
        )
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): canonical(v) for k, v in sorted(value.items(), key=lambda p: str(p[0]))}
    if isinstance(value, list | tuple):
        return [canonical(item) for item in value]
    return value


def fingerprint_of(body: Any) -> str:
    """Digest an execution artefact's body.

    Args:
        body: The artefact's semantic content, without any wall clock.

    Returns:
        SHA-256 over the canonical JSON form.
    """
    return hashlib.sha256(
        json.dumps(canonical(body), sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
