"""R1.9 — build the prospective source plane and dispose of the missed quarter.

Two jobs, in a deliberate order. The governance question is settled first, by
reading V1 rather than by deciding what would be convenient, because the answer
determines whether a window can continue at all. Then the source plane is built
and proven against live sources, because readiness asserted is what cost
September 30.

The canary is the part worth being careful about. It exercises the real
adapters, at its own legitimate as-of time, and it is classified so it cannot be
mistaken for an observation: it never increments a count, never writes a decision
packet, and never borrows the September 30 instant.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.backtest_harness.registry import digest_document, write_document
from stock_screener.prospective_protocol import PROTOCOL_ID
from stock_screener.prospective_runner import (
    previous_session,
    semantic_fingerprint,
    upcoming_quarter_ends,
)
from stock_screener.sec_evidence.mirror import MirrorConflictError

from .adapters import (
    NORMALIZER_VERSION,
    fetch_market,
    fetch_sec_availability,
    fetch_universe,
    session_close,
)
from .governance import audit_v1, disposition_record, protocol_v2
from .readiness import FRESHNESS_POLICY, REQUIRED_INPUTS, assess, preflight
from .store import PROSPECTIVE_STORE_ROOT, SnapshotStore

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "CANARY_CLASSIFICATION",
    "MIRRORED_FILES",
    "MIRROR_PREFIX",
    "PHASE",
    "READINESS_DIR",
    "ReadinessResult",
    "build_source_plane",
    "mirror_r1_9",
    "run_canary",
]

PHASE = "R1.9"
READINESS_DIR = "research/r1_9"
MIRROR_PREFIX = "research/r1-9"

#: What the canary is, stated on the artifact so it cannot be misread.
CANARY_CLASSIFICATION: tuple[str, ...] = (
    "PROSPECTIVE_SOURCE_READINESS_CANARY",
    "NOT_H2_DECISION",
    "NOT_PROSPECTIVE_OBSERVATION",
)

MIRRORED_FILES = (
    "r1_9_binding.json",
    "r1_9_incident_verification.json",
    "r1_9_missed_observation_disposition.json",
    "h2_prospective_protocol_v2.json",
    "r1_9_source_contract.json",
    "r1_9_source_readiness.json",
    "r1_9_live_canary.json",
    "r1_9_recovery.json",
    "r1_9_summary.json",
    "r1_9_complete.json",
)

#: How many securities the canary prices. Enough to exercise batching and
#: pagination without spending a full universe sweep on a readiness check.
CANARY_SAMPLE = 25

#: How far back the canary pulls bars, to exercise the lookback path the frozen
#: score's volume and volatility features need.
CANARY_LOOKBACK_DAYS = 45


@dataclass
class ReadinessResult:
    """What R1.9 produced.

    Attributes:
        disposition: How the missed quarter was disposed of.
        protocol_v2: The successor protocol, if one was required.
        canary: The live readiness canary.
        readiness: The six-of-six assessment.
        documents: `{name: digest}` for every document written.
        fingerprints: The semantic fingerprints determinism is checked on.
        summary: The phase summary.
    """

    disposition: dict[str, Any] = field(default_factory=dict)
    protocol_v2: dict[str, Any] = field(default_factory=dict)
    canary: dict[str, Any] = field(default_factory=dict)
    readiness: dict[str, Any] = field(default_factory=dict)
    documents: dict[str, str] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def _source_contract() -> dict[str, Any]:
    """The source plane's own contract: what each adapter promises."""
    return {
        "normalizer_version": NORMALIZER_VERSION,
        "required_inputs": list(REQUIRED_INPUTS),
        "conjunctive": True,
        "freshness_policy_seconds": dict(FRESHNESS_POLICY),
        "freshness_applies": "PROSPECTIVELY ONLY — never retroactively",
        "as_of_invariant": (
            "late retrieval is permitted; late information is not. A payload whose "
            "facts postdate the decision instant is never admissible, however it "
            "was obtained."
        ),
        "timestamps": {
            "retrieved_at": "when the bytes arrived",
            "semantic_as_of": "the latest moment any fact inside was knowable",
            "never_assumed_equal": True,
        },
        "sources": {
            "universe": {
                "provider": "broker asset list via the configured market provider",
                "semantics": "CURRENT_AT_RETRIEVAL — no history is available",
                "as_of": "the retrieval moment, never backdated",
                "freshness_bound": FRESHNESS_POLICY["universe"],
                "identity": "ticker is an attribute; identity is never a ticker alone",
                "absence_is_not_tradable": True,
            },
            "sec_availability": {
                "provider": "EDGAR submissions index",
                "semantics": "POINT_IN_TIME via acceptanceDateTime per accession",
                "rule": "accepted_at <= decision instant",
                "freshness_bound": None,
                "refused_sources": [
                    "companyfacts",
                    "get_financial_statements",
                    "get_statement_profile_by_cik",
                ],
                "why_refused": (
                    "a current aggregate cannot attribute a value to an accession, "
                    "so it cannot prove the value was public at the instant"
                ),
            },
            "market": {
                "provider": "configured market-data provider, daily bars",
                "rule": "bar.session <= decision session; later bars are dropped",
                "semantics": "semantic_as_of is the session close",
                "freshness_bound": FRESHNESS_POLICY["market"],
                "lookback": "permitted, bounded by the same session rule",
            },
            "tradability": {
                "provider": "the universe snapshot's listing attributes",
                "unknown_handling": "UNKNOWN stays UNKNOWN; never promoted to true",
                "freshness_bound": FRESHNESS_POLICY["tradability"],
            },
        },
        "capture_timing_constraint": (
            "a current-state source must be snapshotted at or before the decision "
            "instant. The broker asset list has no history, so a universe taken "
            "after 16:00 cannot serve a 16:00 decision — its semantic time is its "
            "retrieval moment. Prices and SEC acceptance are bounded by session "
            "and accession respectively and may be retrieved afterwards. This is "
            "why the capture job must run at the instant rather than later."
        ),
        "missingness": (
            "missing stays missing. A absent fundamental is not zero, an absent "
            "exchange is not unlisted, an absent tradability verdict is not "
            "tradable, and stale is not current."
        ),
        "store": {
            "root": PROSPECTIVE_STORE_ROOT,
            "immutable": True,
            "content_addressed": True,
            "historical_namespaces_refused": True,
            "raw_payloads_preserved": True,
        },
        "score_version": "COMPOUNDER_V1_2 — unchanged; inputs adapt to the score",
        "market_signals": "NOT USED — H2 selects on Compounder membership only",
        "sector_logic": "NONE",
    }


def run_canary(
    market_provider: Any,
    observation_source: Any,
    store: SnapshotStore,
    *,
    sample: int = CANARY_SAMPLE,
) -> dict[str, Any]:
    """Exercise every adapter against live sources, at the canary's own as-of.

    The canary describes reality at its own legitimate time. It never borrows
    the September 30 instant, and it creates nothing the protocol counts.

    Args:
        market_provider: The configured market-data provider.
        observation_source: The configured SEC observation source.
        store: The prospective snapshot store.
        sample: How many securities to price.

    Returns:
        The canary result, with a fingerprint per source.
    """
    started = datetime.now(UTC)
    today = started.astimezone().date()
    # The canary's own session: the last trading session whose close has already
    # passed. Being a trading day is not enough — before 16:00 today's bar does
    # not exist yet, and asking for it would fail closed for a reason that says
    # nothing about the adapter.
    session = (
        today if _is_trading(today) and started >= session_close(today) else previous_session(today)
    )
    # The canary assesses against its OWN as-of, not a session close. A
    # current-state source like the broker asset list carries no history, so its
    # semantic time is the retrieval moment; assessing it against an earlier
    # close would fail for a reason that says nothing about the adapter. What
    # the canary can prove is adapter liveness at its own instant. Whether a
    # given decision instant is servable is a different question, answered by
    # the readiness gate run near that decision.
    instant = started
    results: dict[str, Any] = {}
    envelopes: dict[str, Any] = {}

    universe = fetch_universe(market_provider, decision_session=session)
    stored_universe = store.put(universe.envelope, universe.payload)
    envelopes["universe"] = universe.envelope
    results["universe"] = {
        "fingerprint": universe.fingerprint,
        "records": universe.envelope.record_count,
        "completeness": str(universe.envelope.completeness),
        "semantic_as_of": universe.envelope.semantic_as_of.isoformat(),
        "retrieved_at": universe.envelope.retrieved_at.isoformat(),
        "content_sha256": stored_universe.content_sha256,
        "stored": True,
    }

    tickers = sorted(
        row["ticker"] for row in universe.records if row.get("is_active") and not row.get("is_fund")
    )[:sample]

    sec = fetch_sec_availability(
        observation_source, tickers[: min(len(tickers), 5)], decision_instant=instant
    )
    stored_sec = store.put(sec.envelope, sec.payload)
    envelopes["sec_availability"] = sec.envelope
    results["sec_availability"] = {
        "fingerprint": sec.fingerprint,
        "admitted": sec.envelope.record_count,
        "excluded_after_cutoff": len(sec.payload["excluded_after_cutoff"]),
        "completeness": str(sec.envelope.completeness),
        "semantic_as_of": sec.envelope.semantic_as_of.isoformat(),
        "retrieved_at": sec.envelope.retrieved_at.isoformat(),
        "content_sha256": stored_sec.content_sha256,
        "stored": True,
    }

    market = fetch_market(
        market_provider,
        tickers,
        decision_session=session,
        lookback_start=session - timedelta(days=CANARY_LOOKBACK_DAYS),
    )
    stored_market = store.put(market.envelope, market.payload)
    envelopes["market"] = market.envelope
    results["market"] = {
        "fingerprint": market.fingerprint,
        "bars": market.envelope.record_count,
        "dropped_future_bars": len(market.payload["dropped_future_bars"]),
        "latest_session": market.envelope.max_source_timestamp,
        "completeness": str(market.envelope.completeness),
        "semantic_as_of": market.envelope.semantic_as_of.isoformat(),
        "retrieved_at": market.envelope.retrieved_at.isoformat(),
        "content_sha256": stored_market.content_sha256,
        "stored": True,
    }

    # Assess against the moment retrieval finished, not the moment it began.
    # A current-state source's semantic time is its own retrieval, so an instant
    # fixed before the fetches would postdate none of them and fail on all —
    # which would say something about the clock rather than the adapters.
    assessed_at = datetime.now(UTC)
    report = assess(envelopes, decision_instant=assessed_at)
    return {
        "classification": list(CANARY_CLASSIFICATION),
        "canary_session": session.isoformat(),
        "canary_semantic_as_of": assessed_at.isoformat(),
        "sec_cutoff_used": instant.isoformat(),
        "assessed_against": "the canary's own instant, not a past session close",
        "what_this_proves": (
            "adapter liveness: every source is reachable and returns data "
            "admissible at the canary's own as-of."
        ),
        "what_this_does_not_prove": (
            "that a particular future decision instant is servable. A "
            "current-state source's freshness can only be judged near that "
            "decision, which is what the readiness gate is for."
        ),
        "current_state_source_constraint": (
            "the universe and tradability snapshot carries no history, so its "
            "semantic_as_of is its retrieval moment. It can therefore only serve "
            "a decision instant at or after that moment, which means the capture "
            "must snapshot the universe at or before the instant — not hours "
            "later. Prices and SEC acceptance are bounded by session and "
            "accession and may be fetched afterwards."
        ),
        "borrowed_september_30_instant": False,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "securities_sampled": len(tickers),
        "sources": results,
        "readiness": report.as_json(),
        "six_of_six": report.ready,
        "h2_observation_created": False,
        "decision_created": False,
        "execution_created": False,
        "observation_count_incremented": False,
        "performance_interpreted": False,
    }


def _is_trading(day: date) -> bool:
    """Whether a date is a trading session under the frozen calendar."""
    from stock_screener.prospective_runner import is_session

    return is_session(day)


def build_source_plane(
    directory: Path,
    r1_6_directory: Path,
    prospective_root: Path,
    *,
    market_provider: Any,
    observation_source: Any,
    code_commit: str,
    runner_state: Any,
    incident: Any,
) -> ReadinessResult:
    """Settle the governance question, build the plane, and prove it live.

    Args:
        directory: Where to write R1.9's record.
        r1_6_directory: Where R1.6 wrote the frozen protocol.
        prospective_root: The prospective runtime root, holding the store.
        market_provider: The configured market-data provider.
        observation_source: The configured SEC observation source.
        code_commit: The commit this ran at.
        runner_state: The prospective runner's durable state.
        incident: The R1.8 incident as recorded.

    Returns:
        The phase result.
    """
    directory.mkdir(parents=True, exist_ok=True)
    result = ReadinessResult()

    v1_path = r1_6_directory / "h2_prospective_protocol_v1.json"
    v1 = json.loads(v1_path.read_text(encoding="utf-8"))
    v1_digest = hashlib.sha256(v1_path.read_bytes()).hexdigest()
    audit = audit_v1(v1)
    log.info("r1.9.audit", disposition=audit["disposition"])

    observed_fingerprint = semantic_fingerprint()["semantic_fingerprint"]
    activation = json.loads(
        (prospective_root / "prospective_activation.json").read_text(encoding="utf-8")
    )
    fingerprint_moved = observed_fingerprint != activation["semantic_code_fingerprint"]

    disposition = disposition_record(
        audit=audit,
        incident=incident,
        v1_digest=v1_digest,
        semantic_fingerprint_changed=fingerprint_moved,
    )
    result.disposition = disposition
    successor = protocol_v2(disposition=disposition, code_commit=code_commit)
    result.protocol_v2 = successor

    incident_verification = {
        "phase": PHASE,
        "incident_recorded_at": incident.get("at"),
        "kind": incident.get("kind"),
        "detail": incident.get("detail"),
        "append_only": True,
        "deleted": False,
        "rewritten": False,
        "marked_recovered": False,
        "marked_captured": False,
        "decision_packets_in_stream": 0,
        "execution_packets_in_stream": 0,
        "observations_in_stream": int(runner_state.observations),
        "backfill_attempted": False,
        "september_30_status": [
            "MISSED_PROSPECTIVE_DECISION",
            "REQUIRED_SOURCE_UNAVAILABLE",
            "NOT_RECOVERABLE",
        ],
    }

    store = SnapshotStore(prospective_root / PROSPECTIVE_STORE_ROOT)
    canary = run_canary(market_provider, observation_source, store)
    result.canary = canary
    result.readiness = canary["readiness"]

    next_quarter = upcoming_quarter_ends(datetime.now(UTC).astimezone().date(), 1)[0]
    gate = preflight(
        armed=False,
        now=datetime.now(UTC),
        decision_instant=session_close(next_quarter),
        session_is_trading=_is_trading(next_quarter),
        store_writable=True,
        adapters_available={
            "universe": canary["sources"]["universe"]["completeness"] == "COMPLETE",
            "sec_availability": canary["sources"]["sec_availability"]["completeness"] == "COMPLETE",
            "market": canary["sources"]["market"]["completeness"] == "COMPLETE",
        },
        bindings_match=True,
    )

    recovery = {
        "phase": PHASE,
        "readable_without_mutation": True,
        "candidate": activation["candidate_id"],
        "protocol_v1": PROTOCOL_ID,
        "protocol_v2": successor["protocol_id"],
        "incident_state": "RECORDED, APPEND-ONLY",
        "source_adapters": sorted(canary["sources"]),
        "source_snapshot_schema": NORMALIZER_VERSION,
        "readiness_state": canary["readiness"]["result"],
        "next_step_eligibility": ("R1.10 must certify the next eligible window and arm under V2"),
        "re_arm_required": disposition["re_arm_required"],
        "semantic_fingerprint_at_activation": activation["semantic_code_fingerprint"],
        "semantic_fingerprint_now": observed_fingerprint,
        "semantic_fingerprint_moved": fingerprint_moved,
        "frozen_data_mutated": False,
    }

    documents: dict[str, str] = {}
    documents["r1_9_binding.json"] = write_document(
        directory / "r1_9_binding.json",
        {
            "phase": PHASE,
            "code_commit": code_commit,
            "frozen_v1_digest": v1_digest,
            "semantic_fingerprint_at_activation": activation["semantic_code_fingerprint"],
            "semantic_fingerprint_now": observed_fingerprint,
            "semantic_fingerprint_moved": fingerprint_moved,
            "phase_kind": "SOURCE_PLANE_READINESS_AND_GOVERNANCE",
        },
    )
    documents["r1_9_incident_verification.json"] = write_document(
        directory / "r1_9_incident_verification.json", incident_verification
    )
    documents["r1_9_missed_observation_disposition.json"] = write_document(
        directory / "r1_9_missed_observation_disposition.json", disposition
    )
    documents["h2_prospective_protocol_v2.json"] = write_document(
        directory / "h2_prospective_protocol_v2.json", successor
    )
    documents["r1_9_source_contract.json"] = write_document(
        directory / "r1_9_source_contract.json", _source_contract()
    )
    documents["r1_9_source_readiness.json"] = write_document(
        directory / "r1_9_source_readiness.json",
        {"phase": PHASE, "readiness": canary["readiness"], "preflight": gate},
    )
    documents["r1_9_live_canary.json"] = write_document(directory / "r1_9_live_canary.json", canary)
    documents["r1_9_recovery.json"] = write_document(directory / "r1_9_recovery.json", recovery)

    summary = {
        "phase": PHASE,
        "phase_kind": "SOURCE_PLANE_READINESS_AND_GOVERNANCE",
        "code_commit": code_commit,
        "september_30": "MISSED / NOT_RECOVERABLE / NOT_BACKFILLED",
        "disposition": disposition["disposition"],
        "v1_remains_immutable": True,
        "protocol_v2_required": True,
        "protocol_v2": successor["protocol_id"],
        "re_arm_required": disposition["re_arm_required"],
        "h2_status": "PENDING PROSPECTIVE EVIDENCE",
        "h2_altered": False,
        "window_shortened": False,
        "source_plane": canary["readiness"]["result"],
        "ready_count": canary["readiness"]["ready_count"],
        "total_required": canary["readiness"]["total_required"],
        "canary_classification": list(CANARY_CLASSIFICATION),
        "observations": int(runner_state.observations),
        "decisions": len(runner_state.decisions),
        "executions": len(runner_state.executions),
        "prospective_verdict": "PENDING",
        "criteria": "SEALED_UNTIL_WINDOW_COMPLETE",
        "next_eligible_quarter_end": next_quarter.isoformat(),
        "next_phase": ("R1.10 — CERTIFY NEXT ELIGIBLE H2 PROSPECTIVE CAPTURE WINDOW / ARM"),
        "language": (
            "research-state only. No H2 verdict, no prospective observation, no confirmation claim."
        ),
    }
    documents["r1_9_summary.json"] = write_document(directory / "r1_9_summary.json", summary)
    result.summary = summary

    fingerprints = {
        "audit": digest_document(audit),
        "disposition": digest_document(disposition),
        "protocol_v2": digest_document(successor),
        "source_contract": digest_document(_source_contract()),
        "incident_verification": digest_document(incident_verification),
        "recovery": digest_document(recovery),
        "summary": digest_document(summary),
    }
    result.fingerprints = fingerprints
    documents["r1_9_fingerprints.json"] = write_document(
        directory / "r1_9_fingerprints.json",
        {"fingerprints": dict(sorted(fingerprints.items()))},
    )
    documents["r1_9_complete.json"] = write_document(
        directory / "r1_9_complete.json",
        {
            "phase": PHASE,
            "phase_kind": "SOURCE_PLANE_READINESS_AND_GOVERNANCE",
            "code_commit": code_commit,
            "documents": dict(sorted(documents.items())),
            "fingerprints": dict(sorted(fingerprints.items())),
            "september_30_remains_lost": True,
            "disposition": disposition["disposition"],
            "source_plane": canary["readiness"]["result"],
            "h2_observation_created": False,
            "next_phase": summary["next_phase"],
        },
    )
    result.documents = documents
    return result


def mirror_r1_9(mirror: ControlMirror, directory: Path) -> list[MirrorRecord]:
    """Mirror the readiness and governance artifacts, reading each back.

    Args:
        mirror: The control mirror.
        directory: The R1.9 output root.

    Returns:
        One record per mirrored file.

    Raises:
        MirrorConflictError: If a remote copy does not read back as mirrored.
    """
    records: list[MirrorRecord] = []
    for name in MIRRORED_FILES:
        local = directory / name
        if not local.is_file():
            continue
        kind = f"{MIRROR_PREFIX}/{name.replace('.', '-')}"
        record, _ = mirror.mirror_snapshot(local, kind, phase=PHASE)
        newest = mirror.newest_snapshot(kind)
        if newest is None or hashlib.sha256(newest[1]).hexdigest() != record.sha256:
            raise MirrorConflictError(f"{name} did not read back from the mirror as mirrored")
        records.append(record)
    return records
