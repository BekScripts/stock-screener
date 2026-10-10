"""What risk did to the strategy's request, recorded rather than inferred.

A strategy expresses *desired* exposure. The risk engine decides *permitted*
exposure. Execution acts only on the permitted set. Those are three different
facts, and the operational question this module answers is: can the second be
trusted to be a reduction of the first?

The rule is monotonic and one-directional. Risk may reduce any requested
exposure, including to zero. Risk may **not** give this strategy positive
exposure to a security the strategy did not positively request, and may not
raise an exposure above what was requested. A reduction is the system working —
a cap binding, a drawdown band, a concentration limit — and blocking on it
would mean the risk engine could only ever be obeyed when it agreed. An
expansion is something else entirely: it would mean exposure reached the book
that no strategy authority ever asked for, which is the one thing the authority
chain exists to make impossible.

The transformation is recorded, not applied. `StrategyDecision` stays exactly as
it was taken. Rewriting it to look as though risk had selected fewer names would
destroy the only evidence of what the strategy actually wanted, and with it any
ability to tell a risk constraint from a strategy change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import Code, RuntimeBindingError

if TYPE_CHECKING:
    from stock_screener.strategies import StrategyDecision

__all__ = [
    "RISK_AUTHORITY_RULE",
    "RISK_TRANSFORMATION_VERSION",
    "WEIGHT_TOLERANCE",
    "RiskTransformation",
    "RiskTransformationRow",
    "transformation_for",
]

#: The transformation artefact contract.
RISK_TRANSFORMATION_VERSION = "STRATEGY_TO_RISK_TRANSFORMATION_V1"

#: The frozen authority rule, stated so a reader does not have to infer it.
RISK_AUTHORITY_RULE = "RISK_MAY_REDUCE_NEVER_EXPAND_STRATEGY_AUTHORITY_V1"

#: How much float noise counts as equal. The risk engine works in floats and an
#: unconstrained pass-through of 1/3 comes back as 0.3333333333333333 against a
#: desired 0.3333333333333333 — equal in intent, and a strict `<=` on floats
#: would be a coin toss. Deliberately tiny: it absorbs representation error and
#: nothing that could hide a real increase.
WEIGHT_TOLERANCE = 1e-9


@dataclass(frozen=True)
class RiskTransformationRow:
    """What happened to one identity's requested exposure.

    Attributes:
        identity_label: The durable identity, as `KIND:id`.
        desired_weight: What the strategy asked for.
        approved_weight: What risk permitted.
        reduction: How much was taken away. Never negative — an increase is
            refused before a row is built.
        zeroed: Whether a positive request was reduced entirely.
        risk_reasons: Risk's own reason codes, where it gave any.
    """

    identity_label: str
    desired_weight: float
    approved_weight: float
    risk_reasons: tuple[str, ...] = ()

    @property
    def reduction(self) -> float:
        """How much of the request was removed."""
        return max(0.0, self.desired_weight - self.approved_weight)

    @property
    def zeroed(self) -> bool:
        """Whether a positive request ended at zero."""
        return self.desired_weight > 0.0 and self.approved_weight <= 0.0

    def body(self) -> dict[str, Any]:
        """Return the row's semantic content."""
        return {
            "identity": self.identity_label,
            "desired_weight": self.desired_weight,
            "approved_weight": self.approved_weight,
            "reduction": self.reduction,
            "zeroed": self.zeroed,
            "risk_reasons": list(self.risk_reasons),
        }


@dataclass(frozen=True)
class RiskTransformation:
    """The whole desired-to-permitted mapping for one decision.

    Attributes:
        decision_fingerprint: The frozen decision this transforms. Recorded so
            the mapping cannot be read against a different decision.
        risk_policy_fingerprint: Which policy decided.
        rows: One row per identity the strategy positively requested.
        approved_fingerprint: The approved portfolio's digest.
        gross_exposure: What risk permitted in total.
        cash_target_weight: What risk left uninvested.
        outcome: `PROCEED`, or `RISK_CONSTRAINED_NO_ACTION` when every
            requested exposure was reduced to zero.
    """

    decision_fingerprint: str
    risk_policy_fingerprint: str
    rows: tuple[RiskTransformationRow, ...] = ()
    approved_fingerprint: str = ""
    gross_exposure: float = 0.0
    cash_target_weight: float = 0.0
    limitations: tuple[str, ...] = field(
        default_factory=lambda: (
            "A reduction here is the risk engine working, not a strategy failure. "
            "The StrategyDecision is unchanged and still records what was wanted.",
        )
    )

    @property
    def desired_identities(self) -> tuple[str, ...]:
        """Everything the strategy positively requested."""
        return tuple(row.identity_label for row in self.rows if row.desired_weight > 0.0)

    @property
    def approved_identities(self) -> tuple[str, ...]:
        """Everything risk permitted positive exposure to."""
        return tuple(row.identity_label for row in self.rows if row.approved_weight > 0.0)

    @property
    def zeroed_identities(self) -> tuple[str, ...]:
        """Positive requests that risk removed entirely."""
        return tuple(row.identity_label for row in self.rows if row.zeroed)

    @property
    def outcome(self) -> str:
        """Whether there is anything left to execute.

        Returns:
            `RISK_CONSTRAINED_NO_ACTION` when the strategy wanted something and
            risk permitted none of it — a valid, recorded outcome rather than an
            error. `PROCEED` otherwise.
        """
        if self.desired_identities and not self.approved_identities:
            return "RISK_CONSTRAINED_NO_ACTION"
        return "PROCEED"

    def body(self) -> dict[str, Any]:
        """Return the transformation's semantic content."""
        return {
            "version": RISK_TRANSFORMATION_VERSION,
            "authority_rule": RISK_AUTHORITY_RULE,
            "decision_fingerprint": self.decision_fingerprint,
            "risk_policy_fingerprint": self.risk_policy_fingerprint,
            "approved_portfolio_fingerprint": self.approved_fingerprint,
            "gross_exposure": self.gross_exposure,
            "cash_target_weight": self.cash_target_weight,
            "outcome": self.outcome,
            "desired_identities": list(self.desired_identities),
            "approved_identities": list(self.approved_identities),
            "zeroed_identities": list(self.zeroed_identities),
            "rows": [row.body() for row in self.rows],
            "strategy_decision_mutated": False,
            "limitations": list(self.limitations),
        }

    def fingerprint(self) -> str:
        """Digest the transformation."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the transformation with its fingerprint."""
        return {**self.body(), "transformation_fingerprint": self.fingerprint()}

    def summary_lines(self) -> tuple[str, ...]:
        """Render the mapping for a terminal."""
        lines = [
            f"outcome            : {self.outcome}",
            f"risk policy        : {self.risk_policy_fingerprint[:16]}…",
            f"gross exposure     : {self.gross_exposure:.6f}",
            f"cash target        : {self.cash_target_weight:.6f}",
            "desired -> approved:",
        ]
        lines += [
            f"    {row.identity_label:<16} {row.desired_weight:.6f} -> "
            f"{row.approved_weight:.6f}"
            + (f"  (reduced {row.reduction:.6f})" if row.reduction > 0 else "")
            + ("  ZEROED" if row.zeroed else "")
            for row in self.rows
        ]
        lines.append("strategy decision  : unchanged")
        return tuple(lines)


def transformation_for(
    *,
    decision: StrategyDecision,
    approved: Any,
    risk_policy_fingerprint: str,
    risk_decisions: Any = (),
) -> RiskTransformation:
    """Build the transformation, refusing any expansion of strategy authority.

    Args:
        decision: The frozen strategy decision.
        approved: The `ApprovedPortfolio` risk produced.
        risk_policy_fingerprint: Which policy decided.
        risk_decisions: Risk's own per-identity decisions, for their reasons.

    Returns:
        The transformation.

    Raises:
        RuntimeBindingError: `RISK_OUTPUT_EXCEEDS_STRATEGY_AUTHORITY` if the
            approved portfolio holds positive exposure to an identity the
            strategy did not positively request, or raises an exposure above
            what was requested. Either would mean exposure reached the book that
            no strategy authority asked for, so execution is refused rather
            than trimmed to fit — trimming would hide the fault.
    """
    desired = {row.identity.label: float(row.desired_weight) for row in decision.selections}
    for identity in decision.exits:
        desired.setdefault(identity.label, 0.0)
    targets = {str(label): float(weight) for label, weight in dict(approved.targets).items()}
    reasons = _reasons_by_identity(risk_decisions)

    unauthorised = sorted(
        label
        for label, weight in targets.items()
        if weight > WEIGHT_TOLERANCE and desired.get(label, 0.0) <= 0.0
    )
    if unauthorised:
        raise RuntimeBindingError(
            Code.RISK_OUTPUT_EXCEEDS_STRATEGY_AUTHORITY,
            f"the approved portfolio holds positive exposure to {unauthorised}, which "
            f"the frozen decision {decision.decision_id} did not positively request. "
            "Risk may reduce a request and may never create one: exposure nobody's "
            "strategy asked for is what the authority chain exists to prevent. "
            "Execution is refused rather than trimmed, because trimming would hide it",
        )
    oversized = sorted(
        f"{label} approved {targets[label]:.9f} > desired {desired[label]:.9f}"
        for label in targets
        if targets[label] > desired.get(label, 0.0) + WEIGHT_TOLERANCE
    )
    if oversized:
        raise RuntimeBindingError(
            Code.RISK_OUTPUT_EXCEEDS_STRATEGY_AUTHORITY,
            f"the approved portfolio raises exposure above what the strategy "
            f"requested: {oversized}. Risk is a ceiling on strategy desire, never a "
            "floor under it",
        )

    rows = tuple(
        RiskTransformationRow(
            identity_label=label,
            desired_weight=weight,
            approved_weight=targets.get(label, 0.0),
            risk_reasons=reasons.get(label, ()),
        )
        for label, weight in sorted(desired.items())
    )
    return RiskTransformation(
        decision_fingerprint=decision.fingerprint(),
        risk_policy_fingerprint=risk_policy_fingerprint,
        rows=rows,
        approved_fingerprint=str(getattr(approved, "fingerprint", lambda: "")() or ""),
        gross_exposure=float(getattr(approved, "gross_exposure", 0.0)),
        cash_target_weight=float(getattr(approved, "cash_target_weight", 0.0)),
    )


def _reasons_by_identity(risk_decisions: Any) -> dict[str, tuple[str, ...]]:
    """Collect risk's own reason codes per identity.

    Args:
        risk_decisions: Whatever the engine reported, possibly empty.

    Returns:
        Identity label to reason codes. Absent reasons are simply absent — a
        reduction without a stated reason is recorded as a reduction, never as
        a guessed cause.
    """
    collected: dict[str, list[str]] = {}
    for row in risk_decisions or ():
        identity = getattr(row, "identity", None)
        label = getattr(identity, "label", None)
        if label is None:
            continue
        reasons = getattr(row, "reasons", None) or getattr(row, "reason_codes", None) or ()
        for reason in reasons:
            collected.setdefault(str(label), []).append(str(reason))
    return {label: tuple(values) for label, values in collected.items()}
