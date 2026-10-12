"""Stable refusal codes for the paper-operations runtime.

Separate from `paper_operations.errors` because the control plane's codes
describe why a *run* was refused, and these describe why a *runtime* could not
be established or trusted. Collapsing them would make "this root is not bound"
indistinguishable from "this run is blocked", and an operator needs to act
differently on each.
"""

from __future__ import annotations

__all__ = ["Code", "RuntimeBindingError"]


class Code:
    """Every way a runtime can refuse.

    Attributes:
        RUNTIME_NOT_BOUND: No binding exists for this root.
        RUNTIME_ALREADY_BOUND: A different binding is already present.
        RUNTIME_BINDING_INVALID: The binding could not legitimately exist.
        RUNTIME_BINDING_UNREADABLE: A binding exists and cannot be trusted.
        RUNTIME_GENERATION_MISMATCH: The filesystem's lineage is not the bound
            one, so the records on disk belong to something else.
        RUNTIME_STRATEGY_MISMATCH: The strategy is not the bound build.
        RUNTIME_LIVE_NOT_AUTHORIZED: Live was named. There is no live.
        BOOTSTRAP_NOT_CONFIRMED: The operator did not confirm.
        BOOTSTRAP_ENVIRONMENT_NOT_PROVEN: The paper venue was not proven.
        BOOTSTRAP_ACCOUNT_MISMATCH: The venue answered for another account.
        BOOTSTRAP_POSITION_STATE_NOT_EMPTY: The account holds positions.
        BOOTSTRAP_OPEN_ORDERS_PRESENT: The account holds working orders.
        BOOTSTRAP_CASH_FIELD_AMBIGUOUS: No provider field matches internal cash.
        BOOTSTRAP_ALREADY_DONE: This root already holds an opened book.
        DECISION_NOT_FOUND: No frozen decision for the session claimed.
        DECISION_TAMPERED: A stored decision does not match its fingerprint.
        DECISION_NOT_YET_EXECUTABLE: Its execution session has not arrived.
        DECISION_EXECUTION_WINDOW_MISSED: Its window closed; never backfilled.
        DECISION_ALREADY_EXECUTED: It has already been executed.
        MISSED_STRATEGY_DECISION: A decision slot closed without a decision.
        MISSED_STRATEGY_EXECUTION: An execution slot closed without executing.
        SCORE_SOURCE_NOT_AUTHORITATIVE: The input did not come from the frozen
            score authority.
        SCORE_SNAPSHOT_UNAVAILABLE: No admissible cross-section exists.
        RISK_OUTPUT_EXCEEDS_STRATEGY_AUTHORITY: Risk granted exposure the
            strategy never requested, or raised one above the request.
        STATE_DRIFT_SINCE_APPROVAL: Authoritative state moved after approval.
        RISK_POLICY_NOT_BOUND: The runtime names no approved risk policy.
    """

    RUNTIME_NOT_BOUND = "RUNTIME_NOT_BOUND"
    RUNTIME_ALREADY_BOUND = "RUNTIME_ALREADY_BOUND"
    RUNTIME_BINDING_INVALID = "RUNTIME_BINDING_INVALID"
    RUNTIME_BINDING_UNREADABLE = "RUNTIME_BINDING_UNREADABLE"
    RUNTIME_GENERATION_MISMATCH = "RUNTIME_GENERATION_MISMATCH"
    RUNTIME_STRATEGY_MISMATCH = "RUNTIME_STRATEGY_MISMATCH"
    RUNTIME_LIVE_NOT_AUTHORIZED = "RUNTIME_LIVE_NOT_AUTHORIZED"
    BOOTSTRAP_NOT_CONFIRMED = "BOOTSTRAP_NOT_CONFIRMED"
    BOOTSTRAP_ENVIRONMENT_NOT_PROVEN = "BOOTSTRAP_ENVIRONMENT_NOT_PROVEN"
    BOOTSTRAP_ACCOUNT_MISMATCH = "BOOTSTRAP_ACCOUNT_MISMATCH"
    BOOTSTRAP_POSITION_STATE_NOT_EMPTY = "PAPER_BOOK_BOOTSTRAP_POSITION_STATE_NOT_EMPTY"
    BOOTSTRAP_OPEN_ORDERS_PRESENT = "BOOTSTRAP_OPEN_ORDERS_PRESENT"
    BOOTSTRAP_CASH_FIELD_AMBIGUOUS = "BOOTSTRAP_CASH_FIELD_AMBIGUOUS"
    BOOTSTRAP_ALREADY_DONE = "BOOTSTRAP_ALREADY_DONE"
    DECISION_NOT_FOUND = "DECISION_NOT_FOUND"
    DECISION_TAMPERED = "DECISION_TAMPERED"
    DECISION_NOT_YET_EXECUTABLE = "DECISION_NOT_YET_EXECUTABLE"
    DECISION_EXECUTION_WINDOW_MISSED = "DECISION_EXECUTION_WINDOW_MISSED"
    DECISION_ALREADY_EXECUTED = "DECISION_ALREADY_EXECUTED"
    MISSED_STRATEGY_DECISION = "MISSED_STRATEGY_DECISION"
    MISSED_STRATEGY_EXECUTION = "MISSED_STRATEGY_EXECUTION"
    SCORE_SOURCE_NOT_AUTHORITATIVE = "SCORE_SOURCE_NOT_AUTHORITATIVE"
    SCORE_SNAPSHOT_UNAVAILABLE = "SCORE_SNAPSHOT_UNAVAILABLE"
    RISK_OUTPUT_EXCEEDS_STRATEGY_AUTHORITY = "RISK_OUTPUT_EXCEEDS_STRATEGY_AUTHORITY"
    STATE_DRIFT_SINCE_APPROVAL = "STATE_DRIFT_SINCE_APPROVAL"
    RISK_POLICY_NOT_BOUND = "RISK_POLICY_NOT_BOUND"
    RUNTIME_RISK_POLICY_NOT_AUTHORED = "RUNTIME_RISK_POLICY_NOT_AUTHORED"
    RUNTIME_SYMBOL_MAPPING_NOT_AUTHORED = "RUNTIME_SYMBOL_MAPPING_NOT_AUTHORED"
    RUNTIME_OPERATOR_INPUT_UNREADABLE = "RUNTIME_OPERATOR_INPUT_UNREADABLE"


class RuntimeBindingError(RuntimeError):
    """Raised when a runtime refuses to be established, trusted or run.

    Attributes:
        code: Which refusal, from `Code`.
    """

    def __init__(self, code: str, message: str) -> None:
        """Build the error.

        Args:
            code: The stable code.
            message: What a reader needs. Carries no credential.
        """
        super().__init__(f"{code}: {message}")
        self.code = code
