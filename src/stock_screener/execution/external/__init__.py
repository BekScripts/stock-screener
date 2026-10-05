"""External paper-broker adapter. Paper only, and structurally so.

`EXECUTION_ENGINE_V1_2`. This is the first code in the project that can cause
something to happen outside this process, and the whole subpackage is shaped
around containing that.

**There is no live trading, and no flag that turns it on.** `BrokerEnvironment`
can name `LIVE` because refusing something requires being able to say it, and
`is_authorized` is false for it everywhere. `refuse_live` runs when a binding is
built, when an adapter is constructed and when an order is submitted. There is no
`USE_LIVE`, no `--live`, no `mode=live`, no `fallback_to_live`, and no
configuration value anywhere that would change the answer: a live host is
rejected by the allow-list, a live key prefix is rejected by the credential
check, and an unknown value for either is rejected rather than defaulted.
Configuration is not treated as proof — three independent checks must agree, and
the account's own answer is what upgrades a proof to certified.

**One mutating request per submission, and no retry of one.** A `POST` that
times out may have created an order, so it is never repeated. The client order
id is *derived* from the local order id, which means the order can still be named
after a crash and asked about; resolution reads, and an answer that does not
settle the question leaves `REMOTE_STATE_UNKNOWN` standing rather than guessing.

**The venue holds no authority.** A remote position is evidence to reconcile
against, not a book. Three records are compared — journal, accounting, venue —
and none of them is repaired: there is no function here that adopts a broker's
figure, because a break means one of the three is wrong and overwriting the book
destroys the evidence of which.

**Nothing here is reproducible, and the manifest says so.** A V1/V1.1 run
reproduces from its inputs. An external run's fills came from a venue at a
moment that will not recur. The V1.2 manifest is an audit record, not a
reproducibility claim.

What this subpackage never does: trade live, hold a live endpoint, persist or log
a credential, retry a mutation, repair a reconciliation break, transfer or fund
an account, liquidate a position in one call, or let one session mix simulated
and venue fills.
"""

from __future__ import annotations

from .alpaca import ACCOUNT_PATH, ACTIVITIES_PATH, POSITIONS_PATH, PROVIDER, AlpacaPaperAdapter
from .binding import (
    ACCOUNT_FINGERPRINT_SALT,
    ADAPTER_VERSION,
    BrokerCapabilities,
    ExternalBrokerAccountBinding,
    account_fingerprint,
)
from .client_ids import (
    CLIENT_ID_DERIVATION,
    CLIENT_ID_NAMESPACE,
    CLIENT_ID_PREFIX,
    client_order_id_for,
    is_our_client_order_id,
)
from .environment import (
    LIVE_HOSTS,
    PAPER_HOSTS,
    PAPER_KEY_PREFIXES,
    BrokerEnvironment,
    EnvironmentProof,
    prove_paper_environment,
    refuse_live,
)
from .manifest import EXECUTION_MANIFEST_VERSION_V1_2, external_manifest
from .models import (
    ExternalExecutionFill,
    ExternalOrderSnapshot,
    ExternalPaperAccountSnapshot,
    ExternalPosition,
)
from .preflight import PREFLIGHT_GATES, READY_VERDICT, PaperBrokerReadiness, preflight
from .reconciliation import (
    EXTERNAL_RECONCILIATION_POLICY,
    ExternalBreak,
    ExternalBreakSeverity,
    ExternalReconciliationResult,
    ReconciliationVerdict,
    reconcile_external,
)
from .report import (
    CREDENTIAL_PATTERNS,
    ExternalPaperExecutionReport,
    check_no_credentials,
)
from .session import VENUE_BINDING_POLICY, ExecutionVenue, VenueBinding
from .status import (
    PROVIDER_STATUS_MAP,
    STATUS_MAP_POLICY,
    StatusReading,
    read_provider_status,
    status_map_policy,
)
from .submission import (
    ORDERS_PATH,
    SUBMISSION_PROTOCOL,
    SubmissionIntent,
    SubmissionIntentLog,
    SubmissionOutcome,
    SubmissionResult,
    resolve_submission,
    submit_order,
)
from .symbols import SYMBOL_MAPPING_POLICY, SymbolMapping, VenueSymbol
from .transport import (
    READ_RETRY_LIMIT,
    BrokerResponse,
    BrokerTransport,
    FakeTransport,
    RequestMethod,
    TransportOutcome,
    read_with_retry,
)

__all__ = [
    "ACCOUNT_FINGERPRINT_SALT",
    "ACCOUNT_PATH",
    "ACTIVITIES_PATH",
    "ADAPTER_VERSION",
    "CLIENT_ID_DERIVATION",
    "CLIENT_ID_NAMESPACE",
    "CLIENT_ID_PREFIX",
    "CREDENTIAL_PATTERNS",
    "EXECUTION_MANIFEST_VERSION_V1_2",
    "EXTERNAL_RECONCILIATION_POLICY",
    "LIVE_HOSTS",
    "ORDERS_PATH",
    "PAPER_HOSTS",
    "PAPER_KEY_PREFIXES",
    "POSITIONS_PATH",
    "PREFLIGHT_GATES",
    "PROVIDER",
    "PROVIDER_STATUS_MAP",
    "READY_VERDICT",
    "READ_RETRY_LIMIT",
    "STATUS_MAP_POLICY",
    "SUBMISSION_PROTOCOL",
    "SYMBOL_MAPPING_POLICY",
    "VENUE_BINDING_POLICY",
    "AlpacaPaperAdapter",
    "BrokerCapabilities",
    "BrokerEnvironment",
    "BrokerResponse",
    "BrokerTransport",
    "EnvironmentProof",
    "ExecutionVenue",
    "ExternalBreak",
    "ExternalBreakSeverity",
    "ExternalBrokerAccountBinding",
    "ExternalExecutionFill",
    "ExternalOrderSnapshot",
    "ExternalPaperAccountSnapshot",
    "ExternalPaperExecutionReport",
    "ExternalPosition",
    "ExternalReconciliationResult",
    "FakeTransport",
    "PaperBrokerReadiness",
    "ReconciliationVerdict",
    "RequestMethod",
    "StatusReading",
    "SubmissionIntent",
    "SubmissionIntentLog",
    "SubmissionOutcome",
    "SubmissionResult",
    "SymbolMapping",
    "TransportOutcome",
    "VenueBinding",
    "VenueSymbol",
    "account_fingerprint",
    "check_no_credentials",
    "client_order_id_for",
    "external_manifest",
    "is_our_client_order_id",
    "preflight",
    "prove_paper_environment",
    "read_provider_status",
    "read_with_retry",
    "reconcile_external",
    "refuse_live",
    "resolve_submission",
    "status_map_policy",
    "submit_order",
]
