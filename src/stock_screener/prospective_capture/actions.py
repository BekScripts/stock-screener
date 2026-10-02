"""Corporate actions for the prospective window, bounded by ex-date.

Stored and retrieved bars are adjusted, so the price a share count was stated
against is not the price in the series. Recovering it needs the split factor and
the ex-date it pivots on, and neither can be inferred from a price series without
guessing — which is why the historical reconstruction reads corporate actions and
why the prospective path must too.

The temporal rule is the session bound, not the retrieval moment: an action with
an ex-date on or before the decision session was knowable at the decision, and
one after it was not. A split effective the following week must not be applied to
a decision taken before it, so those are dropped and counted rather than kept.

Spinoffs are unsupported historically and remain unsupported here, exactly as the
frozen contract records.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.prospective_source.adapters import (
    NORMALIZER_VERSION,
    SourceResult,
    session_close,
)
from stock_screener.prospective_source.envelope import (
    Completeness,
    SnapshotEnvelope,
    TemporalClass,
    content_digest,
    request_fingerprint,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

log = structlog.get_logger(__name__)

__all__ = ["fetch_corporate_actions"]


def fetch_corporate_actions(
    provider: Any,
    symbols: Sequence[str],
    *,
    decision_session: date,
    lookback_start: date,
) -> SourceResult:
    """Snapshot corporate actions with an ex-date no later than the decision.

    Args:
        provider: A market-data provider exposing `get_corporate_actions`.
        symbols: The securities to look up.
        decision_session: The last admissible ex-date.
        lookback_start: The earliest ex-date to retrieve.

    Returns:
        The snapshot, one record per admitted action. An empty result is valid:
        most windows contain no action at all.
    """
    retrieved = datetime.now(UTC)
    found = provider.get_corporate_actions(list(symbols), lookback_start, decision_session)
    admitted: list[dict[str, Any]] = []
    future: list[dict[str, Any]] = []
    for action in found:
        row = {
            "symbol": action.symbol,
            "kind": str(action.kind),
            "ex_date": action.ex_date.isoformat(),
            "split_factor": action.split_factor,
            "cash_amount": action.cash_amount,
            "currency": action.currency,
            "source": action.source,
        }
        (admitted if action.ex_date <= decision_session else future).append(row)
    admitted.sort(key=lambda row: (row["symbol"], row["ex_date"], row["kind"]))
    dates = [row["ex_date"] for row in admitted]
    payload = {"actions": admitted, "dropped_future_actions": future}
    envelope = SnapshotEnvelope(
        source_type="corporate_actions",
        source_name=type(provider).__name__,
        source_version="corporate actions by ex-date",
        operation="get_corporate_actions",
        retrieved_at=retrieved,
        semantic_as_of=session_close(decision_session),
        content_sha256=content_digest(payload),
        request=request_fingerprint(
            type(provider).__name__,
            "get_corporate_actions",
            {
                "symbols": sorted(symbols),
                "start": lookback_start.isoformat(),
                "end": decision_session.isoformat(),
            },
        ),
        normalizer_version=NORMALIZER_VERSION,
        record_count=len(admitted),
        completeness=Completeness.COMPLETE,
        temporal_class=TemporalClass.SESSION_BOUNDED,
        decision_session=decision_session.isoformat(),
        min_source_timestamp=min(dates) if dates else None,
        max_source_timestamp=max(dates) if dates else None,
        ambiguity=[],
        notes=[
            f"{len(future)} actions had an ex-date after the decision session and were dropped",
            "admissibility is ex_date <= decision session; retrieval may be later",
            "spinoffs are unsupported historically and remain unsupported here",
        ],
    )
    log.info(
        "prospective.corporate_actions",
        admitted=len(admitted),
        dropped_future=len(future),
    )
    return SourceResult(envelope=envelope, payload=payload, records=admitted)
