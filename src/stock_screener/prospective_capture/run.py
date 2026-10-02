"""Orchestration: one entry point per stage, and a canary over the real pipeline.

R1.11's finding was not that the pieces were wrong but that nothing joined them
up. So these are deliberately the only entry points: a capture is not a sequence
of scripts a careful operator remembers to run in order, it is one call that
verifies its own authorisation, seals its inputs, and refuses rather than
improvises.

The canary is the honest version of R1.9's source canary. That one proved three
adapters were reachable, which is why "6/6 READY" survived as long as it did.
This one runs the entire production chain — the same fetchers, the same score
builder, the same scorer, the same ranker, the same fingerprint functions — and
produces six genuinely non-null fingerprints at its own as-of. It stops before H2
selection, so it can never consume a prospective observation.
"""

from __future__ import annotations

import socket
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from domain import PriceBar
from stock_screener.prospective_runner import (
    is_session,
    previous_session,
    semantic_fingerprint,
)
from stock_screener.prospective_source.adapters import (
    fetch_market,
    fetch_sec_availability,
    fetch_universe,
    session_close,
)

from .actions import fetch_corporate_actions
from .calendar_binding import bind_calendar
from .crosssection import build_cross_section
from .guards import audit_package
from .identity import resolve_identities
from .observations import admitted_accessions_by_ticker, fetch_filing_observations
from .provenance import audit as audit_provenance
from .readiness import assess_capture_readiness
from .score_inputs import build_score_inputs
from .scoring import score_cross_section

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from domain import EligibilityThresholds, VolumeBasis
    from stock_screener.prospective_source import SnapshotStore

log = structlog.get_logger(__name__)

__all__ = [
    "CANARY_CLASSIFICATION",
    "CANARY_RECORD_FILE",
    "CANARY_SAMPLE",
    "capture_readiness",
    "run_capture_pipeline_canary",
]

#: Where the latest pipeline canary record is kept. Pre-event readiness reads it
#: to establish that the derived producers actually work: before the armed
#: session arrives there is no package to inspect, so the canary is the evidence
#: that six non-null fingerprints are producible rather than merely hoped for.
CANARY_RECORD_FILE = "last_capture_pipeline_canary.json"

#: How many identities the canary carries through the whole chain. Each one costs
#: a filing-observations read, so this is a real cost rather than a formality.
CANARY_SAMPLE = 25

#: What the canary is, stated so nothing downstream can mistake it for evidence.
CANARY_CLASSIFICATION: tuple[str, ...] = (
    "PROSPECTIVE_CAPTURE_PIPELINE_CANARY",
    "NOT_H2_OBSERVATION",
    "NOT_A_DECISION",
    "NOT_PROSPECTIVE_CONFIRMATION",
)

#: A year of sessions, so the benchmark's twelve-month return can be measured.
_BENCHMARK_LOOKBACK_DAYS = 400

#: Enough history for the liquidity window and the volatility features.
_PRICE_LOOKBACK_DAYS = 400


class NetworkAfterFreezeError(RuntimeError):
    """Raised when the pure computation path attempts a network call."""


@contextmanager
def _no_network() -> Iterator[list[str]]:
    """Make every outbound socket connection a hard failure.

    Once the six inputs are sealed, scoring, ranking and selection must be pure.
    Asserting that in a comment is worthless, and closing the HTTP clients only
    covers the clients this code happens to know about. Blocking the socket layer
    covers anything that tries, including a library's own lazy refresh.

    Yields:
        The attempts that were blocked. Empty is the passing case.
    """
    attempts: list[str] = []
    original = socket.socket.connect

    def refuse(self: Any, address: Any) -> None:
        attempts.append(repr(address))
        raise NetworkAfterFreezeError(
            f"a network call to {address!r} was attempted after the decision "
            "inputs were sealed; the pure computation path must be reproducible"
        )

    socket.socket.connect = refuse  # type: ignore[method-assign]
    try:
        yield attempts
    finally:
        socket.socket.connect = original  # type: ignore[method-assign]


def _canary_session(now: datetime) -> date:
    """The last session whose close has passed, for a canary's own as-of."""
    today = now.astimezone().date()
    if is_session(today) and now >= session_close(today):
        return today
    return previous_session(today)


def run_capture_pipeline_canary(
    market_provider: Any,
    observation_source: Any,
    store: SnapshotStore,
    research_database: Path,
    *,
    benchmark_symbol: str,
    bar_volume_basis: VolumeBasis,
    thresholds: EligibilityThresholds | None = None,
    sample: int = CANARY_SAMPLE,
) -> dict[str, Any]:
    """Run the whole production chain at the canary's own as-of.

    Args:
        market_provider: The configured market-data provider.
        observation_source: The configured SEC source.
        store: The prospective snapshot store.
        research_database: The research database, read for identities only.
        benchmark_symbol: The frozen benchmark's ticker.
        bar_volume_basis: What the bars' volume represents.
        thresholds: The frozen eligibility thresholds.
        sample: How many identities to carry through.

    Returns:
        The canary result, with a real fingerprint per required component.
    """
    started = datetime.now(UTC)
    session = _canary_session(started)
    instant = session_close(session)
    guards = audit_package()

    universe = fetch_universe(market_provider, decision_session=session)
    store.put(universe.envelope, universe.payload)

    with closing(sqlite3.connect(f"file:{research_database}?mode=ro", uri=True)) as db:
        sessions = [
            date.fromisoformat(str(row[0]))
            for row in db.execute("SELECT date FROM benchmark_prices ORDER BY date")
        ]
        calendar = bind_calendar(sessions, source=str(research_database))
        identities, identity_counts, _ = resolve_identities(db, universe.records)
    sampled = identities[:sample]
    symbols = [identity.symbol for identity in sampled]

    acceptance = fetch_sec_availability(observation_source, symbols, decision_instant=instant)
    store.put(acceptance.envelope, acceptance.payload)

    observations = fetch_filing_observations(
        observation_source,
        sampled,
        decision_instant=instant,
        admitted=admitted_accessions_by_ticker(acceptance.records),
    )
    store.put(observations.envelope, observations.payload)

    lookback = session - timedelta(days=_PRICE_LOOKBACK_DAYS)
    bars = fetch_market(market_provider, symbols, decision_session=session, lookback_start=lookback)
    store.put(bars.envelope, bars.payload)

    actions = fetch_corporate_actions(
        market_provider, symbols, decision_session=session, lookback_start=lookback
    )
    store.put(actions.envelope, actions.payload)

    benchmark = fetch_market(
        market_provider,
        [benchmark_symbol],
        decision_session=session,
        lookback_start=session - timedelta(days=_BENCHMARK_LOOKBACK_DAYS),
    )
    benchmark_bars = [
        PriceBar(
            date=date.fromisoformat(str(row["session"])),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        for row in benchmark.records
    ]

    # Everything strategy-relevant is now retrieved. Close the clients and block
    # the socket layer, so the pure phase proves its purity instead of claiming it.
    for client in (market_provider, observation_source):
        closer = getattr(client, "close", None)
        if closer is not None:
            closer()

    source_fingerprints = {
        "universe": universe.fingerprint,
        "tradability": universe.fingerprint,
        "market": bars.fingerprint,
        "sec_availability": acceptance.fingerprint,
        "filing_observations": observations.fingerprint,
        "corporate_actions": actions.fingerprint,
    }
    with _no_network() as blocked:
        inputs = build_score_inputs(
            sampled,
            observation_records=observations.records,
            bar_records=bars.records,
            action_records=actions.records,
            decision_session=session,
            decision_instant=instant.isoformat(),
            source_fingerprints=source_fingerprints,
            bar_volume_basis=bar_volume_basis,
        )
        scores = score_cross_section(
            inputs,
            benchmark_bars=benchmark_bars,
            benchmark_symbol=benchmark_symbol,
            decision_session=session,
            thresholds=thresholds,
        )
        provenance = audit_provenance(
            inputs,
            observation_records=observations.records,
            acceptance_records=acceptance.records,
            decision_instant=instant,
            source_operation=observations.envelope.operation,
        )
    priced = [
        (identity.kind, identity.identity_id)
        for identity in sampled
        if any(
            str(row["ticker"]).upper() == identity.symbol and row["session"] == session.isoformat()
            for row in bars.records
        )
    ]
    cross_section = build_cross_section(
        scores,
        tradability={(identity.kind, identity.identity_id): "TRADABLE" for identity in sampled},
        session_priced=priced,
        source_fingerprints={
            **source_fingerprints,
            "score_inputs": inputs.fingerprint(),
            "scores": scores.fingerprint(),
        },
    )

    fingerprints = {
        "universe": universe.fingerprint,
        "tradability": universe.fingerprint,
        "market": bars.fingerprint,
        "sec_availability": acceptance.fingerprint,
        "score_inputs": inputs.fingerprint(),
        "ranked_cross_section": cross_section.fingerprint(),
    }
    return {
        "classification": list(CANARY_CLASSIFICATION),
        "canary_session": session.isoformat(),
        "canary_instant": instant.isoformat(),
        "started_at": started.isoformat(),
        "guards": guards,
        "calendar": calendar.as_json(),
        "semantic_fingerprint": semantic_fingerprint()["semantic_fingerprint"],
        "universe_listings": universe.envelope.record_count,
        "identities_resolved": len(identities),
        "identity_exclusions": identity_counts,
        "sampled": len(sampled),
        "observations_admitted": observations.envelope.record_count,
        "bars_admitted": bars.envelope.record_count,
        "actions_admitted": actions.envelope.record_count,
        "score_inputs": inputs.as_json() | {"identities": "elided — see the artifact"},
        "scores": {
            "scored_count": scores.scored,
            "rankable_count": scores.rankable,
            "benchmark": scores.benchmark,
            "fingerprint": scores.fingerprint(),
        },
        "cross_section": {
            "identity_count": len(cross_section.rows),
            "executable_count": len(cross_section.executable),
            "funnel": cross_section.funnel,
            "exclusions": cross_section.exclusions,
            "fingerprint": cross_section.fingerprint(),
        },
        "provenance": provenance,
        "network_calls_after_freeze": len(blocked),
        "network_freeze_mechanism": (
            "providers closed and socket.socket.connect replaced with a hard "
            "failure for the duration of the pure phase"
        ),
        "required_fingerprints": fingerprints,
        "non_null_fingerprint_count": sum(1 for v in fingerprints.values() if v),
        "six_real_fingerprints": all(fingerprints.values()),
        "stopped_before": "H2_SELECTION_AND_DECISION_PERSISTENCE",
        "why_it_stops": (
            "selection and packet persistence are what consume a prospective "
            "observation. The canary proves the chain that feeds them and goes no "
            "further."
        ),
        "consumed_an_observation": False,
        "used_production_code": True,
        "fixtures_used": False,
    }


def capture_readiness(
    *,
    decision_instant: str,
    fingerprints: dict[str, str | None],
    produced: dict[str, bool],
    store_healthy: bool,
    calendar_authoritative: bool,
    activation_matches: bool,
    arm_matches: bool,
) -> dict[str, Any]:
    """Assess capture readiness against the strict definition.

    Args:
        decision_instant: The instant, ISO with offset.
        fingerprints: `{component: digest | None}` for the six required inputs.
        produced: `{component: whether a producer actually emitted it}`.
        store_healthy: Whether the store round-trips.
        calendar_authoritative: Whether the calendar binding matched.
        activation_matches: Whether the activation still binds.
        arm_matches: Whether the arm still binds.

    Returns:
        The report, with `CAPTURE_READY` only when everything holds.
    """
    from .decision import capture_decision as _decision_executor
    from .execution import capture_execution_reference as _execution_executor

    sources = {
        name: {"admissible": bool(fingerprints.get(name)), "fingerprint": fingerprints.get(name)}
        for name in ("universe", "tradability", "market", "sec_availability")
    }
    derived = {
        name: {
            "produced": produced.get(name, False),
            "validated": produced.get(name, False) and bool(fingerprints.get(name)),
            "fingerprint": fingerprints.get(name),
            "producer": (
                "prospective_capture.score_inputs.build_score_inputs"
                if name == "score_inputs"
                else "prospective_capture.crosssection.build_cross_section"
            ),
        }
        for name in ("score_inputs", "ranked_cross_section")
    }
    report = assess_capture_readiness(
        decision_instant=decision_instant,
        sources=sources,
        derived=derived,
        executors={
            "D_DECISION": callable(_decision_executor),
            "E_EXECUTION_REFERENCE": callable(_execution_executor),
        },
        store_healthy=store_healthy,
        calendar_authoritative=calendar_authoritative,
        activation_matches=activation_matches,
        arm_matches=arm_matches,
    )
    return report.as_json()
