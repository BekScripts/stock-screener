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
        SOAK_GATE_REGISTRY_MISMATCH: Declared gates are not the registry set.
        SOAK_NAMESPACE_INVALID: A namespace that could escape its root.
        SOAK_PROTOCOL_ALREADY_FROZEN: A different protocol is already frozen.
        SOAK_ACTIVATION_VERSION_MISMATCH: Activation and protocol disagree.
        SOAK_ACTIVATION_ORPHANED: An activation with no frozen protocol.
        SOAK_ALREADY_ACTIVATED: A second, different activation.
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
    SOAK_GATE_REGISTRY_MISMATCH = "SOAK_GATE_REGISTRY_MISMATCH"
    SOAK_RETROSPECTIVE_OBSERVATION = "SOAK_RETROSPECTIVE_OBSERVATION"
    SOAK_NAMESPACE_INVALID = "SOAK_NAMESPACE_INVALID"
    SOAK_PROTOCOL_ALREADY_FROZEN = "SOAK_PROTOCOL_ALREADY_FROZEN"
    SOAK_ACTIVATION_VERSION_MISMATCH = "SOAK_ACTIVATION_VERSION_MISMATCH"
    SOAK_ACTIVATION_ORPHANED = "SOAK_ACTIVATION_ORPHANED"
    SOAK_ALREADY_ACTIVATED = "SOAK_ALREADY_ACTIVATED"


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
