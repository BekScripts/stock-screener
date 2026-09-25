"""`PROSPECTIVE_SHADOW_CONTRACT_V1` — the bridge to data that does not exist yet.

The historical contract governs a frozen dataset assembled after the fact. A
live stream is a different thing arriving under different conditions, and
assuming the one silently governs the other is how a prospective test quietly
stops being prospective. So the bridge is written out: for each rule, what the
historical contract required, and what the equivalent obligation is on a stream
being observed in real time.

The asymmetry worth naming is direction of error. Historically, a late-arriving
filing simply was not in the dataset. Prospectively, it may arrive minutes after
the decision instant and be sitting there when anyone looks — so the obligation
becomes an active one: capture what was knowable at the instant, and never let a
later arrival edit it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["PROSPECTIVE_CONTRACT_ID", "prospective_contract"]

#: The contract a prospective observation is taken under.
PROSPECTIVE_CONTRACT_ID = "PROSPECTIVE_SHADOW_CONTRACT_V1"


def prospective_contract() -> dict[str, Any]:
    """Build the prospective data contract.

    Returns:
        The contract, as a JSON-safe mapping.
    """
    return {
        "contract_id": PROSPECTIVE_CONTRACT_ID,
        "derived_from": "BACKTEST_DATA_CONTRACT_V1",
        "status": "FROZEN — definition only; no observation has been taken under it",
        "why_it_exists": (
            "the historical contract governs a dataset assembled after the fact. A "
            "live stream arrives under different conditions, and assuming the "
            "historical contract automatically governs it is how a prospective test "
            "stops being prospective without anyone deciding to change it."
        ),
        "decision_time_availability": {
            "instant": "16:00 America/New_York on the quarter-end benchmark session",
            "rule": (
                "only information the historical contract would have admitted at "
                "that instant may take part in the decision"
            ),
            "company_evidence": "accepted strictly before the decision instant",
            "historical_securities": "filed on or before the decision session, with "
            "the same conservative lag the historical layer applies",
            "forbidden": [
                "a filing accepted after the decision instant",
                "a next-day filing",
                "a later correction or restatement",
                "any price after the decision session's close",
                "any forward return",
            ],
            "direction_of_error": (
                "historically a late filing was simply absent from the dataset. "
                "Prospectively it may arrive minutes later and be present when "
                "anyone looks, so the obligation is active: capture what was "
                "knowable at the instant and freeze it."
            ),
        },
        "identity_key": {
            "key": "(kind, identity_id) with kind in {COMPANY, HISTORICAL}",
            "ticker": "a dated display attribute, never an identity",
            "rule": "unchanged from the historical layer",
        },
        "feature_timestamp_rules": {
            "score_version": "COMPOUNDER_V1_2, frozen",
            "features": "MARKET_FEATURES_V1 as of the decision session",
            "rule": "a feature carries the timestamp of the observation that "
            "produced it, and a feature with no timestamp is unusable",
        },
        "price_source": {
            "decision_session": "the benchmark session's official close",
            "execution_reference": "the next benchmark session's official open",
            "requirement": (
                "one designated prospective price source, named in the protocol "
                "before activation and unchanged during a window"
            ),
            "substitution": "NONE — see missingness",
        },
        "missingness": {
            "rule": "missing is not zero, in the prospective stream exactly as in "
            "the historical one",
            "unscored": "an identity without a score is not rankable and not selectable",
            "unpriced_at_execution": (
                "recorded as UNPRICED for that identity, applied identically to "
                "both strategies, and documented in the decision packet. No other "
                "price is substituted — not the prior close, not the decision "
                "close, not a later print."
            ),
        },
        "execution_reference": {
            "rule": "CLOSE_SIGNAL_NEXT_SESSION_OPEN, unchanged",
            "fills": (
                "reference prices only. This is a shadow stream: nothing is sent "
                "anywhere, so there is no fill to model beyond the reference open."
            ),
            "forbidden": [
                "a same-close fill",
                "an invented or interpolated fill",
                "a previous-close substitution",
            ],
        },
        "corporate_actions": {
            "rule": "the prospective equivalent of the historical treatment: "
            "adjusted prices carry splits and dividends, and no statement is "
            "restated into another currency",
            "limitation": ("spinoffs are unsupported historically and remain unsupported here"),
        },
        "terminal_treatment": {
            "policy": "the prospective equivalent of TERMINAL_LEG_POLICY_V1, with "
            "identical semantics where the event type is supported",
            "unsupported_event": (
                "recorded as a limitation with the position carried at its last "
                "observed value and marked UNKNOWN. A terminal value is never "
                "improvised, and never defaulted to zero."
            ),
        },
        "corrections": {
            "rule": "POST_DECISION_CORRECTION",
            "effect_on_a_frozen_packet": "NONE — a frozen decision packet is never rewritten",
            "effect_on_later_decisions": "a correction is ordinary input to any "
            "decision taken after it became available",
        },
        "storage": {
            "stream": "PROSPECTIVE_SHADOW_STREAM_V1",
            "must_not_write_into": "PIT_RESEARCH_DATASET_V2",
            "rule": "historical and prospective observations stay distinguishable "
            "by namespace, never merged into one dataset",
        },
        "benchmark": {
            "identity": "SPY",
            "rule": "the same passive benchmark the historical research used. It is "
            "not changed because another index later performs differently.",
        },
        "cost_sensitivity": {
            "model": "SIMPLE_10BPS_PER_SIDE_V1",
            "role": "descriptive continuity only; it affects no prospective criterion",
        },
        "excluded_inputs": {
            "MARKET_SIGNALS_V1": "H2 does not use it to select membership and it is not added here",
            "regime_filter": "NONE",
            "market_timing": "NONE",
            "sector_or_industry": "NONE — H2 is sector-agnostic and sector history "
            "remains unavailable",
        },
    }
