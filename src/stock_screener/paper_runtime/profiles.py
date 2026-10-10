"""What this deployment's timing actually is, written down so nobody assumes.

Two facts about operating this strategy for real differ from the research
convention it came from. Neither is a defect and both are conservative, but both
would be easy to forget in six months when somebody compares a paper track
record against a backtest and finds a gap. So they are artefacts with
fingerprints rather than paragraphs in a commit message.

The honest summary: this deployment decides on slightly older information than
the research convention assumes, and executes slightly later in the session than
it assumes. Both directions cost return rather than manufacturing it, which is
why they are acceptable — and both make a direct paper-versus-backtest
comparison invalid, which is why they are recorded.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "OPERATIONAL_EXECUTION_TIMING_VERSION",
    "OPERATIONAL_SCORE_AVAILABILITY_VERSION",
    "PERFORMANCE_ATTRIBUTION_BOUNDARY",
    "execution_timing_profile",
    "score_availability_profile",
]

#: The score-availability profile contract.
OPERATIONAL_SCORE_AVAILABILITY_VERSION = "OPERATIONAL_COMPOUNDER_SCORE_AVAILABILITY_V1"

#: The execution-timing profile contract.
OPERATIONAL_EXECUTION_TIMING_VERSION = "OPERATIONAL_EXECUTION_TIMING_V1"

#: The one sentence both profiles exist to support.
PERFORMANCE_ATTRIBUTION_BOUNDARY = (
    "No direct comparison between this deployment's results and any research "
    "backtest is authorised. The deployment decides on older information and "
    "executes later in the session than the research convention assumes, so a "
    "difference in outcome is not evidence about the strategy. An apples-to-apples "
    "comparison needs a research run using operational timing, which is a separate "
    "phase."
)


def score_availability_profile() -> dict[str, Any]:
    """Describe what information a decision can actually have.

    Returns:
        The profile. Its fingerprint is bound into the runtime, so a later
        change to the operational score timing has to be a deliberate rebinding
        rather than something that quietly takes effect.
    """
    return {
        "version": OPERATIONAL_SCORE_AVAILABILITY_VERSION,
        "score_version": "COMPOUNDER_V1_2",
        "source": "ScoreSnapshotRepository, coverage=MARKET",
        "decision_cutoff": "16:00 America/New_York on the decision session",
        "admissibility_rule": "score calculated_at <= decision cutoff",
        "retroactive_claim": (
            "FORBIDDEN — a score computed after the cutoff is never treated as the "
            "cutoff's evidence, and no older snapshot is substituted for an "
            "inadmissible one"
        ),
        "latest_underlying_market_bar": (
            "MAY PRECEDE THE DECISION SESSION'S CLOSE. A score admissible at the "
            "16:00 cutoff must have been computed before it, and a score computed "
            "before the close can only have read the previous session's closing "
            "prices. Observed in production: score_date 2026-08-26 was computed at "
            "07:40 UTC (03:40 New York) and the newest stored price bar was "
            "2026-08-25"
        ),
        "direction": (
            "CONSERVATIVE — the decision uses strictly less information than the "
            "research convention assumes, so it cannot look ahead"
        ),
        "research_convention": (
            "signal at the last session's close, per BACKTEST_DATA_CONTRACT_V1"
        ),
        "equivalence_claim": "NONE",
        "performance_attribution": PERFORMANCE_ATTRIBUTION_BOUNDARY,
    }


def execution_timing_profile() -> dict[str, Any]:
    """Describe when a decision is actually executed.

    Returns:
        The profile.
    """
    return {
        "version": OPERATIONAL_EXECUTION_TIMING_VERSION,
        "strategy_decision": "month-end session, 16:00 America/New_York close",
        "first_eligible_execution_session": (
            "the first calendar session strictly after the decision session"
        ),
        "operational_execution_slot": "09:35 America/New_York",
        "operational_execution_window_minutes": 25,
        "research_convention": "open of the next benchmark session",
        "equivalence_claim": (
            "NONE. 09:35 is a declared operational implementation timing, not the "
            "opening auction. No market-on-open order, pre-open submission or "
            "auction semantics were added to imitate an opening print — doing that "
            "to chase backtest equivalence would be a new execution contract, and it "
            "would need its own phase"
        ),
        "late_execution": (
            "NONE — past the window the slot is MISSED_STRATEGY_EXECUTION and is "
            "never filled later that session or the next"
        ),
        "performance_attribution": PERFORMANCE_ATTRIBUTION_BOUNDARY,
    }
