"""The client order id, derived rather than generated.

This is the mechanism that makes an ambiguous submission survivable, so it is
worth being precise about why it is built the way it is.

A `POST` that times out may or may not have created an order. The only safe way
to find out is to ask the venue about *that specific order*, which requires a
name for it that was decided before the request was sent and that can be
recomputed afterwards from local state alone. A generated id — `uuid4()`, a
counter, anything carrying a clock — cannot be recomputed after a crash: the
process that knew it is gone, and the order is now an orphan at the venue that no
local record can name. So there is no UUID here and no clock.

The id is a pure function of the local order id. Deliberately **not** of the
account binding: a binding's fingerprint moves when a capability declaration or
a URL changes, and an id that moved with it would let the same order be submitted
a second time under a new name after a configuration edit. Venue scoping is the
provider's job — client ids are unique per account there — so including the
account would buy nothing and cost idempotency.

The id is also recognisable. Reconciliation has to tell an order this system
placed from one placed by hand in the provider's web console, and
`UNEXPECTED_REMOTE_ORDER` is only meaningful if "ours" is decidable from the
string itself.
"""

from __future__ import annotations

import hashlib
import re

from stock_screener.execution.errors import Code, ExecutionError

__all__ = [
    "CLIENT_ID_DERIVATION",
    "CLIENT_ID_NAMESPACE",
    "CLIENT_ID_PATTERN",
    "CLIENT_ID_PREFIX",
    "DIGEST_CHARS",
    "client_order_id_for",
    "is_our_client_order_id",
]

#: How the id is derived. In the manifest because an artefact's reproducibility
#: depends on it: change this and every outstanding order loses its name.
CLIENT_ID_DERIVATION = "DERIVED_CLIENT_ORDER_ID_SHA256_V1"

#: Domain separation, so this project's ids cannot collide with another system's
#: derivation over the same order ids.
CLIENT_ID_NAMESPACE = "STOCK_SCREENER_EXECUTION_EXTERNAL_PAPER_V1"

#: Marks an id as minted by this adapter.
CLIENT_ID_PREFIX = "ssx1-"

#: How much of the digest is kept. 40 hex characters is 160 bits — far beyond
#: any collision risk for a paper book, and short enough to fit the shortest
#: client-id limit a provider has imposed.
DIGEST_CHARS = 40

#: What one of our ids looks like.
CLIENT_ID_PATTERN = re.compile(rf"^{re.escape(CLIENT_ID_PREFIX)}[0-9a-f]{{{DIGEST_CHARS}}}$")


def client_order_id_for(order_id: str, *, limit: int = 48) -> str:
    """Derive the venue-facing client order id for a local order.

    Pure and total: the same order id yields the same client id in this process,
    in the next one, and after a crash — which is what lets an ambiguous
    submission be resolved by asking the venue rather than by resending.

    Args:
        order_id: The local order id, including a session child id.
        limit: The provider's maximum client-id length.

    Returns:
        The client order id.

    Raises:
        ExecutionError: `EXECUTION_EVENT_INVALID` if the order id is blank;
            `BROKER_CAPABILITY_UNSUPPORTED` if the provider's limit is shorter
            than a derived id. Truncating to fit is refused rather than done: a
            truncated id is a *different* name, and two orders could then share
            it, which is how one order's fills get attributed to another.
    """
    cleaned = order_id.strip()
    if not cleaned:
        raise ExecutionError(
            Code.EXECUTION_EVENT_INVALID,
            "a client order id must be derived from an order id, and none was given",
        )
    minimum = len(CLIENT_ID_PREFIX) + DIGEST_CHARS
    if limit < minimum:
        raise ExecutionError(
            Code.BROKER_CAPABILITY_UNSUPPORTED,
            f"the venue accepts client ids of at most {limit} characters and a "
            f"derived id needs {minimum}. Truncating would make two orders share a "
            "name and attribute one order's fills to another",
        )
    digest = hashlib.sha256(f"{CLIENT_ID_NAMESPACE}:{cleaned}".encode()).hexdigest()
    return f"{CLIENT_ID_PREFIX}{digest[:DIGEST_CHARS]}"


def is_our_client_order_id(client_order_id: str | None) -> bool:
    """Whether an id looks like one this adapter minted.

    A necessary condition, not a proof of authorship — anyone could type the
    prefix into a provider console. It is enough for its purpose: an order whose
    id fails this was certainly not placed by this adapter, and reconciliation
    reports it as `UNEXPECTED_REMOTE_ORDER` instead of trying to match it to a
    local order.

    Args:
        client_order_id: The id the provider reported, if any.

    Returns:
        True if it matches this adapter's derivation shape.
    """
    if not client_order_id:
        return False
    return CLIENT_ID_PATTERN.match(client_order_id.strip()) is not None
