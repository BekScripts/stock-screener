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
    "V1_1_ADDED_ERROR_CODES",
    "V1_1_ERROR_CODES",
    "V1_2_ADDED_ERROR_CODES",
    "V1_ERROR_CODES",
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
    # execution realism (V1.1)
    INVALID_EXECUTION_POLICY = "INVALID_EXECUTION_POLICY"
    INVALID_PAPER_QUOTE = "INVALID_PAPER_QUOTE"
    PAPER_QUOTE_REQUIRED = "PAPER_QUOTE_REQUIRED"
    PAPER_VOLUME_REQUIRED = "PAPER_VOLUME_REQUIRED"
    PAPER_MARKET_SESSION_MISMATCH = "PAPER_MARKET_SESSION_MISMATCH"
    PARTICIPATION_CAPACITY_EXHAUSTED = "PARTICIPATION_CAPACITY_EXHAUSTED"
    ORDER_ALREADY_EXPIRED = "ORDER_ALREADY_EXPIRED"
    EXECUTION_WINDOW_EXHAUSTED = "EXECUTION_WINDOW_EXHAUSTED"
    UNKNOWN_EXECUTION_SESSION = "UNKNOWN_EXECUTION_SESSION"
    EXECUTION_COST_DECOMPOSITION_VIOLATED = "EXECUTION_COST_DECOMPOSITION_VIOLATED"
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
    # the external paper venue (V1.2)
    LIVE_TRADING_NOT_AUTHORIZED = "LIVE_TRADING_NOT_AUTHORIZED"
    BROKER_ENVIRONMENT_NOT_PROVEN_PAPER = "BROKER_ENVIRONMENT_NOT_PROVEN_PAPER"
    BROKER_ACCOUNT_MISMATCH = "BROKER_ACCOUNT_MISMATCH"
    BROKER_CREDENTIALS_MISSING = "BROKER_CREDENTIALS_MISSING"
    BROKER_CAPABILITY_UNSUPPORTED = "BROKER_CAPABILITY_UNSUPPORTED"
    BROKER_SYMBOL_MAPPING_UNRESOLVED = "BROKER_SYMBOL_MAPPING_UNRESOLVED"
    BROKER_ASSET_NOT_TRADABLE = "BROKER_ASSET_NOT_TRADABLE"
    BROKER_FRACTIONAL_NOT_SUPPORTED = "BROKER_FRACTIONAL_NOT_SUPPORTED"
    BROKER_STATUS_UNMAPPED = "BROKER_STATUS_UNMAPPED"
    BROKER_FILL_FEE_UNKNOWN = "BROKER_FILL_FEE_UNKNOWN"
    BROKER_DAY_ORDER_NOT_TERMINAL = "BROKER_DAY_ORDER_NOT_TERMINAL"
    BROKER_UNREACHABLE = "BROKER_UNREACHABLE"
    BROKER_RATE_LIMITED = "BROKER_RATE_LIMITED"
    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    REMOTE_CLIENT_ORDER_ID_CONFLICT = "REMOTE_CLIENT_ORDER_ID_CONFLICT"
    REMOTE_FILL_CONFLICT = "REMOTE_FILL_CONFLICT"
    REMOTE_EVENT_ORDER_AMBIGUOUS = "REMOTE_EVENT_ORDER_AMBIGUOUS"
    REMOTE_ORDER_NOT_FOUND = "REMOTE_ORDER_NOT_FOUND"
    REMOTE_PROVIDER_ORDER_ID_REQUIRED = "REMOTE_PROVIDER_ORDER_ID_REQUIRED"
    """A provider-side read needs the venue's own order id and was given none.

    Raised rather than falling back to the client order id: the activities
    endpoint does not filter on that, and sending it would return the account's
    other fills while looking like it had worked."""

    REMOTE_FILL_ORDER_MISMATCH = "REMOTE_FILL_ORDER_MISMATCH"
    """An execution the venue returned does not belong to the order it was
    requested for. Refused rather than ingested — attributing one order's fill
    to another puts quantity in the book that nothing authorised."""
    # observability: a mutation that cannot be recorded is not sent
    EXTERNAL_REQUEST_AUDIT_FAILED = "EXTERNAL_REQUEST_AUDIT_FAILED"
    EXTERNAL_SUBMISSION_BLOCKED = "EXTERNAL_SUBMISSION_BLOCKED"
    EXTERNAL_VENUE_MISMATCH = "EXTERNAL_VENUE_MISMATCH"
    UNEXPECTED_REMOTE_ORDER = "UNEXPECTED_REMOTE_ORDER"
    REMOTE_FILL_MISSING_LOCAL = "REMOTE_FILL_MISSING_LOCAL"
    LOCAL_FILL_MISSING_REMOTE = "LOCAL_FILL_MISSING_REMOTE"
    REMOTE_POSITION_MISMATCH = "REMOTE_POSITION_MISMATCH"
    REMOTE_CASH_MISMATCH = "REMOTE_CASH_MISMATCH"
    REMOTE_ORDER_MISMATCH = "REMOTE_ORDER_MISMATCH"
    # persistence
    EXECUTION_PARTIAL_RECORD = "EXECUTION_PARTIAL_RECORD"
    EXECUTION_PERSISTENCE_VERIFY_FAILED = "EXECUTION_PERSISTENCE_VERIFY_FAILED"
    FORBIDDEN_EXECUTION_NAMESPACE = "FORBIDDEN_EXECUTION_NAMESPACE"
    # the plan
    PLAN_INVALID = "PLAN_INVALID"
    PLAN_ALREADY_TERMINAL = "PLAN_ALREADY_TERMINAL"


#: What the V1.2 engine adds: everything about reaching an external venue. None
#: of it is reachable from a V1 or V1.1 run, which never opens a socket.
V1_2_ADDED_ERROR_CODES: frozenset[str] = frozenset(
    {
        "BROKER_ACCOUNT_MISMATCH",
        "BROKER_ASSET_NOT_TRADABLE",
        "BROKER_CAPABILITY_UNSUPPORTED",
        "BROKER_CREDENTIALS_MISSING",
        "BROKER_DAY_ORDER_NOT_TERMINAL",
        "BROKER_ENVIRONMENT_NOT_PROVEN_PAPER",
        "BROKER_FILL_FEE_UNKNOWN",
        "BROKER_FRACTIONAL_NOT_SUPPORTED",
        "BROKER_RATE_LIMITED",
        "BROKER_STATUS_UNMAPPED",
        "BROKER_SYMBOL_MAPPING_UNRESOLVED",
        "BROKER_UNREACHABLE",
        "EXTERNAL_REQUEST_AUDIT_FAILED",
        "EXTERNAL_SUBMISSION_BLOCKED",
        "EXTERNAL_VENUE_MISMATCH",
        "LIVE_TRADING_NOT_AUTHORIZED",
        "LOCAL_FILL_MISSING_REMOTE",
        "REMOTE_CASH_MISMATCH",
        "REMOTE_CLIENT_ORDER_ID_CONFLICT",
        "REMOTE_EVENT_ORDER_AMBIGUOUS",
        "REMOTE_FILL_CONFLICT",
        "REMOTE_FILL_MISSING_LOCAL",
        "REMOTE_ORDER_MISMATCH",
        "REMOTE_FILL_ORDER_MISMATCH",
        "REMOTE_ORDER_NOT_FOUND",
        "REMOTE_PROVIDER_ORDER_ID_REQUIRED",
        "REMOTE_POSITION_MISMATCH",
        "REMOTE_STATE_UNKNOWN",
        "UNEXPECTED_REMOTE_ORDER",
    }
)

#: What the V1.1 engine adds. Separate because a V1 run genuinely cannot raise
#: any of them: it has no quote, no volume, no participation ceiling, no expiry
#: and no multi-session horizon. Listing them in a V1 manifest would claim a
#: vocabulary that version does not have, and would move a fingerprint P2.0
#: froze every time a later phase adds a code.
V1_1_ADDED_ERROR_CODES: frozenset[str] = frozenset(
    {
        "EXECUTION_COST_DECOMPOSITION_VIOLATED",
        "EXECUTION_WINDOW_EXHAUSTED",
        "INVALID_EXECUTION_POLICY",
        "INVALID_PAPER_QUOTE",
        "ORDER_ALREADY_EXPIRED",
        "PAPER_MARKET_SESSION_MISMATCH",
        "PAPER_QUOTE_REQUIRED",
        "PAPER_VOLUME_REQUIRED",
        "PARTICIPATION_CAPACITY_EXHAUSTED",
        "UNKNOWN_EXECUTION_SESSION",
    }
)

EXECUTION_ERROR_CODES: frozenset[str] = frozenset(
    value
    for name, value in vars(Code).items()
    if not name.startswith("_") and isinstance(value, str)
)
"""Every code this module declares, across both engine versions."""

#: What the V1 engine can raise: everything except the later additions.
V1_ERROR_CODES: frozenset[str] = (
    EXECUTION_ERROR_CODES - V1_1_ADDED_ERROR_CODES - V1_2_ADDED_ERROR_CODES
)

#: What the V1.1 engine can raise. Still no external venue.
V1_1_ERROR_CODES: frozenset[str] = V1_ERROR_CODES | V1_1_ADDED_ERROR_CODES


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
