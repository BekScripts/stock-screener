"""The V1.2 manifest: every contract behind a run that touched a real venue.

A new manifest version rather than a widened one, for the reason this project has
now hit three times. The V1.1 manifest states `external_paper_broker:
NOT_IMPLEMENTED`, and that is *true of V1.1* — a V1.1 engine has no adapter and
cannot reach a venue. Editing it to say otherwise would make every V1.1 artefact
claim a capability its engine did not have, and would move a fingerprint that
exists to prove the opposite. So V1.1 keeps its answer and V1.2 gives its own.

The manifest carries one admission that matters more than the rest of it. A V1
or V1.1 run is reproducible: the inputs are in the repository and the fills
follow from them. **An external run is not.** Its fills came from a venue's
matching engine at a moment that will not recur, and no amount of recorded
policy brings them back. The manifest therefore records what the run *did* and
what it was *bound to*, and states plainly that re-running it will not reproduce
it — which is the difference between an audit record and a reproducibility
claim, and conflating them would be the dishonest part.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..errors import V1_1_ADDED_ERROR_CODES, V1_2_ADDED_ERROR_CODES, V1_ERROR_CODES
from ..manifest import manifest_fingerprint
from ..persistence import STORE_VERSION
from ..versions import (
    DAY_EXPIRY_POLICY,
    EXECUTION_ENGINE_VERSION_V1_2,
    FILL_ATTRIBUTION_RULE,
    JOURNAL_CHAIN_VERSION,
    ORDER_SEQUENCING_RULE,
    REQUIRED_ACCOUNTING_VERSION,
    SESSION_ROLLOVER_POLICY,
)
from .binding import ADAPTER_VERSION
from .client_ids import CLIENT_ID_DERIVATION
from .reconciliation import EXTERNAL_RECONCILIATION_POLICY
from .session import VENUE_BINDING_POLICY, ExecutionVenue
from .status import STATUS_MAP_POLICY, status_map_policy
from .submission import SUBMISSION_PROTOCOL
from .symbols import SYMBOL_MAPPING_POLICY

if TYPE_CHECKING:
    from .session import VenueBinding
    from .symbols import SymbolMapping

__all__ = [
    "EXECUTION_MANIFEST_VERSION_V1_2",
    "external_manifest",
]

#: The V1.2 manifest contract.
EXECUTION_MANIFEST_VERSION_V1_2 = "EXECUTION_MANIFEST_V1_2"


def external_manifest(
    *,
    venue: VenueBinding,
    mapping: SymbolMapping,
    execution_plan_fingerprint: str | None = None,
    journal_generation: str | None = None,
    journal_sequence: int | None = None,
    journal_head: str | None = None,
    journal_anchor_fingerprint: str | None = None,
    accounting_generation: str | None = None,
    accounting_version: str | None = None,
    initial_accounting_state: str | None = None,
    final_accounting_state: str | None = None,
    readiness_verdict: str | None = None,
    reconciliation_verdict: str | None = None,
    submitted_order_ids: tuple[str, ...] = (),
    unresolved_order_ids: tuple[str, ...] = (),
    code_commit: str | None = None,
) -> dict[str, Any]:
    """Collect every policy and binding behind a V1.2 execution run.

    Args:
        venue: Which venue the run was bound to, and the account behind it.
        mapping: The declared symbol mapping.
        execution_plan_fingerprint: The authorisation.
        journal_generation: The journal lineage.
        journal_sequence: The last committed journal sequence.
        journal_head: The journal's chain head.
        journal_anchor_fingerprint: The durable anchor's digest.
        accounting_generation: The book's ledger lineage.
        accounting_version: The book's engine version.
        initial_accounting_state: The book before the run.
        final_accounting_state: The book afterwards.
        readiness_verdict: What preflight concluded.
        reconciliation_verdict: What the three-way sweep concluded.
        submitted_order_ids: Which orders were submitted.
        unresolved_order_ids: Which submissions are still unresolved. Recorded
            in the manifest rather than only in a log, because a run that ended
            with an unknown order is not a clean run and the artefact should say
            so without being asked.
        code_commit: The commit the engine ran from.

    Returns:
        The manifest, with its own fingerprint.
    """
    external = venue.venue is ExecutionVenue.EXTERNAL_PAPER
    body: dict[str, Any] = {
        "manifest_version": EXECUTION_MANIFEST_VERSION_V1_2,
        "policies": {
            "engine": EXECUTION_ENGINE_VERSION_V1_2,
            "venue_binding": VENUE_BINDING_POLICY,
            "adapter": ADAPTER_VERSION,
            "submission_protocol": SUBMISSION_PROTOCOL,
            "client_order_id": CLIENT_ID_DERIVATION,
            "symbol_mapping": SYMBOL_MAPPING_POLICY,
            "status_map": STATUS_MAP_POLICY,
            "reconciliation": EXTERNAL_RECONCILIATION_POLICY,
            "expiry": DAY_EXPIRY_POLICY,
            "rollover": SESSION_ROLLOVER_POLICY,
            "order_sequencing": ORDER_SEQUENCING_RULE,
            "fill_attribution": FILL_ATTRIBUTION_RULE,
            "journal_chain": JOURNAL_CHAIN_VERSION,
            "store": STORE_VERSION,
            "required_accounting_version": REQUIRED_ACCOUNTING_VERSION,
        },
        "venue": venue.as_json(),
        "symbol_mapping": mapping.as_json(),
        "provider_status_map": status_map_policy(),
        "bindings": {
            "execution_plan_fingerprint": execution_plan_fingerprint,
            "journal_generation": journal_generation,
            "journal_sequence": journal_sequence,
            "journal_head": journal_head,
            "journal_anchor_fingerprint": journal_anchor_fingerprint,
            "accounting_generation": accounting_generation,
            "accounting_version": accounting_version,
            "initial_accounting_state": initial_accounting_state,
            "final_accounting_state": final_accounting_state,
            "readiness_verdict": readiness_verdict,
            "reconciliation_verdict": reconciliation_verdict,
            "submitted_order_ids": list(submitted_order_ids),
            "unresolved_order_ids": list(unresolved_order_ids),
            "code_commit": code_commit,
        },
        "error_codes": sorted(V1_ERROR_CODES | V1_1_ADDED_ERROR_CODES | V1_2_ADDED_ERROR_CODES),
        "v1_1_added_error_codes": sorted(V1_1_ADDED_ERROR_CODES),
        "v1_2_added_error_codes": sorted(V1_2_ADDED_ERROR_CODES),
        "invariants": [
            "an order changes no holding, no cash and no NAV",
            "only an admitted accounting FillEvent changes the book",
            "every order originates from an authorised execution plan",
            "cumulative filled quantity never exceeds the ordered quantity",
            "a fill maps to exactly one accounting event, derived from its id",
            "the broker holds no cash, no positions and no P&L",
            "exactly one mutating request per submission call",
            "a client order id is derived from the local order id, never generated",
            "a submission intent is durable before any request is made",
            "an ambiguous submission is resolved by reading, never by resending",
            "a session is bound to exactly one venue and never falls back",
            "a reconciliation break is reported and never repaired",
            "no credential, token or authorization header is persisted or logged",
        ],
        "refusals": [
            "any environment other than PAPER",
            "a host not on the paper allow-list",
            "a credential whose key id does not carry a paper prefix",
            "a provider account the binding does not name",
            "an account the provider says is not a paper account",
            "a provider order status this adapter has not been taught",
            "an identity with no declared venue symbol",
            "a venue symbol mapped from two identities",
            "a fractional quantity at a venue that would round it",
            "an order type or time-in-force the venue does not support",
            "an execution the venue reported without a durable id",
            "a fill whose fee the venue did not state and no schedule declared",
            "a submission while an earlier intent is unresolved",
        ],
        "live_trading": "NOT_AUTHORIZED",
        "live_broker": "NOT_IMPLEMENTED",
        "external_paper_broker": "IMPLEMENTED_PAPER_ONLY",
        "not_guaranteed": [
            (
                "COORDINATED_LOCAL_ROLLBACK_DETECTION: a journal and its local "
                "anchor rolled back together to a mutually consistent earlier "
                "state cannot be detected from local files alone."
            ),
            (
                "REPRODUCIBILITY OF AN EXTERNAL RUN: a run bound to "
                "EXTERNAL_PAPER is not reproducible. Its fills came from a "
                "venue's matching engine at a moment that will not recur, and "
                "nothing recorded here brings them back. This manifest is an "
                "audit record of what was bound and what happened — it is not a "
                "claim that re-running it reproduces it."
            )
            if external
            else (
                "REPRODUCIBILITY: this run was bound to the local deterministic "
                "venue, so it reproduces from its inputs. An EXTERNAL_PAPER run "
                "would not."
            ),
            (
                "PAPER FILL REALISM AT THE VENUE: a provider's paper engine is "
                "the provider's model of itself. It may fill more readily than "
                "its live engine would, it is not bound by this project's "
                "participation ceiling, and its queue behaviour is unobservable "
                "from here. A paper fill is evidence that the plumbing works, "
                "not evidence that the trade was achievable."
            ),
            (
                "PROOF OF PAPER FROM CONFIGURATION ALONE: a host allow-list and "
                "a key prefix establish what was configured, not what the "
                "credential can reach. Only the account's own answer adds that, "
                "and only for as long as the credential is unchanged."
            ),
        ],
    }
    body["manifest_fingerprint"] = manifest_fingerprint(body)
    return body
