"""Stable refusal codes for soak verification."""

from __future__ import annotations

__all__ = ["Code", "SoakContractError"]


class Code:
    """Every way soak verification can refuse.

    Attributes:
        SOAK_GATE_WITHOUT_EVALUATOR: A declared gate no evaluator covers.
        SOAK_EVALUATOR_UNDECLARED: A known check the protocol never declared.
        SOAK_STORE_INCOMPATIBLE: The store cannot supply required evidence.
        SOAK_DRILL_TYPE_UNSUPPORTED: A required drill type nothing can record.
        SOAK_VERDICT_UNREACHABLE: A protocol whose `A` is mechanically
            impossible, which must never be armed.
        SOAK_PREFLIGHT_FAILED: The pre-arming self-check refused.
        SOAK_DRILL_INADMISSIBLE: Drill evidence not bound to this protocol.
        SOAK_DRILL_CONFLICT: Conflicting evidence under one drill identity.
        SOAK_CORRECTION_AMBIGUOUS: Competing corrections for one session.
    """

    SOAK_GATE_WITHOUT_EVALUATOR = "SOAK_GATE_WITHOUT_EVALUATOR"
    SOAK_EVALUATOR_UNDECLARED = "SOAK_EVALUATOR_UNDECLARED"
    SOAK_STORE_INCOMPATIBLE = "SOAK_STORE_INCOMPATIBLE"
    SOAK_DRILL_TYPE_UNSUPPORTED = "SOAK_DRILL_TYPE_UNSUPPORTED"
    SOAK_VERDICT_UNREACHABLE = "SOAK_VERDICT_UNREACHABLE"
    SOAK_PREFLIGHT_FAILED = "SOAK_PREFLIGHT_FAILED"
    SOAK_DRILL_INADMISSIBLE = "SOAK_DRILL_INADMISSIBLE"
    SOAK_DRILL_CONFLICT = "SOAK_DRILL_CONFLICT"
    SOAK_CORRECTION_AMBIGUOUS = "SOAK_CORRECTION_AMBIGUOUS"


class SoakContractError(RuntimeError):
    """Raised when a soak contract cannot be verified.

    Attributes:
        code: Which refusal, from `Code`.
    """

    def __init__(self, code: str, message: str) -> None:
        """Build the error.

        Args:
            code: The stable code.
            message: What a reader needs.
        """
        super().__init__(f"{code}: {message}")
        self.code = code
