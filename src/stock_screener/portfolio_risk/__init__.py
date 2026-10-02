"""The portfolio and risk engine: deterministic authority between strategy and execution.

A strategy decides what it would like to hold. This layer decides what is
actually authorised, and nothing downstream of it can be reached without passing
through it.

```text
Strategy          signal authority
PortfolioEngine   allocation authority
RiskEngine        veto and resize authority
ExecutionEngine   order-planning authority      (later phase)
BrokerAdapter     external-action authority     (later phase)
```

Three rules govern it:

- **A strategy may request; it may not insist.** The risk engine approves,
  resizes or rejects, and there is no override anywhere in the strategy-facing
  surface — no force, no bypass, no flag. An override that exists gets used
  during the incident it would make worse.
- **Construction and risk are separate questions.** *What would express these
  intents* and *how much of it is permitted* are answered by different modules, so
  a final weight can always be attributed to one or the other. Collapsing them
  produces portfolios nobody can explain.
- **Nothing here can trade.** An `ExecutionIntent` is inert data describing a
  difference between two portfolios: no quantity in shares, no broker field, no
  side effect. Broker credentials and order submission belong to an adapter that
  does not exist yet, and keeping this layer incapable of reaching one is what
  makes that boundary enforceable rather than aspirational.

This is `PORTFOLIO_RISK_ENGINE_V1`. It is a foundation, not a production-ready
risk system: no policy here is authorised for live capital, and no threshold in
it has been established as correct.
"""

from __future__ import annotations

from .arithmetic import (
    ARITHMETIC_POLICY_ID,
    EXPOSURE_TOLERANCE,
    HOLD_TOLERANCE,
    WEIGHT_DECIMALS,
    WEIGHT_TOLERANCE,
    arithmetic_policy,
    is_close,
    quantise,
)
from .constructor import CONSTRUCTOR_ID, ConstructedPortfolio, construct
from .engine import (
    TURNOVER_DEFINITION_ID,
    ApprovedPortfolio,
    InvariantViolationError,
    PortfolioDecision,
    evaluate,
)
from .fingerprint import ENGINE_VERSION, binding, digest
from .intents import (
    Action,
    Direction,
    ExecutionIntent,
    IntentError,
    StrategyIntent,
    classify,
    intents_fingerprint,
)
from .models import (
    KINDS,
    PortfolioSnapshot,
    PortfolioStateError,
    Position,
    SecurityIdentity,
)
from .policy import (
    EXAMPLE_POLICY_V1,
    ConstructionMode,
    PolicyError,
    PolicyMode,
    RiskPolicy,
)
from .risk import CONSTRAINT_MODEL_ID, Decision, RiskDecision, RiskOutcome, authorize
from .run import evaluate_request, load_request
from .validation import REASON_CODES, ReasonCode, ValidationError, validate_inputs

__all__ = [
    "ARITHMETIC_POLICY_ID",
    "CONSTRAINT_MODEL_ID",
    "CONSTRUCTOR_ID",
    "ENGINE_VERSION",
    "EXAMPLE_POLICY_V1",
    "EXPOSURE_TOLERANCE",
    "HOLD_TOLERANCE",
    "KINDS",
    "REASON_CODES",
    "TURNOVER_DEFINITION_ID",
    "WEIGHT_DECIMALS",
    "WEIGHT_TOLERANCE",
    "Action",
    "ApprovedPortfolio",
    "ConstructedPortfolio",
    "ConstructionMode",
    "Decision",
    "Direction",
    "ExecutionIntent",
    "IntentError",
    "InvariantViolationError",
    "PolicyError",
    "PolicyMode",
    "PortfolioDecision",
    "PortfolioSnapshot",
    "PortfolioStateError",
    "Position",
    "ReasonCode",
    "RiskDecision",
    "RiskOutcome",
    "RiskPolicy",
    "SecurityIdentity",
    "StrategyIntent",
    "ValidationError",
    "arithmetic_policy",
    "authorize",
    "binding",
    "classify",
    "construct",
    "digest",
    "evaluate",
    "evaluate_request",
    "intents_fingerprint",
    "is_close",
    "load_request",
    "quantise",
    "validate_inputs",
]
