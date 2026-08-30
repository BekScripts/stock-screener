"""Point-in-time capture: recording what the system knew, on the day it knew it.

Everything else in this application stores a *current* view. `financial_snapshots`
overwrites a restated quarter with the restatement; `score_snapshots` replaces
the day's rows when the score command runs again; adjusted closes are rewritten
by every corporate action that follows them. Each is correct for answering "what
looks interesting today", and each destroys the one thing a trading system can
be honestly evaluated against — the figure that was actually on the wire when a
decision would have been made.

This package writes the parallel record. It adds nothing to the scoring pipeline
and changes none of its numbers; it copies what the pipeline produced into
tables that are never updated.

## The three times, and why they are three

A dataset that conflates these acquires look-ahead silently, so they are
separate columns everywhere:

`snapshot_date`
    The day being described. A UTC calendar date, matching
    `score_snapshots.score_date`, which the scoring engine sets from
    `datetime.now(UTC).date()`. It is a calendar day and not a trading session:
    the repository has no market calendar, and inventing one here would put this
    package's dates out of step with every other date in the database. A capture
    on a Sunday therefore describes Sunday, using the newest bars available,
    exactly as a score computed on a Sunday does.

`as_of`
    The bound applied when reading. Bars are loaded with `until=snapshot_date`
    so a feature can never be computed from a session that had not happened;
    fundamentals are selected on `filed_date <= as_of` so a figure cannot be
    read before it was filed. Where a caller does not say, `as_of` is
    `snapshot_date`.

`captured_at`
    When the process ran. An aware UTC timestamp. A capture for Friday started
    at 00:05 on Saturday still describes Friday, and the gap between the two is
    a fact worth being able to see — a capture that ran late read a database
    that had moved on.

A fourth, narrower time appears on the feature rows. `feature_as_of` is the date
of the newest bar actually used, which equals `snapshot_date` for a security
still trading and is older for one that has stopped. Recording it is what stops
a stale series being read as a current observation.

## What a capture guarantees

**Immutability.** No repository here has an update path. A second capture for a
day that already has one raises `ImmutableSnapshotError` rather than replacing
the rows, because replacing them would substitute hindsight for the record.

**Atomicity.** The run row reaches `COMPLETE` in the same transaction that
writes the snapshots. A failure rolls the whole thing back, so a day is either
wholly present or wholly absent — never partial and marked good.

**Idempotency.** Re-running for a day that already completed does nothing and
says so.

**No fabrication.** Nothing here reconstructs a past date. The first capture
records the first day, and the history starts there.
"""

from stock_screener.pit.acceptance import (
    AcceptanceIngestResult,
    ingest_filing_acceptance,
)
from stock_screener.pit.actions import (
    ActionIngestResult,
    CorporateActionSource,
    ingest_corporate_actions,
)
from stock_screener.pit.capture import (
    FEATURE_VERSION,
    CaptureResult,
    capture_day,
    record_capture_failure,
)
from stock_screener.pit.decision_time import (
    MARKET_CLOSE,
    MARKET_TIMEZONE,
    DecisionPoint,
    decision_instant,
)
from stock_screener.pit.delistings import (
    DelistingDiscoveryResult,
    DelistingSource,
    discover_delistings,
)
from stock_screener.pit.metrics import (
    PointInTimeMetrics,
    build_metrics_as_of,
    periods_as_of,
)
from stock_screener.pit.observations import ingest_filing_observations
from stock_screener.pit.returns import (
    ReturnMeasurementResult,
    measure_forward_returns,
)

__all__ = [
    "FEATURE_VERSION",
    "MARKET_CLOSE",
    "MARKET_TIMEZONE",
    "AcceptanceIngestResult",
    "ActionIngestResult",
    "CaptureResult",
    "CorporateActionSource",
    "DecisionPoint",
    "DelistingDiscoveryResult",
    "DelistingSource",
    "PointInTimeMetrics",
    "ReturnMeasurementResult",
    "build_metrics_as_of",
    "capture_day",
    "decision_instant",
    "discover_delistings",
    "ingest_corporate_actions",
    "ingest_filing_acceptance",
    "ingest_filing_observations",
    "measure_forward_returns",
    "periods_as_of",
    "record_capture_failure",
]
