"""The deployed paper-operations runtime.

`paper_operations` is the control plane: it decides whether the system may act.
`paper_runtime` is one *deployment* of it — which paper account, which approved
strategy build, which accounting ledger, which execution journal, which
calendar — and the orchestration that connects the authorities together.

The split matters because the control plane is reusable and a deployment is
not. A coordinator can gate any environment; a runtime is bound to one account
and one book, and a run that opened the wrong book would reconcile valid
records belonging to something else against the right venue.

What this package may do: orchestrate. It calls the strategy, the portfolio and
risk engine, the execution planner, the certified external-paper adapter, the
accounting ledger and the reconciliation — and reimplements none of them.

What it may not do: construct a broker order, submit one directly, write an
accounting event outside the one-time bootstrap, repair a reconciliation, or
reach a live venue. There is no code and no import for any of it.
"""

from __future__ import annotations

from .binding import (
    PAPER_OPERATIONS_RUNTIME_BINDING_VERSION,
    RUNTIME_DIRECTORY,
    PaperOperationsRuntimeBinding,
    RuntimeBindingStore,
)
from .bootstrap import (
    BOOTSTRAP_ARTEFACT_VERSION,
    CASH_FIELD,
    PaperBookBootstrapReport,
    bootstrap_paper_book,
    read_bootstrap,
)
from .decisions import (
    DECISION_TRANSITIONS,
    DECISIONS_DIRECTORY,
    PERSISTED_DECISION_VERSION,
    DecisionStatus,
    PersistedStrategyDecision,
    StrategyDecisionStore,
)
from .environment import (
    RUNTIME_ENVIRONMENT_VERSION,
    AlphaTraderPaperOperationsEnvironment,
)
from .errors import Code, RuntimeBindingError
from .orchestration import RuntimeOrchestrationOutcome, RuntimePlanner

__all__ = [
    "BOOTSTRAP_ARTEFACT_VERSION",
    "CASH_FIELD",
    "DECISIONS_DIRECTORY",
    "DECISION_TRANSITIONS",
    "PAPER_OPERATIONS_RUNTIME_BINDING_VERSION",
    "PERSISTED_DECISION_VERSION",
    "RUNTIME_DIRECTORY",
    "RUNTIME_ENVIRONMENT_VERSION",
    "AlphaTraderPaperOperationsEnvironment",
    "Code",
    "DecisionStatus",
    "PaperBookBootstrapReport",
    "PaperOperationsRuntimeBinding",
    "PersistedStrategyDecision",
    "RuntimeBindingError",
    "RuntimeBindingStore",
    "RuntimeOrchestrationOutcome",
    "RuntimePlanner",
    "StrategyDecisionStore",
    "bootstrap_paper_book",
    "read_bootstrap",
]
