"""Stable refusal codes for the execution layer.

Execution is the only layer that creates orders, and an order is the only thing
in this system that could become a trade. Everything here therefore fails closed:
where a figure is missing, a price is unusable, a state transition is impossible
or an authorisation does not match, the answer is a named refusal rather than a
default. A permissive execution engine does not produce a slightly wrong trade —
it produces a trade nobody authorised.

The codes are grouped by what went wrong, and each is a contract. Renaming one is
a breaking change for whatever reads them.
"""

from __future__ import annotations

__all__ = [
    "EXECUTION_ERROR_CODES",
    "Code",
    "ExecutionError",
]


class Code:
    """Every refusal the execution engine can issue."""

    # authorisation: the boundary between an authorised target and an order
    EXECUTION_AUTHORIZATION_MISMATCH = "EXECUTION_AUTHORIZATION_MISMATCH"
    EXECUTION_BOOK_MISMATCH = "EXECUTION_BOOK_MISMATCH"
    STALE_EXECUTION_INTENT = "STALE_EXECUTION_INTENT"
    EXECUTION_STATE_DRIFT = "EXECUTION_STATE_DRIFT"
    UNAUTHORIZED_ORDER = "UNAUTHORIZED_ORDER"
    UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION = "UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION"
    # the order contract
    UNSUPPORTED_ORDER_TYPE = "UNSUPPORTED_ORDER_TYPE"
    UNSUPPORTED_TIME_IN_FORCE = "UNSUPPORTED_TIME_IN_FORCE"
    UNSUPPORTED_ORDER_SIDE = "UNSUPPORTED_ORDER_SIDE"
    ORDER_INVALID = "ORDER_INVALID"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    # sizing
    INVALID_EXECUTION_PRICE = "INVALID_EXECUTION_PRICE"
    SIZING_SNAPSHOT_INCOMPLETE = "SIZING_SNAPSHOT_INCOMPLETE"
    # the order state machine
    INVALID_ORDER_STATE_TRANSITION = "INVALID_ORDER_STATE_TRANSITION"
    ORDER_OVERFILL = "ORDER_OVERFILL"
    ORDER_ALREADY_TERMINAL = "ORDER_ALREADY_TERMINAL"
    # fills
    FILL_ID_CONFLICT = "FILL_ID_CONFLICT"
    FILL_INVALID = "FILL_INVALID"
    FILL_ATTRIBUTION_DOES_NOT_SUM = "FILL_ATTRIBUTION_DOES_NOT_SUM"
    FILL_ATTRIBUTION_UNAUTHORIZED = "FILL_ATTRIBUTION_UNAUTHORIZED"
    ACCOUNTING_ADMISSION_FAILED = "ACCOUNTING_ADMISSION_FAILED"
    # the paper broker
    PAPER_EXECUTION_PRICE_MISSING = "PAPER_EXECUTION_PRICE_MISSING"
    PAPER_SNAPSHOT_BEFORE_ACKNOWLEDGEMENT = "PAPER_SNAPSHOT_BEFORE_ACKNOWLEDGEMENT"
    PAPER_POLICY_REJECTED = "PAPER_POLICY_REJECTED"
    # the journal, its chain and its anchor
    EXECUTION_EVENT_ID_CONFLICT = "EXECUTION_EVENT_ID_CONFLICT"
    EXECUTION_EVENT_INVALID = "EXECUTION_EVENT_INVALID"
    EXECUTION_JOURNAL_HASH_CHAIN_MISMATCH = "EXECUTION_JOURNAL_HASH_CHAIN_MISMATCH"
    EXECUTION_JOURNAL_SEQUENCE_GAP = "EXECUTION_JOURNAL_SEQUENCE_GAP"
    EXECUTION_JOURNAL_TRUNCATION_DETECTED = "EXECUTION_JOURNAL_TRUNCATION_DETECTED"
    EXECUTION_JOURNAL_HEAD_ANCHOR_MISMATCH = "EXECUTION_JOURNAL_HEAD_ANCHOR_MISMATCH"
    EXECUTION_ANCHOR_MISSING = "EXECUTION_ANCHOR_MISSING"
    EXECUTION_ANCHOR_INVALID = "EXECUTION_ANCHOR_INVALID"
    EXECUTION_ANCHOR_FINGERPRINT_MISMATCH = "EXECUTION_ANCHOR_FINGERPRINT_MISMATCH"
    EXECUTION_GENERATION_MISMATCH = "EXECUTION_GENERATION_MISMATCH"
    EXECUTION_RECOVERY_REQUIRED = "EXECUTION_RECOVERY_REQUIRED"
    # persistence
    EXECUTION_PARTIAL_RECORD = "EXECUTION_PARTIAL_RECORD"
    EXECUTION_PERSISTENCE_VERIFY_FAILED = "EXECUTION_PERSISTENCE_VERIFY_FAILED"
    FORBIDDEN_EXECUTION_NAMESPACE = "FORBIDDEN_EXECUTION_NAMESPACE"
    # the plan
    PLAN_INVALID = "PLAN_INVALID"
    PLAN_ALREADY_TERMINAL = "PLAN_ALREADY_TERMINAL"


EXECUTION_ERROR_CODES: frozenset[str] = frozenset(
    value
    for name, value in vars(Code).items()
    if not name.startswith("_") and isinstance(value, str)
)
"""Every code this module declares."""


class ExecutionError(ValueError):
    """Raised when the execution engine refuses to proceed.

    Attributes:
        code: The stable refusal code.
        detail: What was refused, and why that is the safe answer.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error.

        Args:
            code: One of `Code`'s values.
            detail: The explanation.
        """
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
