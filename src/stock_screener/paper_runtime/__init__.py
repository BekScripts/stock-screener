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
from .composition import (
    AUDIT_DIRECTORY,
    PRODUCTION_COMPOSITION_VERSION,
    OperationsComposition,
    compose_operations,
    composition_readiness,
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
from .missed import (
    MISSED_SLOT_VERSION,
    MissedSlotRecord,
    expire_pending_executions,
    missed_records,
    record_missed_slot,
)
from .observability import (
    OBSERVABILITY_VERSION,
    EventStatus,
    OperationalEventSpec,
    event_vocabulary,
    observability_contract,
    observability_fingerprint,
)
from .operator_inputs import (
    OPERATOR_INPUTS_VERSION,
    RISK_POLICY_FILENAME,
    SYMBOL_MAPPING_FILENAME,
    OperatorInputStatus,
    operator_input_status,
    read_risk_policy,
    read_symbol_mapping,
    stored_marks,
    write_risk_policy,
    write_symbol_mapping,
)
from .orchestration import RuntimeOrchestrationOutcome, RuntimePlanner
from .profiles import (
    OPERATIONAL_EXECUTION_TIMING_VERSION,
    OPERATIONAL_SCORE_AVAILABILITY_VERSION,
    PERFORMANCE_ATTRIBUTION_BOUNDARY,
    execution_timing_profile,
    score_availability_profile,
)
from .risk_transformation import (
    RISK_AUTHORITY_RULE,
    RISK_TRANSFORMATION_VERSION,
    RiskTransformation,
    RiskTransformationRow,
    transformation_for,
)
from .submission import SUBMISSION_AUTHORITY_VERSION, RuntimeSubmissionAuthority

__all__ = [
    "AUDIT_DIRECTORY",
    "BOOTSTRAP_ARTEFACT_VERSION",
    "CASH_FIELD",
    "DECISIONS_DIRECTORY",
    "DECISION_TRANSITIONS",
    "MISSED_SLOT_VERSION",
    "OBSERVABILITY_VERSION",
    "OPERATIONAL_EXECUTION_TIMING_VERSION",
    "OPERATIONAL_SCORE_AVAILABILITY_VERSION",
    "OPERATOR_INPUTS_VERSION",
    "PAPER_OPERATIONS_RUNTIME_BINDING_VERSION",
    "PERFORMANCE_ATTRIBUTION_BOUNDARY",
    "PERSISTED_DECISION_VERSION",
    "PRODUCTION_COMPOSITION_VERSION",
    "RISK_AUTHORITY_RULE",
    "RISK_POLICY_FILENAME",
    "RISK_TRANSFORMATION_VERSION",
    "RUNTIME_DIRECTORY",
    "RUNTIME_ENVIRONMENT_VERSION",
    "SUBMISSION_AUTHORITY_VERSION",
    "SYMBOL_MAPPING_FILENAME",
    "AlphaTraderPaperOperationsEnvironment",
    "Code",
    "DecisionStatus",
    "EventStatus",
    "MissedSlotRecord",
    "OperationalEventSpec",
    "OperationsComposition",
    "OperatorInputStatus",
    "PaperBookBootstrapReport",
    "PaperOperationsRuntimeBinding",
    "PersistedStrategyDecision",
    "RiskTransformation",
    "RiskTransformationRow",
    "RuntimeBindingError",
    "RuntimeBindingStore",
    "RuntimeOrchestrationOutcome",
    "RuntimePlanner",
    "RuntimeSubmissionAuthority",
    "StrategyDecisionStore",
    "bootstrap_paper_book",
    "compose_operations",
    "composition_readiness",
    "event_vocabulary",
    "execution_timing_profile",
    "expire_pending_executions",
    "missed_records",
    "observability_contract",
    "observability_fingerprint",
    "operator_input_status",
    "read_bootstrap",
    "read_risk_policy",
    "read_symbol_mapping",
    "record_missed_slot",
    "score_availability_profile",
    "stored_marks",
    "transformation_for",
    "write_risk_policy",
    "write_symbol_mapping",
]
