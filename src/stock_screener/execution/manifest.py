"""The execution manifest: which rules produced a set of orders and fills.

A simulated execution result is only meaningful alongside the policies that
produced it. The same plan under a different fill model, a different sequencing
rule or a different attribution rule gives different fills — so the manifest
binds every version, every fingerprint and the code commit, and a reader can
establish that two runs are comparable rather than assuming it.
"""

from __future__ import annotations

from typing import Any

from .anchor import JOURNAL_ANCHOR_VERSION
from .errors import EXECUTION_ERROR_CODES
from .paper_broker import VENUE
from .paper_market import paper_market_policy
from .persistence import STORE_VERSION
from .state_machine import transition_table
from .versions import (
    EXECUTION_ENGINE_VERSION,
    FILL_ATTRIBUTION_RULE,
    JOURNAL_CHAIN_VERSION,
    ORDER_SEQUENCING_RULE,
    PAPER_FILL_MODEL,
    REQUIRED_ACCOUNTING_VERSION,
    engine_policy,
)

__all__ = ["EXECUTION_MANIFEST_VERSION", "execution_manifest", "manifest_fingerprint"]

#: The manifest contract.
EXECUTION_MANIFEST_VERSION = "EXECUTION_MANIFEST_V1"


def manifest_fingerprint(body: dict[str, Any]) -> str:
    """Digest a manifest body.

    Args:
        body: The manifest without its fingerprint.

    Returns:
        SHA-256 over the canonical form.
    """
    from .fingerprint import fingerprint_of

    return fingerprint_of(
        {key: value for key, value in body.items() if key != "manifest_fingerprint"}
    )


def execution_manifest(
    *,
    execution_plan_fingerprint: str | None = None,
    journal_generation: str | None = None,
    journal_sequence: int | None = None,
    journal_head: str | None = None,
    journal_anchor_fingerprint: str | None = None,
    accounting_generation: str | None = None,
    accounting_version: str | None = None,
    initial_accounting_state: str | None = None,
    final_accounting_state: str | None = None,
    paper_snapshot_fingerprints: tuple[str, ...] = (),
    plan_report_fingerprint: str | None = None,
    code_commit: str | None = None,
) -> dict[str, Any]:
    """Collect every policy and binding behind an execution run.

    Args:
        execution_plan_fingerprint: The authorisation.
        journal_generation: The journal lineage.
        journal_sequence: The last committed journal sequence.
        journal_head: The journal's chain head.
        journal_anchor_fingerprint: The durable anchor's digest.
        accounting_generation: The book's ledger lineage.
        accounting_version: The book's engine version.
        initial_accounting_state: The book before the plan ran.
        final_accounting_state: The book afterwards.
        paper_snapshot_fingerprints: Which price snapshots were used.
        plan_report_fingerprint: The plan report.
        code_commit: The commit the engine ran from.

    Returns:
        The manifest, with its own fingerprint.
    """
    body: dict[str, Any] = {
        "manifest_version": EXECUTION_MANIFEST_VERSION,
        "policies": {
            "engine": EXECUTION_ENGINE_VERSION,
            "order_sequencing": ORDER_SEQUENCING_RULE,
            "paper_fill_model": PAPER_FILL_MODEL,
            "fill_attribution": FILL_ATTRIBUTION_RULE,
            "journal_chain": JOURNAL_CHAIN_VERSION,
            "journal_anchor": JOURNAL_ANCHOR_VERSION,
            "store": STORE_VERSION,
            "required_accounting_version": REQUIRED_ACCOUNTING_VERSION,
        },
        "engine": engine_policy(),
        "paper_market": paper_market_policy(),
        "order_lifecycle": transition_table(),
        "venue": VENUE,
        "bindings": {
            "execution_plan_fingerprint": execution_plan_fingerprint,
            "journal_generation": journal_generation,
            "journal_sequence": journal_sequence,
            "journal_head": journal_head,
            "journal_anchor_fingerprint": journal_anchor_fingerprint,
            "accounting_generation": accounting_generation,
            "accounting_version": accounting_version,
            "initial_accounting_state": initial_accounting_state,
            "final_accounting_state": final_accounting_state,
            "paper_snapshot_fingerprints": list(paper_snapshot_fingerprints),
            "plan_report_fingerprint": plan_report_fingerprint,
            "code_commit": code_commit,
        },
        "error_codes": sorted(EXECUTION_ERROR_CODES),
        "invariants": [
            "an order changes no holding, no cash and no NAV",
            "only an admitted accounting FillEvent changes the book",
            "an approved portfolio creates no order by itself",
            "an execution intent creates no order by itself",
            "every order originates from an authorised execution plan",
            "cumulative filled quantity never exceeds the ordered quantity",
            "a fill maps to exactly one accounting event, derived from its id",
            "fill attribution sums exactly to the filled quantity",
            "the broker holds no cash, no positions and no P&L",
        ],
        "refusals": [
            "an order type or time in force outside the frozen baseline",
            "an order the active plan does not authorise",
            "an execution intent stale against the current book",
            "a book of the wrong ledger generation or accounting version",
            "a new execution book under PORTFOLIO_ACCOUNTING_V1",
            "a state transition outside the frozen table",
            "a fill that would overfill its order",
            "a fill id reused with different content",
            "a market order with no execution price",
            "an execution snapshot predating the order's acknowledgement",
            "a buy the book's cash cannot fund",
            "a journal shorter than its anchor witnesses",
        ],
        "not_guaranteed": [
            (
                "COORDINATED_LOCAL_ROLLBACK_DETECTION: a journal and its local "
                "anchor rolled back together to a mutually consistent earlier "
                "state cannot be detected from local files alone. The same limit "
                "applies to the accounting ledger, and closing either needs an "
                "independently trusted external anchor."
            ),
            (
                "EXECUTION REALISM: no spread, slippage, market impact, volume "
                "participation, latency or queue position is modelled. A strategy "
                "run through this engine will look better than it would trade."
            ),
        ],
        "live_trading": "NOT_AUTHORIZED",
        "live_broker": "NOT_IMPLEMENTED",
        "external_paper_broker": "NOT_IMPLEMENTED",
    }
    body["manifest_fingerprint"] = manifest_fingerprint(body)
    return body
