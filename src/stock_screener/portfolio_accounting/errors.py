"""Stable error codes for the accounting authority.

Accounting is the layer where a quiet repair becomes a wrong number that
everything downstream trusts. So every refusal here has a code a monitor can be
written against, and the codes do not change when a message is improved.

The rule throughout: accounting **records evidence**. It does not create fills,
infer entitlements, guess a corporate action's economics, or reconcile a break by
adopting someone else's figure. Each of those would turn a missing input into a
plausible-looking state, and a plausible-looking wrong state is worse than a
refusal because nobody investigates it.
"""

from __future__ import annotations

__all__ = [
    "ACCOUNTING_ERROR_CODES",
    "AccountingError",
    "Code",
]


class Code:
    """Every refusal the accounting engine can issue."""

    # event and ledger integrity
    EVENT_ID_CONFLICT = "EVENT_ID_CONFLICT"
    EVENT_TIME_REGRESSION = "EVENT_TIME_REGRESSION"
    EVENT_INVALID = "EVENT_INVALID"
    LEDGER_HASH_CHAIN_MISMATCH = "LEDGER_HASH_CHAIN_MISMATCH"
    LEDGER_SEQUENCE_GAP = "LEDGER_SEQUENCE_GAP"
    LEDGER_EMPTY = "LEDGER_EMPTY"
    OPENING_BALANCE_ALREADY_EXISTS = "OPENING_BALANCE_ALREADY_EXISTS"
    OPENING_BALANCE_REQUIRED = "OPENING_BALANCE_REQUIRED"
    CHECKPOINT_UNVERIFIABLE = "CHECKPOINT_UNVERIFIABLE"

    # trading
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    SHORT_POSITION_NOT_SUPPORTED = "SHORT_POSITION_NOT_SUPPORTED"

    # attribution
    SELL_ATTRIBUTION_REQUIRED = "SELL_ATTRIBUTION_REQUIRED"
    ATTRIBUTION_DOES_NOT_SUM = "ATTRIBUTION_DOES_NOT_SUM"
    ATTRIBUTION_EXCEEDS_SLICE = "ATTRIBUTION_EXCEEDS_SLICE"
    UNATTRIBUTED_EXISTING = "UNATTRIBUTED_EXISTING"
    ATTRIBUTION_RECONCILIATION_FAILED = "ATTRIBUTION_RECONCILIATION_FAILED"

    # corporate actions
    UNSUPPORTED_CORPORATE_ACTION = "UNSUPPORTED_CORPORATE_ACTION"
    SPLIT_RATIO_INVALID = "SPLIT_RATIO_INVALID"

    # valuation
    MARK_AFTER_VALUATION_TIME = "MARK_AFTER_VALUATION_TIME"
    VALUATION_INCOMPLETE = "VALUATION_INCOMPLETE"
    MARK_STALE = "MARK_STALE"
    MARK_INVALID = "MARK_INVALID"

    # accounting identities
    NAV_IDENTITY_VIOLATED = "NAV_IDENTITY_VIOLATED"
    PNL_DECOMPOSITION_VIOLATED = "PNL_DECOMPOSITION_VIOLATED"

    # reconciliation
    CASH_MISMATCH = "CASH_MISMATCH"
    POSITION_QUANTITY_MISMATCH = "POSITION_QUANTITY_MISMATCH"
    EXTERNAL_POSITION_MISSING_INTERNAL = "EXTERNAL_POSITION_MISSING_INTERNAL"
    INTERNAL_POSITION_MISSING_EXTERNAL = "INTERNAL_POSITION_MISSING_EXTERNAL"
    INCOMPLETE_EXTERNAL = "INCOMPLETE_EXTERNAL"
    INCOMPLETE_INTERNAL = "INCOMPLETE_INTERNAL"

    # persistence
    PARTIAL_EVENT_RECORD = "PARTIAL_EVENT_RECORD"
    PERSISTENCE_VERIFY_FAILED = "PERSISTENCE_VERIFY_FAILED"
    FORBIDDEN_LEDGER_NAMESPACE = "FORBIDDEN_LEDGER_NAMESPACE"


#: Every code, so a test can assert that nothing reaches a record which a
#: consumer could not have been written against.
ACCOUNTING_ERROR_CODES: frozenset[str] = frozenset(
    value
    for name, value in vars(Code).items()
    if not name.startswith("_") and isinstance(value, str)
)


class AccountingError(ValueError):
    """Raised when the accounting engine refuses to record or derive something."""

    def __init__(self, code: str, detail: str) -> None:
        """Carry the machine-readable code alongside the prose.

        Args:
            code: The stable reason code.
            detail: What went wrong, and why refusing is the right answer.
        """
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
