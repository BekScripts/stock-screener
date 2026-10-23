"""Portfolio state, NAV/P&L accounting and reconciliation.

This package answers one question the risk engine cannot: *what is actually
owned?* P1.0 through P1.2 decide what a portfolio **should** hold — allocations,
ceilings, budgets, approved targets. None of that is evidence that anything was
bought. A target is a statement about intent; a holding is a fact about the
world, and the distinction is the reason this package exists as its own layer.

The design rests on four commitments:

**State is derived, never set.** A `PortfolioState` comes from replaying an
append-only ledger of events and from nowhere else. There is no setter, no
constructor from a broker statement, and no patch. The ledger's hash chain makes
tampering, deletion, reordering and insertion detectable from the records alone.

**Accounting is exact.** Every figure is a `Decimal`, quantised by one policy, and
the identities are compared exactly rather than within a tolerance. The one
boundary to the risk layer's `float64` is a single adapter, in one direction.

**Refusal beats approximation.** A buy without the cash, a sale beyond the
holding, a mark from after the valuation instant, a valuation missing a position,
a corporate action whose economics are not modelled — each is refused by name.
None of them is a number this engine will guess at, because a guessed cost basis
is indistinguishable from a correct one downstream.

**Reconciliation never repairs.** A difference from an external account is
evidence that something happened the ledger was not told about. Adopting the
external figure destroys that evidence and leaves the cause in place. There is no
repair function here and it cannot be configured to have one.

What this package deliberately never does: place or cancel an order, simulate a
fill, count a deposit as performance, infer a dividend entitlement, write a risk
target into a holding, or touch a research database.
"""

from __future__ import annotations

from .accounting import (
    ACCOUNTING_ENGINE_VERSION,
    CURRENT_ACCOUNTING_VERSION,
    AppliedEvent,
    apply_event,
    replay,
)
from .adapter import (
    DECIMAL_TO_FLOAT_SCALE,
    RISK_ADAPTER_VERSION,
    seam_policy,
    to_float,
    to_risk_snapshot,
)
from .anchor import (
    ANCHOR_VERSION,
    AnchorStatus,
    LedgerHeadAnchor,
    classify_anchor,
    state_fingerprint,
)
from .attribution import (
    ATTRIBUTION_POLICY,
    AttributionReport,
    StrategyAttribution,
    attribute_portfolio,
)
from .corporate_actions import (
    CORPORATE_ACTION_POLICY,
    SUPPORTED_ACTIONS,
    action_policy,
    classify_action,
    refuse_unsupported,
)
from .errors import (
    ACCOUNTING_ERROR_CODES,
    ANCHOR_ERROR_CODES,
    V1_ERROR_CODES,
    V2_ERROR_CODES,
    AccountingError,
    Code,
)
from .events import (
    UNATTRIBUTED,
    UNSUPPORTED_ACTIONS,
    CashDividendEvent,
    CashFlowDirection,
    CorporateActionEvent,
    EventType,
    ExternalCashFlowEvent,
    FillEvent,
    OpeningBalanceEvent,
    OpeningPosition,
    PortfolioEvent,
    Side,
    SplitEvent,
    StrategySlice,
    event_fingerprint,
)
from .external_anchor import (
    EXTERNAL_ANCHOR_CONTRACT,
    ExternalAnchorRecord,
    ExternalAnchorSink,
    InMemoryExternalAnchor,
    compare_external_anchor,
)
from .fingerprint import (
    ACCOUNTING_MANIFEST_VERSION,
    ACCOUNTING_MANIFEST_VERSION_V2,
    accounting_manifest,
    manifest_fingerprint,
)
from .ledger import (
    GENESIS_HASH,
    LEDGER_CHAIN_VERSION,
    LedgerRecord,
    PortfolioLedger,
    chain_link,
    verify_chain,
)
from .migration import (
    MIGRATION_MODE,
    MigrationReport,
    migrate_from_events,
    refuse_snapshot_migration,
)
from .money import (
    CASH_SCALE,
    MONEY_POLICY_ID,
    PRICE_SCALE,
    QUANTITY_SCALE,
    ZERO,
    as_decimal,
    is_zero,
    money_policy,
    quantise_cash,
    quantise_price,
    quantise_quantity,
)
from .persistence import (
    ANCHOR_FILENAME,
    DEFAULT_GENERATION,
    FORBIDDEN_ROOTS,
    STORE_VERSION,
    AppendResult,
    Checkpoint,
    PortfolioStore,
    RecoveryOutcome,
    event_from_json,
)
from .pnl import (
    PNL_POLICY_ID,
    PnlDecomposition,
    PositionValuation,
    ValuedPortfolioSnapshot,
    value_portfolio,
)
from .pools import (
    PER_ATTRIBUTION_COST_POLICY,
    AttributionCostPool,
    PooledPosition,
    PoolSaleAllocation,
)
from .positions import COST_BASIS_POLICY, PositionState, SaleAllocation, allocate_sale
from .reconciliation import (
    RECONCILIATION_POLICY,
    Break,
    BreakSeverity,
    ExternalPosition,
    ExternalStatement,
    ReconciliationResult,
    reconcile,
    statement_from,
)
from .run import evaluate_request, load_ledger, load_request
from .state import AnyPosition, CashSummary, PortfolioState
from .valuation import (
    DEFAULT_STALENESS_LIMIT_HOURS,
    MARK_POLICY_ID,
    MarkPrice,
    MarkPriceSnapshot,
    ValuationCoverage,
    marks_from,
    value_positions,
)
from .versions import (
    ACCOUNTING_V1,
    ACCOUNTING_V2,
    DEFAULT_ACCOUNTING_VERSION,
    AccountingVersion,
    resolve_version,
    version_policy,
)

__all__ = [
    "ACCOUNTING_ENGINE_VERSION",
    "ACCOUNTING_ERROR_CODES",
    "ACCOUNTING_MANIFEST_VERSION",
    "ACCOUNTING_MANIFEST_VERSION_V2",
    "ACCOUNTING_V1",
    "ACCOUNTING_V2",
    "ANCHOR_ERROR_CODES",
    "ANCHOR_FILENAME",
    "ANCHOR_VERSION",
    "ATTRIBUTION_POLICY",
    "CASH_SCALE",
    "CORPORATE_ACTION_POLICY",
    "COST_BASIS_POLICY",
    "CURRENT_ACCOUNTING_VERSION",
    "DECIMAL_TO_FLOAT_SCALE",
    "DEFAULT_ACCOUNTING_VERSION",
    "DEFAULT_GENERATION",
    "DEFAULT_STALENESS_LIMIT_HOURS",
    "EXTERNAL_ANCHOR_CONTRACT",
    "FORBIDDEN_ROOTS",
    "GENESIS_HASH",
    "LEDGER_CHAIN_VERSION",
    "MARK_POLICY_ID",
    "MIGRATION_MODE",
    "MONEY_POLICY_ID",
    "PER_ATTRIBUTION_COST_POLICY",
    "PNL_POLICY_ID",
    "PRICE_SCALE",
    "QUANTITY_SCALE",
    "RECONCILIATION_POLICY",
    "RISK_ADAPTER_VERSION",
    "STORE_VERSION",
    "SUPPORTED_ACTIONS",
    "UNATTRIBUTED",
    "UNSUPPORTED_ACTIONS",
    "V1_ERROR_CODES",
    "V2_ERROR_CODES",
    "ZERO",
    "AccountingError",
    "AccountingVersion",
    "AnchorStatus",
    "AnyPosition",
    "AppendResult",
    "AppliedEvent",
    "AttributionCostPool",
    "AttributionReport",
    "Break",
    "BreakSeverity",
    "CashDividendEvent",
    "CashFlowDirection",
    "CashSummary",
    "Checkpoint",
    "Code",
    "CorporateActionEvent",
    "EventType",
    "ExternalAnchorRecord",
    "ExternalAnchorSink",
    "ExternalCashFlowEvent",
    "ExternalPosition",
    "ExternalStatement",
    "FillEvent",
    "InMemoryExternalAnchor",
    "LedgerHeadAnchor",
    "LedgerRecord",
    "MarkPrice",
    "MarkPriceSnapshot",
    "MigrationReport",
    "OpeningBalanceEvent",
    "OpeningPosition",
    "PnlDecomposition",
    "PoolSaleAllocation",
    "PooledPosition",
    "PortfolioEvent",
    "PortfolioLedger",
    "PortfolioState",
    "PortfolioStore",
    "PositionState",
    "PositionValuation",
    "ReconciliationResult",
    "RecoveryOutcome",
    "SaleAllocation",
    "Side",
    "SplitEvent",
    "StrategyAttribution",
    "StrategySlice",
    "ValuationCoverage",
    "ValuedPortfolioSnapshot",
    "accounting_manifest",
    "action_policy",
    "allocate_sale",
    "apply_event",
    "as_decimal",
    "attribute_portfolio",
    "chain_link",
    "classify_action",
    "classify_anchor",
    "compare_external_anchor",
    "evaluate_request",
    "event_fingerprint",
    "event_from_json",
    "is_zero",
    "load_ledger",
    "load_request",
    "manifest_fingerprint",
    "marks_from",
    "migrate_from_events",
    "money_policy",
    "quantise_cash",
    "quantise_price",
    "quantise_quantity",
    "reconcile",
    "refuse_snapshot_migration",
    "refuse_unsupported",
    "replay",
    "resolve_version",
    "seam_policy",
    "state_fingerprint",
    "statement_from",
    "to_float",
    "to_risk_snapshot",
    "value_portfolio",
    "value_positions",
    "verify_chain",
    "version_policy",
]
