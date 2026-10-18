"""The AlphaTrader strategy authority.

The layer that turns the frozen research record into desired exposure. It is the
first link in the operational authority chain and the one the system was missing:

    research evidence
        -> STRATEGY (this package)
        -> portfolio construction
        -> PORTFOLIO_RISK_ENGINE_V1_2
        -> EXECUTION_ENGINE_V1_2
        -> EXTERNAL_PAPER
        -> PORTFOLIO_ACCOUNTING_V2

What this package may do: read stored research evidence, rank it, and express a
relative desired exposure as the existing `StrategyIntent`.

What it may not do, enforced by having no code and no import for any of it: size
a position against portfolio cash, read broker state or buying power, construct
or submit an order, write an accounting event, repair a reconciliation, or reach
a network. The risk engine is the only sizing authority and execution is the only
order authority; this package can neither override nor anticipate them.
"""

from __future__ import annotations

from .authorization import (
    AUTHORIZED_ENVIRONMENTS,
    FORBIDDEN_ENVIRONMENTS,
    STRATEGY_AUTHORIZATION_CONTRACT,
    STRATEGY_AUTHORIZATION_CONTRACT_V2,
    StrategyDeploymentAuthorization,
    StrategyDeploymentAuthorizationV2,
    require_authorization,
)
from .compounder_monthly import (
    CADENCE,
    DECILE_FRACTION,
    SELECTION_RULE,
    STRATEGY_ID,
    STRATEGY_VERSION,
    TARGET_COUNT_RULE,
    CompounderMonthlyStrategy,
    decision_sessions,
    is_decision_session,
    target_count,
)
from .compounder_monthly_fractionable import (
    DEPLOYMENT_EXCLUSION_REASON,
    STRATEGY_VERSION_V2,
    CompounderMonthlyFractionableStrategy,
    DeploymentUniverseView,
)
from .errors import Code, StrategyError
from .fingerprint import FINGERPRINT_SCHEME, strategy_fingerprint_of
from .models import (
    StrategyDecision,
    StrategyInputBundle,
    StrategyInputRecord,
    StrategySelection,
)
from .run import (
    STRATEGY_MANIFEST_VERSION,
    StrategyRunReport,
    evaluate_session,
    load_bundle,
    strategy_manifest,
)
from .state import STRATEGY_STATE_VERSION, StrategyState, StrategyStateStore

__all__ = [
    "AUTHORIZED_ENVIRONMENTS",
    "CADENCE",
    "DECILE_FRACTION",
    "DEPLOYMENT_EXCLUSION_REASON",
    "FINGERPRINT_SCHEME",
    "FORBIDDEN_ENVIRONMENTS",
    "SELECTION_RULE",
    "STRATEGY_AUTHORIZATION_CONTRACT",
    "STRATEGY_AUTHORIZATION_CONTRACT_V2",
    "STRATEGY_ID",
    "STRATEGY_MANIFEST_VERSION",
    "STRATEGY_STATE_VERSION",
    "STRATEGY_VERSION",
    "STRATEGY_VERSION_V2",
    "TARGET_COUNT_RULE",
    "Code",
    "CompounderMonthlyFractionableStrategy",
    "CompounderMonthlyStrategy",
    "DeploymentUniverseView",
    "StrategyDecision",
    "StrategyDeploymentAuthorization",
    "StrategyDeploymentAuthorizationV2",
    "StrategyError",
    "StrategyInputBundle",
    "StrategyInputRecord",
    "StrategyRunReport",
    "StrategySelection",
    "StrategyState",
    "StrategyStateStore",
    "decision_sessions",
    "evaluate_session",
    "is_decision_session",
    "load_bundle",
    "require_authorization",
    "strategy_fingerprint_of",
    "strategy_manifest",
    "target_count",
]
