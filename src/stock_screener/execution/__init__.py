"""Broker-neutral order lifecycle and deterministic paper execution.

`EXECUTION_ENGINE_V1`. The layer between an authorised portfolio target and an
accounting fill, and the first layer in this system whose output could — once a
real broker adapter exists — move money.

Five authorities, and none may impersonate another:

    strategy          finds opportunities
    portfolio / risk  decides which targets are permitted
    execution         decides how authorised deltas become orders
    broker            decides what fills
    accounting        decides holdings, cash and P&L

Four commitments make that enforceable rather than aspirational.

**An order is not a position.** Creating, submitting or acknowledging one changes
no holding, no cash and no NAV. Only an admitted accounting `FillEvent` changes
the book, and the paper broker holds no cash, positions or P&L of its own — there
is no second number that could disagree with the first.

**A target is not an order.** An approved portfolio and an execution intent are
inert. Orders exist only inside an `ExecutionPlan`, which binds the targets, the
intents, the sizing snapshot, the valued snapshot and the book it was computed
against. Any of those differing at execution time is a refusal, not a best
effort. There is no `force`, no `unsafe_submit` and no arbitrary-order path.

**The handoff is exactly once.** A fill is durable execution evidence before the
book is touched, and the handoff is confirmed only after the book is durable. A
crash between them leaves the fill `PENDING`, and recovery admits it under an
accounting event id *derived* from the fill id — so a second admission is a no-op
rather than a second trade.

**The journal knows its own length.** Append-only, hash-chained, and anchored on
every committed event rather than when somebody remembers. A journal shorter than
its anchor is detected instead of accepted.

What this package never does: contact a broker, hold a credential, place a live or
remote paper order, introduce randomness into a fill, estimate market impact,
optimise an execution parameter, or keep a book of its own.

**It is a deterministic correctness simulator, not a realistic fill simulator.**
No spread, no slippage, no impact, no participation, no latency, no queue. A
strategy run through it will look better than it would trade, by roughly those
costs. The manifest says so, because a simulated return that does not disclose
its cost assumptions invites being read as a forecast.
"""

from __future__ import annotations

from .accounting_adapter import (
    AdmissionResult,
    admit_fill,
    allocate_partial_fill,
    to_accounting_fill_event,
)
from .anchor import (
    JOURNAL_ANCHOR_VERSION,
    ExecutionJournalHeadAnchor,
    JournalAnchorStatus,
    classify_journal_anchor,
)
from .authorization import (
    BookBinding,
    ExecutionPlan,
    PlanStatus,
    intents_fingerprint,
    require_supported_accounting_version,
)
from .engine import ExecutionSession, RecoveryOutcome
from .errors import EXECUTION_ERROR_CODES, Code, ExecutionError
from .events import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionFill,
    FillApplication,
    FillAppliedEvent,
    OrderAcknowledgedEvent,
    OrderCancelledEvent,
    OrderCreatedEvent,
    OrderFilledEvent,
    OrderRejectedEvent,
    OrderSubmittedEvent,
)
from .fingerprint import fingerprint_of
from .journal import (
    GENESIS_HASH,
    ExecutionJournal,
    JournalRecord,
    OrderLedger,
    chain_link,
    verify_journal,
)
from .manifest import EXECUTION_MANIFEST_VERSION, execution_manifest, manifest_fingerprint
from .models import (
    SUPPORTED_ORDER_TYPES,
    SUPPORTED_TIME_IN_FORCE,
    AttributionShare,
    Order,
    OrderSide,
    OrderType,
    TimeInForce,
)
from .paper_broker import VENUE, DeterministicPaperBroker
from .paper_market import ExecutionPrice, PaperExecutionSnapshot, paper_market_policy
from .persistence import (
    ANCHOR_FILENAME,
    DEFAULT_JOURNAL_GENERATION,
    FORBIDDEN_ROOTS,
    STORE_VERSION,
    CommitResult,
    ExecutionStore,
    order_state_fingerprint,
)
from .planner import (
    SEQUENCE_GROUPS,
    ExecutionSizingSnapshot,
    SizingPrice,
    plan_execution,
    sequence_group_of,
)
from .reports import ExecutionPlanReport, OrderReport, build_plan_report
from .run import evaluate_request, load_request, run_paper_execution
from .serde import event_from_json, order_from_json
from .state_machine import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    OrderState,
    OrderStatus,
    transition_table,
)
from .versions import (
    EXECUTION_ENGINE_VERSION,
    FILL_ATTRIBUTION_RULE,
    JOURNAL_CHAIN_VERSION,
    ORDER_SEQUENCING_RULE,
    PAPER_FILL_MODEL,
    PAPER_FILL_POLICY,
    REQUIRED_ACCOUNTING_VERSION,
    engine_policy,
)

__all__ = [
    "ANCHOR_FILENAME",
    "DEFAULT_JOURNAL_GENERATION",
    "EXECUTION_ENGINE_VERSION",
    "EXECUTION_ERROR_CODES",
    "EXECUTION_MANIFEST_VERSION",
    "FILL_ATTRIBUTION_RULE",
    "FORBIDDEN_ROOTS",
    "GENESIS_HASH",
    "JOURNAL_ANCHOR_VERSION",
    "JOURNAL_CHAIN_VERSION",
    "ORDER_SEQUENCING_RULE",
    "PAPER_FILL_MODEL",
    "PAPER_FILL_POLICY",
    "REQUIRED_ACCOUNTING_VERSION",
    "SEQUENCE_GROUPS",
    "STORE_VERSION",
    "SUPPORTED_ORDER_TYPES",
    "SUPPORTED_TIME_IN_FORCE",
    "TERMINAL_STATES",
    "VALID_TRANSITIONS",
    "VENUE",
    "AdmissionResult",
    "AttributionShare",
    "BookBinding",
    "Code",
    "CommitResult",
    "DeterministicPaperBroker",
    "ExecutionError",
    "ExecutionEvent",
    "ExecutionEventType",
    "ExecutionFill",
    "ExecutionJournal",
    "ExecutionJournalHeadAnchor",
    "ExecutionPlan",
    "ExecutionPlanReport",
    "ExecutionPrice",
    "ExecutionSession",
    "ExecutionSizingSnapshot",
    "ExecutionStore",
    "FillApplication",
    "FillAppliedEvent",
    "JournalAnchorStatus",
    "JournalRecord",
    "Order",
    "OrderAcknowledgedEvent",
    "OrderCancelledEvent",
    "OrderCreatedEvent",
    "OrderFilledEvent",
    "OrderLedger",
    "OrderRejectedEvent",
    "OrderReport",
    "OrderSide",
    "OrderState",
    "OrderStatus",
    "OrderSubmittedEvent",
    "OrderType",
    "PaperExecutionSnapshot",
    "PlanStatus",
    "RecoveryOutcome",
    "SizingPrice",
    "TimeInForce",
    "admit_fill",
    "allocate_partial_fill",
    "build_plan_report",
    "chain_link",
    "classify_journal_anchor",
    "engine_policy",
    "evaluate_request",
    "event_from_json",
    "execution_manifest",
    "fingerprint_of",
    "intents_fingerprint",
    "load_request",
    "manifest_fingerprint",
    "order_from_json",
    "order_state_fingerprint",
    "paper_market_policy",
    "plan_execution",
    "require_supported_accounting_version",
    "run_paper_execution",
    "sequence_group_of",
    "to_accounting_fill_event",
    "transition_table",
    "verify_journal",
]
