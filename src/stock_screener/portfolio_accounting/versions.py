"""The accounting version boundary, and why this one is major.

P1.3 froze `PORTFOLIO_ACCOUNTING_V1`: one weighted-average basis per security,
divided among owners by quantity. P1.4 introduces per-attribution cost pools,
and that is not an additive change. Consider two strategies entering the same
security at different prices and one of them selling:

                        V1 (pooled)     V2 (per-attribution)
    global realised         15                  20
    global remaining basis  15                  20

The *portfolio's own* figures move, not merely their division across owners. The
risk engine's `V1_1`/`V1_2` pattern exists for changes that reproduce the earlier
version byte for byte; this one cannot, so it takes a major version.

Both versions remain executable. A V1 ledger replays under V1 forever and
reproduces its golden output exactly; V2 is selected explicitly, per ledger. The
version is therefore a property of a ledger generation rather than of the
installed code — which is what makes "this NAV was computed under these rules" a
statement anyone can check later rather than an assumption about when the books
were opened.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from .errors import AccountingError, Code

__all__ = [
    "ACCOUNTING_V1",
    "ACCOUNTING_V2",
    "DEFAULT_ACCOUNTING_VERSION",
    "AccountingVersion",
    "resolve_version",
    "version_policy",
]

ACCOUNTING_V1 = "PORTFOLIO_ACCOUNTING_V1"
ACCOUNTING_V2 = "PORTFOLIO_ACCOUNTING_V2"

#: V1 stays the default so every ledger and fixture written before P1.4 keeps its
#: meaning without carrying a version field it could not have known to write. New
#: ledgers opt into V2 explicitly. Defaulting to V2 would silently restate
#: existing books under rules they were not opened under.
DEFAULT_ACCOUNTING_VERSION = ACCOUNTING_V1


class AccountingVersion(StrEnum):
    """Which accounting rules a ledger generation runs under."""

    V1 = ACCOUNTING_V1
    """Global weighted-average cost. Frozen; reproduces P1.3 byte for byte."""

    V2 = ACCOUNTING_V2
    """Per-attribution weighted-average cost pools. Current."""

    @property
    def uses_cost_pools(self) -> bool:
        """Whether this version keeps a basis per attribution owner."""
        return self is AccountingVersion.V2

    @property
    def basis_policy(self) -> str:
        """The cost-basis convention this version applies."""
        from .pools import PER_ATTRIBUTION_COST_POLICY
        from .positions import COST_BASIS_POLICY

        return PER_ATTRIBUTION_COST_POLICY if self.uses_cost_pools else COST_BASIS_POLICY


def resolve_version(value: str | AccountingVersion | None) -> AccountingVersion:
    """Resolve a declared accounting version.

    Args:
        value: The version string, an `AccountingVersion`, or None for the
            default.

    Returns:
        The version.

    Raises:
        AccountingError: `ACCOUNTING_VERSION_UNKNOWN` for an unrecognised
            version. A ledger naming rules this build does not have must not be
            replayed under whatever rules it does have — the figures would be
            wrong and would look right.
    """
    if value is None:
        return AccountingVersion(DEFAULT_ACCOUNTING_VERSION)
    if isinstance(value, AccountingVersion):
        return value
    try:
        return AccountingVersion(str(value))
    except ValueError as exc:
        raise AccountingError(
            Code.ACCOUNTING_VERSION_UNKNOWN,
            f"{value!r} is not an accounting version this build implements "
            f"(have: {', '.join(sorted(v.value for v in AccountingVersion))}). "
            "Replaying it under different rules would produce figures that are "
            "wrong and look right.",
        ) from exc


def version_policy() -> dict[str, Any]:
    """Describe the version boundary.

    Returns:
        The policy, for the manifest.
    """
    return {
        "default": DEFAULT_ACCOUNTING_VERSION,
        "current": ACCOUNTING_V2,
        "versions": {
            ACCOUNTING_V1: {
                "basis": "GLOBAL_WEIGHTED_AVERAGE — one pool per security",
                "status": "FROZEN",
                "per_strategy_realised": (
                    "exact only when co-owners entered at the same price; this is "
                    "the limitation P1.4 exists to close"
                ),
            },
            ACCOUNTING_V2: {
                "basis": "PER_ATTRIBUTION_WEIGHTED_AVERAGE — one pool per (identity, owner)",
                "status": "CURRENT",
                "per_strategy_realised": (
                    "exact under this internal accounting convention, for every supported event"
                ),
            },
        },
        "why_major": (
            "per-attribution basis moves global realised P&L and global cost "
            "basis, not only their division across owners, so the earlier version "
            "cannot be reproduced from the new rules"
        ),
        "tax_accounting": (
            "NOT_IMPLEMENTED — no FIFO, LIFO, specific lots, holding periods or wash sales"
        ),
        "migration": "REPLAY_MIGRATION_FROM_EVENTS — see migration.py",
    }
