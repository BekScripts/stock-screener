"""Deterministic digests for every portfolio artifact.

Two runs over the same inputs must produce the same digest, which rules out three
things that are easy to do by accident: iterating a set, depending on the order
keys were inserted into a dict, and putting the wall clock inside a semantic
hash. The last is the subtle one — an operational timestamp belongs on the
record, but if it reaches the digest then nothing can ever be compared to
anything, because every run differs.

So `digest` sorts keys, rejects sets outright, and callers keep `generated_at`
out of the body they hash. `binding` is the one place that states which inputs an
output is a function of: change any of the three and the output digest changes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["ENGINE_VERSION", "binding", "digest"]

#: The engine's identity. Not a claim that it is production-ready.
ENGINE_VERSION = "PORTFOLIO_RISK_ENGINE_V1"


def _canonical(value: Any) -> Any:
    """Convert a value to a form with exactly one serialisation.

    Args:
        value: Anything JSON-representable, plus tuples.

    Returns:
        The canonical form.

    Raises:
        TypeError: If a set is encountered. A set has no order, so hashing one
            makes the digest depend on insertion history.
    """
    if isinstance(value, set | frozenset):
        raise TypeError(
            "a set cannot be fingerprinted: its iteration order is not part of its "
            "value, so the digest would depend on how it was built. Sort it into a "
            "list first."
        )
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda p: str(p[0]))
        }
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


def digest(body: Any) -> str:
    """Digest an artifact body.

    Args:
        body: The body. Must not contain a set, and must not contain a wall-clock
            timestamp the caller expects to vary.

    Returns:
        SHA-256 over the canonical JSON form.
    """
    return hashlib.sha256(
        json.dumps(_canonical(body), sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def binding(
    *, intents_fingerprint: str, snapshot_fingerprint: str, policy_fingerprint: str
) -> dict[str, str]:
    """The three inputs every portfolio decision is a function of.

    Args:
        intents_fingerprint: The strategy intent set's digest.
        snapshot_fingerprint: The portfolio snapshot's digest.
        policy_fingerprint: The risk policy's digest.

    Returns:
        The binding, which is embedded in every output so a changed input
        necessarily changes the output digest.
    """
    return {
        "engine_version": ENGINE_VERSION,
        "strategy_intents_fingerprint": intents_fingerprint,
        "portfolio_snapshot_fingerprint": snapshot_fingerprint,
        "risk_policy_fingerprint": policy_fingerprint,
    }
