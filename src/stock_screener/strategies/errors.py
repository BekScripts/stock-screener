"""Stable refusal codes for the strategy authority.

Codes rather than prose because an operational caller has to branch on them, and
a refusal a caller cannot distinguish from another refusal is a refusal it will
eventually ignore.
"""

from __future__ import annotations

__all__ = ["Code", "StrategyError"]


class Code:
    """Every way strategy evaluation can refuse.

    Attributes:
        STRATEGY_INPUT_AFTER_DECISION_TIME: Evidence postdates the decision.
        STRATEGY_INPUT_NOT_ADMISSIBLE: Admissibility could not be established.
        STRATEGY_INPUT_VERSION_MISMATCH: The bundle is not the frozen version.
        STRATEGY_INPUT_INCOMPLETE: A record lacks what admissibility needs.
        STRATEGY_NO_DECISION_DUE: The session is not a decision session.
        STRATEGY_SESSION_NOT_IN_CALENDAR: The session is not a market session.
        STRATEGY_NO_EXECUTION_SESSION: The calendar ends at the decision.
        STRATEGY_NOT_AUTHORIZED: No deployment grant covers this request.
        STRATEGY_AUTHORIZATION_VERSION_MISMATCH: The grant names another build.
        STRATEGY_LIVE_NOT_AUTHORIZED: Live was requested. There is no live.
        STRATEGY_STATE_UNREADABLE: Persisted state could not be trusted.
    """

    STRATEGY_INPUT_AFTER_DECISION_TIME = "STRATEGY_INPUT_AFTER_DECISION_TIME"
    STRATEGY_INPUT_NOT_ADMISSIBLE = "STRATEGY_INPUT_NOT_ADMISSIBLE"
    STRATEGY_INPUT_VERSION_MISMATCH = "STRATEGY_INPUT_VERSION_MISMATCH"
    STRATEGY_INPUT_INCOMPLETE = "STRATEGY_INPUT_INCOMPLETE"
    STRATEGY_NO_DECISION_DUE = "STRATEGY_NO_DECISION_DUE"
    STRATEGY_SESSION_NOT_IN_CALENDAR = "STRATEGY_SESSION_NOT_IN_CALENDAR"
    STRATEGY_NO_EXECUTION_SESSION = "STRATEGY_NO_EXECUTION_SESSION"
    STRATEGY_NOT_AUTHORIZED = "STRATEGY_NOT_AUTHORIZED"
    STRATEGY_AUTHORIZATION_VERSION_MISMATCH = "STRATEGY_AUTHORIZATION_VERSION_MISMATCH"
    STRATEGY_LIVE_NOT_AUTHORIZED = "STRATEGY_LIVE_NOT_AUTHORIZED"
    STRATEGY_STATE_UNREADABLE = "STRATEGY_STATE_UNREADABLE"


class StrategyError(RuntimeError):
    """Raised when the strategy refuses to decide.

    Attributes:
        code: Which refusal, from `Code`.
    """

    def __init__(self, code: str, message: str) -> None:
        """Build the error.

        Args:
            code: The stable code.
            message: What a reader needs to know. Carries no credential.
        """
        super().__init__(f"{code}: {message}")
        self.code = code
