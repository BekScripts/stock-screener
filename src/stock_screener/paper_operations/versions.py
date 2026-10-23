"""The operations control plane's frozen contracts.

Versioned apart from the execution engine on purpose. An operational
coordinator can change how often it runs, what it checks before running and how
it records what it did, without changing what a fill *is* — and the two must be
able to move independently or every scheduling tweak would invalidate a
reproducible execution artefact.

So nothing here touches `EXECUTION_ENGINE_V1_2` or `PORTFOLIO_ACCOUNTING_V2`.
If an operational requirement ever seems to need an execution-semantic change,
that is a signal to stop rather than a reason to bump this version.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "INCIDENT_POLICY",
    "KILL_SWITCH_CONTRACT",
    "OPERATIONS_AUTHORIZATION_CONTRACT",
    "OPERATIONS_JOURNAL_ANCHOR_VERSION",
    "OPERATIONS_JOURNAL_CHAIN_VERSION",
    "OPERATIONS_STORE_VERSION",
    "PAPER_OPERATIONS_ENGINE_VERSION",
    "REQUIRED_ACCOUNTING_VERSION",
    "REQUIRED_EXECUTION_VERSION",
    "RUN_ID_DERIVATION",
    "RUN_LEASE_CONTRACT",
    "SCHEDULE_CONTRACT",
    "operations_policy",
]

#: The operations contract. P2.3.
PAPER_OPERATIONS_ENGINE_VERSION = "PAPER_OPERATIONS_ENGINE_V1"

#: Which execution engine this control plane operates. Named rather than
#: inferred: an operations run binds the execution version it drove, so a later
#: reader can tell whether the fills in a run report came from the engine whose
#: semantics they think they are reading.
REQUIRED_EXECUTION_VERSION = "EXECUTION_ENGINE_V1_2"

#: Which accounting rules the books must be under.
REQUIRED_ACCOUNTING_VERSION = "PORTFOLIO_ACCOUNTING_V2"

#: How an operations run id is derived: from the run's semantic inputs, never
#: generated. Retrying the same scheduled slot must recover the same run rather
#: than start a second one, and only a derivation gives that.
RUN_ID_DERIVATION = "DERIVED_OPERATIONS_RUN_ID_SHA256_V1"

#: The schedule contract.
SCHEDULE_CONTRACT = "PAPER_OPERATIONS_SCHEDULE_V1"

#: The arming contract.
OPERATIONS_AUTHORIZATION_CONTRACT = "PAPER_OPERATIONS_AUTHORIZATION_V1"

#: The kill-switch contract. Persistent, and safe when unreadable.
KILL_SWITCH_CONTRACT = "PERSISTENT_PAPER_KILL_SWITCH_V1"

#: The run-lease contract.
RUN_LEASE_CONTRACT = "PAPER_OPERATIONS_RUN_LEASE_V1"

#: The run journal's hash-chain contract.
OPERATIONS_JOURNAL_CHAIN_VERSION = "PAPER_OPERATIONS_JOURNAL_HASH_CHAIN_V1"

#: The run journal's head-anchor contract. Without an anchor a chain cannot
#: detect its own truncation; this is the same lesson P1.4 and P2.0 recorded.
OPERATIONS_JOURNAL_ANCHOR_VERSION = "PAPER_OPERATIONS_JOURNAL_HEAD_ANCHOR_V1"

#: The operational store contract.
OPERATIONS_STORE_VERSION = "PAPER_OPERATIONS_STORE_V1"

#: How incidents behave: recorded and gating, never repairing.
INCIDENT_POLICY = "APPEND_ONLY_INCIDENTS_NO_AUTO_REPAIR_V1"


def operations_policy() -> dict[str, Any]:
    """Describe every frozen operational contract.

    Returns:
        The policy, for the manifest and for anyone asking what this control
        plane is allowed to do.
    """
    return {
        "operations_engine_version": PAPER_OPERATIONS_ENGINE_VERSION,
        "required_execution_version": REQUIRED_EXECUTION_VERSION,
        "required_accounting_version": REQUIRED_ACCOUNTING_VERSION,
        "run_id_derivation": RUN_ID_DERIVATION,
        "schedule": SCHEDULE_CONTRACT,
        "authorization": OPERATIONS_AUTHORIZATION_CONTRACT,
        "kill_switch": KILL_SWITCH_CONTRACT,
        "run_lease": RUN_LEASE_CONTRACT,
        "journal_chain": OPERATIONS_JOURNAL_CHAIN_VERSION,
        "journal_anchor": OPERATIONS_JOURNAL_ANCHOR_VERSION,
        "store": OPERATIONS_STORE_VERSION,
        "incidents": INCIDENT_POLICY,
        "default_state": "NOT_ARMED",
        "may": [
            "schedule a run",
            "gate a run on health",
            "pause and kill",
            "recover a crashed run",
            "require reconciliation",
            "record incidents",
            "cancel an open order it can prove it owns",
        ],
        "may_never": [
            "invent a target, an order, a fill, a position or a cash balance",
            "grant a risk approval",
            "authorise a security",
            "repair a reconciliation break",
            "liquidate a position",
            "cancel an order it cannot prove it owns",
            "fall back to the local simulator when a venue is unreachable",
            "backfill a missed run",
            "trade on stale input",
            "reach a live venue",
        ],
        "live_trading": "NOT_AUTHORIZED",
        "what_this_is": (
            "an observer and a gate. It decides whether the system may act, "
            "never what the action should be."
        ),
        "not_guaranteed": [
            (
                "COORDINATED_LOCAL_ROLLBACK_DETECTION: the run journal and its "
                "local anchor rolled back together to a mutually consistent "
                "earlier state cannot be detected from local files alone. The "
                "same limit applies to the execution journal and the accounting "
                "ledger, and an independently trusted anchor would close all "
                "three."
            ),
            (
                "OPERATIONAL CONTROL IS NOT EXECUTION QUALITY: this plane can "
                "establish that a run was permitted, reconciled and recorded. It "
                "says nothing about whether the orders it permitted were good "
                "ones."
            ),
        ],
    }
