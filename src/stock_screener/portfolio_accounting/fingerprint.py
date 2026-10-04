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
from .errors import ANCHOR_ERROR_CODES, V1_ERROR_CODES, V2_ERROR_CODES
from .ledger import LEDGER_CHAIN_VERSION
from .money import MONEY_POLICY_ID, money_policy
from .persistence import STORE_VERSION
from .pnl import PNL_POLICY_ID
from .positions import COST_BASIS_POLICY
from .reconciliation import RECONCILIATION_POLICY
from .valuation import DEFAULT_STALENESS_LIMIT_HOURS, MARK_POLICY_ID
from .versions import AccountingVersion, resolve_version, version_policy

__all__ = [
    "ACCOUNTING_MANIFEST_VERSION",
    "ACCOUNTING_MANIFEST_VERSION_V2",
    "accounting_manifest",
    "manifest_fingerprint",
]

#: The V1 manifest contract, frozen with P1.3.
ACCOUNTING_MANIFEST_VERSION = "PORTFOLIO_ACCOUNTING_MANIFEST_V1"

#: The V2 manifest contract. It adds the attribution convention, the ledger
#: generation and the head anchor, so it is a separate version rather than extra
#: keys on V1 — a V1 manifest must keep describing the V1 engine exactly.
ACCOUNTING_MANIFEST_VERSION_V2 = "PORTFOLIO_ACCOUNTING_MANIFEST_V1_1"


def accounting_manifest(
    *,
    accounting_version: str | None = None,
    ledger_generation: str | None = None,
    ledger_sequence: int | None = None,
    ledger_head: str | None = None,
    head_anchor_fingerprint: str | None = None,
    state_fingerprint: str | None = None,
    valuation_fingerprint: str | None = None,
    code_commit: str | None = None,
) -> dict[str, Any]:
    """Collect every policy version the accounting engine ran under.

    Args:
        accounting_version: Which engine version this manifest describes.
            Defaults to V1, so an existing ledger's manifest is unchanged.
        ledger_generation: The ledger lineage, under V2.
        ledger_sequence: The last committed sequence, under V2.
        ledger_head: The ledger's chain head, under V2.
        head_anchor_fingerprint: The durable anchor's digest, under V2.
        state_fingerprint: The replayed state's digest, under V2.
        valuation_fingerprint: The valuation's digest, under V2.
        code_commit: The commit the engine ran from, under V2.

    Returns:
        The manifest, with its own fingerprint.
    """
    version = resolve_version(accounting_version)
    if version.uses_cost_pools:
        return _manifest_v2(
            version=version,
            ledger_generation=ledger_generation,
            ledger_sequence=ledger_sequence,
            ledger_head=ledger_head,
            head_anchor_fingerprint=head_anchor_fingerprint,
            state_fingerprint=state_fingerprint,
            valuation_fingerprint=valuation_fingerprint,
            code_commit=code_commit,
        )
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
        "error_codes": sorted(V1_ERROR_CODES),
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


def _manifest_v2(
    *,
    version: AccountingVersion,
    ledger_generation: str | None,
    ledger_sequence: int | None,
    ledger_head: str | None,
    head_anchor_fingerprint: str | None,
    state_fingerprint: str | None,
    valuation_fingerprint: str | None,
    code_commit: str | None,
) -> dict[str, Any]:
    """Build the V2 manifest.

    It binds what V1's could not: the attribution convention, which ledger
    lineage the figures came from, and the durable head anchor that asserts the
    committed tail is complete. A NAV that cannot name its anchor cannot claim
    to have been computed over the whole ledger.

    Args:
        version: The engine version.
        ledger_generation: The ledger lineage.
        ledger_sequence: The last committed sequence.
        ledger_head: The ledger's chain head.
        head_anchor_fingerprint: The durable anchor's digest.
        state_fingerprint: The replayed state's digest.
        valuation_fingerprint: The valuation's digest.
        code_commit: The commit the engine ran from.

    Returns:
        The manifest, with its own fingerprint.
    """
    from .anchor import ANCHOR_VERSION
    from .pools import PER_ATTRIBUTION_COST_POLICY

    policies = {
        "accounting_engine": str(version),
        "ledger_chain": LEDGER_CHAIN_VERSION,
        "head_anchor": ANCHOR_VERSION,
        "money": MONEY_POLICY_ID,
        "cost_basis": PER_ATTRIBUTION_COST_POLICY,
        "mark_admissibility": MARK_POLICY_ID,
        "pnl_decomposition": PNL_POLICY_ID,
        "strategy_attribution": ATTRIBUTION_POLICY,
        "corporate_actions": CORPORATE_ACTION_POLICY,
        "reconciliation": RECONCILIATION_POLICY,
        "store": STORE_VERSION,
        "risk_adapter": RISK_ADAPTER_VERSION,
    }
    body: dict[str, Any] = {
        "manifest_version": ACCOUNTING_MANIFEST_VERSION_V2,
        "policies": dict(sorted(policies.items())),
        "attribution_convention": PER_ATTRIBUTION_COST_POLICY,
        "tax_accounting": (
            "NOT_IMPLEMENTED — no FIFO, LIFO, specific-lot identification, "
            "holding periods or wash-sale rules. This is an internal accounting "
            "basis, not a tax basis."
        ),
        "ledger": {
            "generation": ledger_generation,
            "sequence": ledger_sequence,
            "head": ledger_head,
            "head_anchor_fingerprint": head_anchor_fingerprint,
        },
        "state_fingerprint": state_fingerprint,
        "valuation_fingerprint": valuation_fingerprint,
        "code_commit": code_commit,
        "money": money_policy(),
        "corporate_actions": action_policy(),
        "risk_seam": seam_policy(),
        "versions": version_policy(),
        "staleness_limit_hours": DEFAULT_STALENESS_LIMIT_HOURS,
        "error_codes": sorted(V2_ERROR_CODES),
        "anchor_error_codes": sorted(ANCHOR_ERROR_CODES),
        "invariants": [
            "cash = opening + deposits - withdrawals - purchases + sales - fees + dividends",
            "NAV = cash + market value of holdings",
            "total P&L = realised + unrealised + income - fees",
            (
                "NAV - inception NAV at cost = total P&L + net external flows - "
                "unpriced cost basis (zero for a complete valuation)"
            ),
            "attribution sums to the quantity held, per security",
            "global quantity = sum of attribution-pool quantities",
            "global cost basis = sum of attribution-pool cost bases",
            "global realised trading P&L = sum of attribution-pool realised P&L",
            "global unrealised P&L = sum of attribution-pool unrealised P&L",
        ],
        "refusals": [
            "a buy the cash balance cannot fund",
            "a sale beyond the quantity held",
            "a sale beyond one owner's own pool, even when the security has enough",
            "a sale whose ownership is ambiguous and unstated",
            "a mark stamped after the valuation instant",
            "a valuation missing any held position",
            "a corporate action whose economics are not modelled",
            "an event id reused with a different payload",
            "an event taking effect before the ledger's last event",
            "a second opening balance",
            "overwriting a stored ledger record",
            "a store rooted in a research or evidence namespace",
            "an anchor naming a sequence the ledger does not reach",
            "an anchor disagreeing with the ledger head at its own sequence",
            "a ledger replayed under an accounting version this build lacks",
        ],
        "never_does": [
            "place or cancel an order",
            "simulate a fill",
            "count an external cash flow as P&L",
            "repair a reconciliation break by adopting the external figure",
            "infer a dividend entitlement from current holdings",
            "write a risk target into a holding",
            "select a tax lot",
            "rewrite a head anchor backward",
        ],
        "not_guaranteed": [
            (
                "COORDINATED_LOCAL_ROLLBACK_DETECTION: a ledger and its anchor "
                "rolled back together to a mutually consistent earlier state "
                "cannot be detected from local files alone. An independently "
                "trusted external anchor is required, and is not part of P1.4."
            ),
            (
                "per-strategy dividend income, where the event states only an "
                "aggregate entitlement; global dividend income stays exact"
            ),
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
