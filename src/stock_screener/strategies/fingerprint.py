"""Digests for the strategy layer.

There is deliberately no new hashing function here. `fingerprint_of` is the one
the execution, accounting and operations layers already use, and a second digest
convention would make two records of the same object disagree about its
identity. This module exists to say that explicitly and to name what gets
digested.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["FINGERPRINT_SCHEME", "strategy_fingerprint_of"]

#: Which scheme digests a strategy artefact. The same one the rest of the system
#: uses, named here so a reader does not have to go and check.
FINGERPRINT_SCHEME = "CANONICAL_JSON_SHA256_V1"


def strategy_fingerprint_of(body: Mapping[str, Any]) -> str:
    """Digest a strategy artefact's body.

    Args:
        body: The artefact's semantic content. Must be JSON-safe and must
            exclude operational metadata — a timestamp of when something was
            read belongs outside the digest, or two reads of one decision would
            fingerprint differently.

    Returns:
        The digest.
    """
    from stock_screener.execution import fingerprint_of

    return fingerprint_of(dict(body))
