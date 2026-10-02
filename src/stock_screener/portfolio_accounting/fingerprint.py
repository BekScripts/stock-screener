"""The accounting manifest: what rules produced a set of books.

A NAV is only meaningful alongside the policies that computed it. The same ledger
under FIFO instead of weighted-average cost, or with fees capitalised instead of
expensed, or with a wider corporate-action policy, produces a different and
equally internally-consistent portfolio. A figure that cannot name its rules
cannot be compared with one computed later, and the comparison is usually the
whole point.

So every policy version in the package is collected into one manifest with a
digest over it. Two sets of books agree on their rules exactly when their
manifest fingerprints match, and a change to any policy moves the fingerprint —
which makes a silent policy drift impossible rather than merely unlikely.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .accounting import ACCOUNTING_ENGINE_VERSION
from .adapter import RISK_ADAPTER_VERSION, seam_policy
from .attribution import ATTRIBUTION_POLICY
from .corporate_actions import CORPORATE_ACTION_POLICY, action_policy
from .errors import ACCOUNTING_ERROR_CODES
from .ledger import LEDGER_CHAIN_VERSION
from .money import MONEY_POLICY_ID, money_policy
from .persistence import STORE_VERSION
from .pnl import PNL_POLICY_ID
from .positions import COST_BASIS_POLICY
from .reconciliation import RECONCILIATION_POLICY
from .valuation import DEFAULT_STALENESS_LIMIT_HOURS, MARK_POLICY_ID

__all__ = [
    "ACCOUNTING_MANIFEST_VERSION",
    "accounting_manifest",
    "manifest_fingerprint",
]

#: The manifest contract.
ACCOUNTING_MANIFEST_VERSION = "PORTFOLIO_ACCOUNTING_MANIFEST_V1"


def accounting_manifest() -> dict[str, Any]:
    """Collect every policy version in the accounting engine.

    Returns:
        The manifest, with its own fingerprint.
    """
    policies = {
        "accounting_engine": ACCOUNTING_ENGINE_VERSION,
        "ledger_chain": LEDGER_CHAIN_VERSION,
        "money": MONEY_POLICY_ID,
        "cost_basis": COST_BASIS_POLICY,
        "mark_admissibility": MARK_POLICY_ID,
        "pnl_decomposition": PNL_POLICY_ID,
        "strategy_attribution": ATTRIBUTION_POLICY,
        "corporate_actions": CORPORATE_ACTION_POLICY,
        "reconciliation": RECONCILIATION_POLICY,
        "store": STORE_VERSION,
        "risk_adapter": RISK_ADAPTER_VERSION,
    }
    body: dict[str, Any] = {
        "manifest_version": ACCOUNTING_MANIFEST_VERSION,
        "policies": dict(sorted(policies.items())),
        "money": money_policy(),
        "corporate_actions": action_policy(),
        "risk_seam": seam_policy(),
        "staleness_limit_hours": DEFAULT_STALENESS_LIMIT_HOURS,
        "error_codes": sorted(ACCOUNTING_ERROR_CODES),
        "invariants": [
            "cash = opening + deposits - withdrawals - purchases + sales - fees + dividends",
            "NAV = cash + market value of holdings",
            "total P&L = realised + unrealised + income - fees",
            (
                "NAV - inception NAV at cost = total P&L + net external flows - "
                "unpriced cost basis (zero for a complete valuation)"
            ),
            "attribution sums to the quantity held, per security",
        ],
        "refusals": [
            "a buy the cash balance cannot fund",
            "a sale beyond the quantity held",
            "a sale whose ownership is ambiguous and unstated",
            "a mark stamped after the valuation instant",
            "a valuation missing any held position",
            "a corporate action whose economics are not modelled",
            "an event id reused with a different payload",
            "an event taking effect before the ledger's last event",
            "a second opening balance",
            "overwriting a stored ledger record",
            "a store rooted in a research or evidence namespace",
        ],
        "never_does": [
            "place or cancel an order",
            "simulate a fill",
            "count an external cash flow as P&L",
            "repair a reconciliation break by adopting the external figure",
            "infer a dividend entitlement from current holdings",
            "write a risk target into a holding",
        ],
    }
    body["manifest_fingerprint"] = manifest_fingerprint(body)
    return body


def manifest_fingerprint(body: dict[str, Any]) -> str:
    """Digest a manifest body.

    Args:
        body: The manifest without its fingerprint.

    Returns:
        SHA-256 over its canonical JSON form.
    """
    without = {key: value for key, value in body.items() if key != "manifest_fingerprint"}
    return hashlib.sha256(
        json.dumps(without, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
