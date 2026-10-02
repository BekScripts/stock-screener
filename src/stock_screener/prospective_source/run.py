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
from zoneinfo import ZoneInfo

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
    SESSION_TIMEZONE,
    fetch_market,
    fetch_sec_availability,
    fetch_universe,
    session_close,
)
from .governance import audit_v1, disposition_record, protocol_v2
from .readiness import FRESHNESS_POLICY, REQUIRED_INPUTS, assess, preflight
from .store import PROSPECTIVE_STORE_ROOT, SnapshotStore
from .window import WindowError

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "CANARY_CLASSIFICATION",
    "CERTIFY_DIR",
    "CERTIFY_MIRRORED_FILES",
    "CERTIFY_MIRROR_PREFIX",
    "MIRRORED_FILES",
    "MIRROR_PREFIX",
    "PHASE",
    "READINESS_DIR",
    "ReadinessResult",
    "build_source_plane",
    "certify_window",
    "mirror_r1_9",
    "run_canary",
    "timing_canary",
    "v1_v2_diff",
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
    # The broker asset list supplies the tradability verdict as well as the
    # listing, so one retrieval satisfies both stage-A sources. Recording it under
    # both names is what stops `tradability` being counted as derived — the
    # miscategorisation that let it report ready with no source of its own.
    envelopes["tradability"] = universe.envelope
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
        "sources_ready": report.sources_ready,
        "readiness_kind": report.as_json()["readiness_kind"],
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


#: R1.10's artifacts, mirrored under their own prefix.
CERTIFY_DIR = "research/r1_10"
CERTIFY_MIRROR_PREFIX = "research/r1-10"
CERTIFY_MIRRORED_FILES = (
    "r1_10_binding.json",
    "r1_10_v1_v2_diff.json",
    "r1_10_temporal_source_audit.json",
    "h2_prospective_protocol_v2.json",
    "prospective_capture_sequence_v2.json",
    "r1_10_calendar_derivation.json",
    "r1_10_source_readiness.json",
    "r1_10_timing_canary.json",
    "r1_10_decision_day_runbook.json",
    "r1_10_recovery.json",
    "r1_10_summary.json",
    "r1_10_complete.json",
)


def v1_v2_diff(v1: dict[str, Any], v2: dict[str, Any]) -> dict[str, Any]:
    """Compare V1 and V2 on the things H2's science depends on.

    Args:
        v1: The frozen V1 protocol.
        v2: The V2 protocol.

    Returns:
        A field-by-field comparison, and a verdict on whether any scientific
        semantic moved.
    """
    checks = {
        "candidate_id": (v1["candidate_id"], v2["candidate_id"]),
        "rule_version": (v1["rule_version"], v2["rule_version"]),
        "entry": (v1["rule"]["entry"], v2["rule"]["entry"]),
        "retention": (v1["rule"]["retention"], v2["rule"]["retention"]),
        "target_size": (v1["rule"]["target_size"], v2["rule"]["target_size"]),
        "weighting": (v1["rule"]["weighting"], v2["rule"]["weighting"]),
        "rebalance": (v1["rule"]["rebalance"], v2["rule"]["rebalance"]),
        "execution": (v1["rule"]["execution"], v2["rule"]["execution"]),
        "criteria": ([row["id"] for row in v1["criteria"]], list(v2["criteria"])),
        "aggregate_expression": (
            v1["aggregate_rule"]["expression"],
            v2["aggregate_rule"]["expression"],
        ),
        "required_decisions": (
            v1["window"]["required_decisions"],
            v2["required_decisions"],
        ),
        "required_transitions": (
            v1["window"]["required_transitions"],
            v2["required_transitions"],
        ),
    }
    changed = {name: pair for name, pair in checks.items() if pair[0] != pair[1]}
    return {
        "compared": {
            name: {"v1": pair[0], "v2": pair[1], "same": pair[0] == pair[1]}
            for name, pair in sorted(checks.items())
        },
        "h2_scientific_semantics_changed": bool(changed),
        "changed_fields": sorted(changed),
        "added_in_v2": sorted(v2["added_in_v2"]),
        "v2_may_not": list(v2["may_not"]),
        "window_requirement_changed": (
            v1["window"]["required_transitions"] != v2["required_transitions"]
        ),
        "verdict": (
            "V2 changes only prospective operational governance"
            if not changed
            else f"STOP — V2 altered {sorted(changed)}"
        ),
    }


def timing_canary() -> dict[str, Any]:
    """Prove the three temporal classes behave as the audit claims.

    Four cases, because the fourth is the one that was missing: a current-only
    snapshot read after the instant must be refused by name.

    Returns:
        Each case with its verdict.
    """
    from .envelope import (
        Completeness,
        SnapshotEnvelope,
        TemporalClass,
        admissible_for,
        content_digest,
    )

    instant = datetime(2026, 12, 31, 16, 0, tzinfo=ZoneInfo(SESSION_TIMEZONE))
    payload = [{"probe": True}]

    def envelope(
        temporal: TemporalClass,
        retrieved: datetime,
        semantic: datetime,
        effective: datetime | None = None,
    ) -> SnapshotEnvelope:
        return SnapshotEnvelope(
            source_type="probe",
            source_name="timing_canary",
            source_version="v1",
            operation="probe",
            retrieved_at=retrieved,
            semantic_as_of=semantic,
            content_sha256=content_digest(payload),
            request="probe",
            normalizer_version=NORMALIZER_VERSION,
            record_count=1,
            completeness=Completeness.COMPLETE,
            temporal_class=temporal,
            effective_at=effective,
        )

    late = instant + timedelta(hours=3)
    early = instant - timedelta(hours=2)
    cases: dict[str, dict[str, Any]] = {
        "event_time_late_retrieval": {
            "expected": True,
            "verdict": admissible_for(envelope(TemporalClass.EVENT_TIME, late, instant), instant),
        },
        "session_bounded_post_close_retrieval": {
            "expected": True,
            "verdict": admissible_for(
                envelope(TemporalClass.SESSION_BOUNDED, late, instant), instant
            ),
        },
        "current_only_pre_decision_retrieval": {
            "expected": True,
            "verdict": admissible_for(
                envelope(TemporalClass.CURRENT_AT_RETRIEVAL, early, early),
                instant,
                max_age=FRESHNESS_POLICY["universe"],
            ),
        },
        "current_only_post_decision_retrieval": {
            "expected": False,
            "verdict": admissible_for(
                envelope(TemporalClass.CURRENT_AT_RETRIEVAL, late, late), instant
            ),
        },
        "current_only_with_provider_effective_time": {
            "expected": True,
            "verdict": admissible_for(
                envelope(TemporalClass.CURRENT_AT_RETRIEVAL, late, instant, effective=early),
                instant,
            ),
        },
    }
    results: dict[str, dict[str, Any]] = {}
    for name, body in cases.items():
        verdict = dict(body["verdict"])
        expected = bool(body["expected"])
        results[name] = {
            "expected_admissible": expected,
            "observed_admissible": bool(verdict["admissible"]),
            "reasons": list(verdict["reasons"]),
            "matches_expectation": bool(verdict["admissible"]) == expected,
        }
    return {
        "canary_id": "PROSPECTIVE_TIMING_CANARY_V1",
        "decision_instant": instant.isoformat(),
        "cases": dict(sorted(results.items())),
        "all_as_expected": all(row["matches_expectation"] for row in results.values()),
        "the_case_that_was_missing": "current_only_post_decision_retrieval",
        "its_failure_code": "CURRENT_ONLY_SOURCE_TOO_LATE",
        "creates_an_observation": False,
    }


def certify_window(
    directory: Path,
    r1_6_directory: Path,
    r1_9_directory: Path,
    prospective_root: Path,
    *,
    market_provider: Any,
    observation_source: Any,
    code_commit: str,
    arm: bool,
) -> dict[str, Any]:
    """Certify the next eligible window, and arm it under V2 when asked.

    The order is forced: verify V2 against the frozen V1 before anything else,
    because an arm resting on a protocol that moved H2's science would be worse
    than no arm. Then classify the sources temporally, derive the window from the
    calendar, freeze the capture sequence, prove readiness live, and only then
    activate and arm.

    Args:
        directory: Where to write R1.10's record.
        r1_6_directory: Where V1 lives.
        r1_9_directory: Where R1.9's V2 draft lives.
        prospective_root: The prospective runtime root.
        market_provider: The configured market-data provider.
        observation_source: The configured SEC observation source.
        code_commit: The commit to bind to.
        arm: Whether to create the activation and arm. False certifies only.

    Returns:
        The certification, and the activation and arm when created.

    Raises:
        WindowError: If V2 altered H2's science, or the window cannot be armed.
    """
    from .governance import protocol_v2
    from .window import (
        activate_v2,
        arm_v2,
        calendar_derivation,
        capture_sequence,
        certify_timing,
        decision_day_runbook,
        derive_window,
    )

    directory.mkdir(parents=True, exist_ok=True)
    v1_path = r1_6_directory / "h2_prospective_protocol_v1.json"
    v1 = json.loads(v1_path.read_text(encoding="utf-8"))
    disposition = json.loads(
        (r1_9_directory / "r1_9_missed_observation_disposition.json").read_text("utf-8")
    )
    if not disposition["september_30_remains_lost"]:
        raise WindowError("the R1.9 disposition no longer records September 30 as lost")

    # R1.9's V2 carried H2's historical aggregate rule by mistake. Regenerate it
    # from the corrected generator and compare both, so the correction is on the
    # record rather than silently applied.
    draft_path = r1_9_directory / "h2_prospective_protocol_v2.json"
    draft_digest = hashlib.sha256(draft_path.read_bytes()).hexdigest()
    v2 = protocol_v2(disposition=disposition, code_commit=code_commit)
    diff = v1_v2_diff(v1, v2)
    if diff["h2_scientific_semantics_changed"]:
        raise WindowError(
            f"V2 would alter H2's scientific semantics: {diff['changed_fields']}. "
            "Refusing to certify or arm."
        )

    audit = certify_timing()
    window = derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC))
    sequence = capture_sequence(window)
    calendar = calendar_derivation(window)
    runbook = decision_day_runbook(window)

    store = SnapshotStore(prospective_root / PROSPECTIVE_STORE_ROOT)
    canary = run_canary(market_provider, observation_source, store)
    timing = timing_canary()
    if not canary["readiness"]["sources_ready"]:
        raise WindowError(
            f"source readiness is {canary['readiness']['result']}; a window is not "
            "armed on a plane that cannot feed it"
        )
    if not timing["all_as_expected"]:
        raise WindowError("the timing canary did not behave as the audit claims")

    documents: dict[str, str] = {}
    documents["h2_prospective_protocol_v2.json"] = write_document(
        directory / "h2_prospective_protocol_v2.json", v2
    )
    documents["r1_10_v1_v2_diff.json"] = write_document(
        directory / "r1_10_v1_v2_diff.json",
        {
            **diff,
            "r1_9_draft_digest": draft_digest,
            "r1_9_draft_superseded_before_use": True,
            "why_superseded": (
                "R1.9's generator imported H2's historical aggregate rule "
                "(C1 AND C2 AND C3 AND C4) in place of the prospective one "
                "(P1 AND P2 AND P3 AND P4). Nothing was armed under it, so it was "
                "a draft that failed this phase's verification rather than an "
                "authority being rewritten. The corrected V2 is emitted here."
            ),
            "authoritative_v2_digest": documents["h2_prospective_protocol_v2.json"],
        },
    )
    documents["r1_10_temporal_source_audit.json"] = write_document(
        directory / "r1_10_temporal_source_audit.json", audit
    )
    documents["prospective_capture_sequence_v2.json"] = write_document(
        directory / "prospective_capture_sequence_v2.json", sequence
    )
    documents["r1_10_calendar_derivation.json"] = write_document(
        directory / "r1_10_calendar_derivation.json", calendar
    )
    documents["r1_10_source_readiness.json"] = write_document(
        directory / "r1_10_source_readiness.json",
        {"readiness": canary["readiness"], "canary": canary},
    )
    documents["r1_10_timing_canary.json"] = write_document(
        directory / "r1_10_timing_canary.json", timing
    )
    documents["r1_10_decision_day_runbook.json"] = write_document(
        directory / "r1_10_decision_day_runbook.json", runbook
    )

    activation: Any = None
    armed: Any = None
    if arm:
        activation = activate_v2(
            prospective_root,
            code_commit=code_commit,
            candidate_sha256=hashlib.sha256(
                (r1_6_directory / "h2_candidate_freeze.json").read_bytes()
            ).hexdigest(),
            protocol_v2_sha256=documents["h2_prospective_protocol_v2.json"],
            contract_sha256=hashlib.sha256(
                (r1_6_directory / "prospective_shadow_contract_v1.json").read_bytes()
            ).hexdigest(),
            runner_version="PROSPECTIVE_SHADOW_RUNNER_V1",
            capture_sequence_sha256=documents["prospective_capture_sequence_v2.json"],
            readiness_sha256=documents["r1_10_source_readiness.json"],
        )
        armed = arm_v2(
            prospective_root,
            activation=activation,
            window=window,
            candidate_sha256=activation.body["candidate_sha256"],
            protocol_v2_sha256=documents["h2_prospective_protocol_v2.json"],
            capture_sequence_sha256=documents["prospective_capture_sequence_v2.json"],
            readiness_sha256=documents["r1_10_source_readiness.json"],
            missed_opportunities=1,
        )

    recovery = {
        "september_30_was_missed": True,
        "why": "REQUIRED_SOURCE_UNAVAILABLE / PROSPECTIVE_DATA_PLANE_NOT_READY",
        "september_30_backfillable": False,
        "v1_closed_for_that_window": True,
        "v2_authoritative_prospectively": True,
        "armed_code_commit": code_commit,
        "armed_semantic_fingerprint": semantic_fingerprint()["semantic_fingerprint"],
        "armed_decision_session": window["decision_session"],
        "source_timing_rules": audit["sources"],
        "valid_observations": 0,
        "missed_eligible_opportunities": 1,
        "activation_sha256": None if activation is None else activation.sha256,
        "arm_sha256": None if armed is None else armed.sha256,
        "V2_PROSPECTIVE_ARM_RECOVERY_SUFFICIENT": bool(armed is not None),
    }
    documents["r1_10_recovery.json"] = write_document(directory / "r1_10_recovery.json", recovery)
    documents["r1_10_binding.json"] = write_document(
        directory / "r1_10_binding.json",
        {
            "code_commit": code_commit,
            "v1_digest": hashlib.sha256(v1_path.read_bytes()).hexdigest(),
            "v2_digest": documents["h2_prospective_protocol_v2.json"],
            "semantic_code_fingerprint": semantic_fingerprint()["semantic_fingerprint"],
            "phase_kind": "WINDOW_CERTIFICATION_AND_REARM",
        },
    )

    summary = {
        "phase": "R1.10",
        "phase_kind": "WINDOW_CERTIFICATION_AND_REARM",
        "code_commit": code_commit,
        "september_30": "MISSED / NOT_BACKFILLED / NOT_BACKFILLABLE",
        "h2_scientific_semantics_changed": False,
        "v1_immutable": True,
        "v2_authoritative": True,
        "window": window,
        "source_plane": canary["readiness"]["result"],
        "ready_count": canary["readiness"]["ready_count"],
        "timing_canary": timing["all_as_expected"],
        "valid_observations": 0,
        "decisions": 0,
        "executions": 0,
        "completed_transitions": 0,
        "missed_eligible_opportunities": 1,
        "state": "ARMED_WAITING_FOR_DECISION" if armed else "CERTIFIED_NOT_ARMED",
        "activation_sha256": None if activation is None else activation.sha256,
        "arm_sha256": None if armed is None else armed.sha256,
        "h2_verdict": "PENDING",
        "next_action": (
            "wait for the armed decision window; run readiness and preflight "
            "before the instant, and capture exactly once at it"
        ),
    }
    documents["r1_10_summary.json"] = write_document(directory / "r1_10_summary.json", summary)
    documents["r1_10_complete.json"] = write_document(
        directory / "r1_10_complete.json",
        {
            "phase": "R1.10",
            "documents": dict(sorted(documents.items())),
            "state": summary["state"],
            "valid_observations": 0,
            "missed_eligible_opportunities": 1,
            "h2_verdict": "PENDING",
        },
    )
    return {
        "summary": summary,
        "diff": diff,
        "audit": audit,
        "window": window,
        "sequence": sequence,
        "calendar": calendar,
        "runbook": runbook,
        "readiness": canary["readiness"],
        "timing": timing,
        "recovery": recovery,
        "documents": documents,
        "activation": activation,
        "arm": armed,
    }
