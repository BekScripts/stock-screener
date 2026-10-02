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

`PORTFOLIO_RISK_ENGINE_V1_1` adds market-risk awareness: a portfolio-volatility
ceiling from a declared covariance matrix, position and trade capacity limits from
average dollar volume, and a drawdown overlay that can tighten the gross limit or
escalate the operating mode. Three further rules govern that layer:

- **Risk controls only reduce.** Every ceiling is seeded at the pre-risk weight
  and only ever lowered, so no later stage — including the budget
  redistribution — can hand back exposure an earlier one removed. The budget a cap
  frees becomes cash rather than another name's position.
- **Ignorance is not permission.** An unknown volatility is not zero and an
  unknown ADV is not infinite. A missing metric holds the position where it stands
  and still permits a reduction, unless the policy explicitly says otherwise.
- **The overlay can only tighten.** A drawdown multiplier above one is refused, a
  band that weakens at greater depth is refused, and the effective mode is always
  the stricter of the base mode and the band's. Halting still does not liquidate.

`PORTFOLIO_RISK_ENGINE_V1_2` closes the covariance gap P1.1 declared and lets
several strategies request capital through one authority. Three further rules:

- **Covariance fails closed.** A matrix is validated for positive semidefiniteness
  by `LDLᵀ` before it is given any authority, and a materially negative portfolio
  variance raises rather than clamping to zero — reporting zero volatility for a
  portfolio the matrix cannot describe is the one failure that looks like safety.
  Singular-but-valid matrices are accepted: perfect correlation is a fact, not a
  fault.
- **Capital authority is explicit.** Several strategies need a
  `StrategyAllocationPolicy`; without one the engine still refuses. Budgets are
  ceilings and unused sleeve capacity becomes **cash**, never another strategy's
  position. Nothing in the package can read a return, a Sharpe ratio or a win rate.
- **Exposure aggregates, attribution does not.** Two strategies wanting one
  security hold one position, and the record still says who asked for how much.
  When global risk reduces the aggregate, attribution reconciles proportionally.

Supplying no risk snapshot keeps V1 semantics exactly, and supplying no allocation
policy or correlation control keeps V1.1 semantics exactly, so both earlier
decisions and their golden outputs still reproduce byte for byte.

Neither version is a production risk system: no policy here is authorised for live
capital, no threshold in either has been established as correct, and none was
chosen by running historical performance.
"""

from __future__ import annotations

from .advanced import ADVANCED_STAGE_ORDER, ADVANCED_STAGE_ORDER_V1_2
from .allocation import (
    ALLOCATOR_ID,
    EXAMPLE_MULTI_STRATEGY_POLICY_V1,
    AggregatedRequest,
    BudgetSemantics,
    StrategyAllocationPolicy,
    StrategyBudget,
    StrategyContribution,
    StrategySleeve,
    allocate,
)
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
from .concentration import (
    CONCENTRATION_ALGORITHM_ID,
    apply_correlation_concentration,
    correlation_of,
)
from .constructor import CONSTRUCTOR_ID, ConstructedPortfolio, construct
from .drawdown import (
    MODE_SEVERITY,
    DrawdownBand,
    EffectiveRiskPolicy,
    derive_effective_policy,
    stricter_mode,
    validate_bands,
)
from .engine import (
    ADVANCED_ENGINE_VERSION,
    MULTI_STRATEGY_ENGINE_VERSION,
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
from .linalg import LDL_METHOD_ID, PsdVerdict, is_positive_semidefinite
from .models import (
    KINDS,
    PortfolioSnapshot,
    PortfolioStateError,
    Position,
    SecurityIdentity,
)
from .policy import (
    EXAMPLE_ADVANCED_RISK_POLICY_V1,
    EXAMPLE_POLICY_V1,
    ConstructionMode,
    MissingMetricPolicy,
    PolicyError,
    PolicyMode,
    RiskPolicy,
)
from .report import PortfolioRiskReport, build_risk_report, build_strategy_report
from .risk import CONSTRAINT_MODEL_ID, Decision, RiskDecision, RiskOutcome, authorize
from .risk_inputs import (
    COVARIANCE_TOLERANCE,
    MAX_PLAUSIBLE_VOLATILITY,
    CovarianceSnapshot,
    MetricState,
    PortfolioRiskSnapshot,
    RiskInputError,
    SecurityRiskMetrics,
)
from .run import evaluate_request, load_allocation, load_request, load_risk
from .validation import REASON_CODES, ReasonCode, ValidationError, validate_inputs

__all__ = [
    "ADVANCED_ENGINE_VERSION",
    "ADVANCED_STAGE_ORDER",
    "ADVANCED_STAGE_ORDER_V1_2",
    "ALLOCATOR_ID",
    "ARITHMETIC_POLICY_ID",
    "CONCENTRATION_ALGORITHM_ID",
    "CONSTRAINT_MODEL_ID",
    "CONSTRUCTOR_ID",
    "COVARIANCE_TOLERANCE",
    "ENGINE_VERSION",
    "EXAMPLE_ADVANCED_RISK_POLICY_V1",
    "EXAMPLE_MULTI_STRATEGY_POLICY_V1",
    "EXAMPLE_POLICY_V1",
    "EXPOSURE_TOLERANCE",
    "HOLD_TOLERANCE",
    "KINDS",
    "LDL_METHOD_ID",
    "MAX_PLAUSIBLE_VOLATILITY",
    "MODE_SEVERITY",
    "MULTI_STRATEGY_ENGINE_VERSION",
    "REASON_CODES",
    "TURNOVER_DEFINITION_ID",
    "WEIGHT_DECIMALS",
    "WEIGHT_TOLERANCE",
    "Action",
    "AggregatedRequest",
    "ApprovedPortfolio",
    "BudgetSemantics",
    "ConstructedPortfolio",
    "ConstructionMode",
    "CovarianceSnapshot",
    "Decision",
    "Direction",
    "DrawdownBand",
    "EffectiveRiskPolicy",
    "ExecutionIntent",
    "IntentError",
    "InvariantViolationError",
    "MetricState",
    "MissingMetricPolicy",
    "PolicyError",
    "PolicyMode",
    "PortfolioDecision",
    "PortfolioRiskReport",
    "PortfolioRiskSnapshot",
    "PortfolioSnapshot",
    "PortfolioStateError",
    "Position",
    "PsdVerdict",
    "ReasonCode",
    "RiskDecision",
    "RiskInputError",
    "RiskOutcome",
    "RiskPolicy",
    "SecurityIdentity",
    "SecurityRiskMetrics",
    "StrategyAllocationPolicy",
    "StrategyBudget",
    "StrategyContribution",
    "StrategyIntent",
    "StrategySleeve",
    "ValidationError",
    "allocate",
    "apply_correlation_concentration",
    "arithmetic_policy",
    "authorize",
    "binding",
    "build_risk_report",
    "build_strategy_report",
    "classify",
    "construct",
    "correlation_of",
    "derive_effective_policy",
    "digest",
    "evaluate",
    "evaluate_request",
    "intents_fingerprint",
    "is_close",
    "is_positive_semidefinite",
    "load_allocation",
    "load_request",
    "load_risk",
    "quantise",
    "stricter_mode",
    "validate_bands",
    "validate_inputs",
]
