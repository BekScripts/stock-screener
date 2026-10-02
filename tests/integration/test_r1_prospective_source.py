"""Adversarial tests for the prospective source plane.

R1.8 was lost to one mistake with two faces: a source plane that did not exist,
and no gate that would have said so before the decision instant. Each test here
names a way either could recur — stale data accepted, a future bar admitted, a
post-cutoff filing leaking through a current aggregate, a missing source
treated as merely absent, a historical namespace written to — and asserts the
code refuses it.

The September 30 fixture is the permanent one. It encodes the exact situation
that was lost and must never read READY again.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from stock_screener.prospective_source import (
    CANARY_CLASSIFICATION,
    DISPOSITIONS,
    FORBIDDEN_PREFIXES,
    FRESHNESS_POLICY,
    NORMALIZER_VERSION,
    PROSPECTIVE_STORE_ROOT,
    PROTOCOL_V2_ID,
    REQUIRED_INPUTS,
    SOURCE_TEMPORAL_CLASS,
    Completeness,
    SnapshotEnvelope,
    SnapshotStore,
    StaleSourceError,
    StoreConflictError,
    TemporalClass,
    admissible_for,
    assess,
    audit_v1,
    content_digest,
    disposition_record,
    fetch_market,
    fetch_sec_availability,
    fetch_universe,
    preflight,
    protocol_v2,
    refuses_historical_namespace,
    session_close,
)

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
R1_6 = REPO / ".evidence" / "research" / "r1_6"
EASTERN = ZoneInfo("America/New_York")

#: The instant that was lost, and the age of the newest data that existed.
SEPT_30 = datetime(2026, 9, 30, 16, 0, tzinfo=EASTERN)
AUGUST_DATA = datetime(2026, 8, 28, 16, 0, tzinfo=EASTERN)

needs_frozen = pytest.mark.skipif(
    not (R1_6 / "h2_prospective_protocol_v1.json").is_file(),
    reason="the frozen R1.6 protocol is runtime data, not committed",
)


def _env(
    *,
    source_type: str = "market",
    semantic_as_of: datetime = SEPT_30,
    retrieved_at: datetime | None = None,
    completeness: Completeness = Completeness.COMPLETE,
    records: int = 1,
    temporal_class: TemporalClass | None = None,
) -> SnapshotEnvelope:
    payload = [{"n": records}]
    # Declare the class the production adapter would, so a fixture cannot be
    # accidentally more permissive than the real source it stands for.
    temporal = temporal_class or SOURCE_TEMPORAL_CLASS.get(
        source_type, TemporalClass.SESSION_BOUNDED
    )
    return SnapshotEnvelope(
        source_type=source_type,
        source_name="fixture",
        source_version="v1",
        operation="fetch",
        retrieved_at=retrieved_at or (semantic_as_of + timedelta(hours=1)),
        semantic_as_of=semantic_as_of,
        content_sha256=content_digest(payload),
        request="r",
        normalizer_version=NORMALIZER_VERSION,
        record_count=records,
        completeness=completeness,
        temporal_class=temporal,
    )


# --------------------------------------------------------------------------
# The September 30 regression. This must never read READY.
# --------------------------------------------------------------------------


def test_the_september_30_situation_is_permanently_not_ready() -> None:
    august = _env(semantic_as_of=AUGUST_DATA, retrieved_at=SEPT_30 + timedelta(hours=1))

    verdict = admissible_for(august, SEPT_30, max_age=FRESHNESS_POLICY["market"])

    assert verdict["admissible"] is False
    assert "SOURCE_TOO_STALE_FOR_DECISION" in verdict["reasons"]


def test_the_september_30_readiness_assessment_is_blocked_not_ready() -> None:
    report = assess(
        {
            "universe": _env(source_type="universe", semantic_as_of=AUGUST_DATA),
            "sec_availability": None,
            "market": _env(semantic_as_of=AUGUST_DATA),
        },
        decision_instant=SEPT_30,
    )

    assert report.ready is False
    assert report.as_json()["result"] == "BLOCKED"
    assert report.ready_count < len(REQUIRED_INPUTS)


def test_no_amount_of_late_retrieval_makes_august_data_admissible() -> None:
    for delay in (timedelta(hours=1), timedelta(days=1), timedelta(days=30)):
        late = _env(semantic_as_of=AUGUST_DATA, retrieved_at=SEPT_30 + delay)
        assert (
            admissible_for(late, SEPT_30, max_age=FRESHNESS_POLICY["market"])["admissible"] is False
        )


# --------------------------------------------------------------------------
# The as-of invariant
# --------------------------------------------------------------------------


def test_late_retrieval_of_timely_facts_is_admissible() -> None:
    timely = _env(semantic_as_of=SEPT_30, retrieved_at=SEPT_30 + timedelta(hours=3))

    verdict = admissible_for(timely, SEPT_30, max_age=FRESHNESS_POLICY["market"])

    assert verdict["admissible"] is True
    assert verdict["retrieved_after_semantic_time"] is True


def test_facts_after_the_instant_are_never_admissible() -> None:
    future = _env(semantic_as_of=SEPT_30 + timedelta(minutes=1))

    verdict = admissible_for(future, SEPT_30)

    assert verdict["admissible"] is False
    assert "SEMANTIC_AS_OF_AFTER_DECISION_INSTANT" in verdict["reasons"]


def test_retrieved_at_and_semantic_as_of_are_never_assumed_equal() -> None:
    envelope = _env(semantic_as_of=SEPT_30, retrieved_at=SEPT_30 + timedelta(hours=2))

    body = envelope.as_json()

    assert body["retrieved_at"] != body["semantic_as_of"]
    assert body["retrieved_after_semantic_time"] is True


def test_an_incomplete_source_is_not_admissible() -> None:
    assert (
        admissible_for(_env(completeness=Completeness.INCOMPLETE), SEPT_30)["admissible"] is False
    )


@pytest.mark.parametrize(
    "state", [Completeness.INCOMPLETE, Completeness.UNAVAILABLE, Completeness.AMBIGUOUS]
)
def test_only_complete_sources_are_admissible(state: Completeness) -> None:
    assert admissible_for(_env(completeness=state), SEPT_30)["admissible"] is False


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(StaleSourceError, match="timezone-aware"):
        SnapshotEnvelope(
            source_type="market",
            source_name="fixture",
            source_version="v1",
            operation="fetch",
            retrieved_at=datetime(2026, 9, 30, 16, 0),  # noqa: DTZ001 - the point
            semantic_as_of=SEPT_30,
            content_sha256="0" * 64,
            request="r",
            normalizer_version=NORMALIZER_VERSION,
            record_count=1,
            completeness=Completeness.COMPLETE,
        )


def test_a_naive_decision_instant_is_refused() -> None:
    with pytest.raises(StaleSourceError, match="timezone-aware"):
        admissible_for(_env(), datetime(2026, 9, 30, 16, 0))  # noqa: DTZ001 - the point


# --------------------------------------------------------------------------
# Readiness is conjunctive
# --------------------------------------------------------------------------


def test_all_six_inputs_are_required() -> None:
    assert len(REQUIRED_INPUTS) == 6


def test_five_of_six_is_blocked_not_nearly_ready() -> None:
    report = assess(
        {
            "universe": _env(source_type="universe"),
            "sec_availability": _env(source_type="sec_availability"),
            "market": None,
        },
        decision_instant=SEPT_30,
    )

    assert report.ready is False
    assert report.as_json()["result"] == "BLOCKED"


def test_a_missing_source_blocks_rather_than_being_skipped() -> None:
    report = assess({"universe": None}, decision_instant=SEPT_30)

    assert report.inputs["universe"]["reasons"] == ["SOURCE_UNAVAILABLE"]
    assert any("SOURCE_UNAVAILABLE" in blocker for blocker in report.blockers)


def test_derived_inputs_block_when_their_sources_do() -> None:
    report = assess({"market": None}, decision_instant=SEPT_30)

    for name in ("score_inputs", "ranked_cross_section"):
        assert report.inputs[name]["reasons"] == ["UPSTREAM_SOURCE_BLOCKED"]


def test_a_derived_input_is_never_reported_ready_by_this_module() -> None:
    """R1.12 narrowed what this module claims.

    It previously marked a derived input READY whenever its sources were
    admissible, which R1.9 and R1.10 then certified as 6/6 with no producer in
    existence. Derivability is now reported as what it is.
    """
    before = SEPT_30 - timedelta(hours=1)
    universe = _env(source_type="universe", semantic_as_of=before, retrieved_at=before)
    report = assess(
        {
            "universe": universe,
            "tradability": universe,
            "sec_availability": _env(source_type="sec_availability"),
            "market": _env(),
        },
        decision_instant=SEPT_30,
    )

    assert report.sources_ready is True
    assert report.ready_count == 4
    for name in ("score_inputs", "ranked_cross_section"):
        assert report.inputs[name]["status"] == "DERIVABLE_NOT_PRODUCED"
        assert report.inputs[name]["fingerprint"] is None
    assert report.as_json()["readiness_kind"] == "SOURCE_AND_DERIVABILITY"
    assert report.as_json()["result"] == "SOURCES_READY"


@pytest.mark.parametrize("missing", ["universe", "sec_availability", "market"])
def test_every_source_outage_combination_blocks(missing: str) -> None:
    envelopes: dict[str, Any] = {
        "universe": _env(source_type="universe"),
        "sec_availability": _env(source_type="sec_availability"),
        "market": _env(),
    }
    envelopes[missing] = None

    assert assess(envelopes, decision_instant=SEPT_30).ready is False


def test_sec_availability_has_no_freshness_bound() -> None:
    # A filing accepted in 2019 is old, not stale.
    assert FRESHNESS_POLICY["sec_availability"] is None
    ancient = _env(
        source_type="sec_availability",
        semantic_as_of=SEPT_30,
        retrieved_at=SEPT_30 + timedelta(hours=1),
    )
    assert admissible_for(ancient, SEPT_30, max_age=None)["admissible"] is True


def test_the_freshness_policy_is_prospective_only() -> None:
    body = assess({}, decision_instant=SEPT_30).as_json()

    assert body["freshness_applies_prospectively_only"] is True
    assert body["september_30_not_reassessed"] is True


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------


class _Bar:
    def __init__(self, day: date, close: float = 10.0) -> None:
        self.date = day
        self.open = close
        self.high = close
        self.low = close
        self.close = close
        self.volume = 1_000


class _Profile:
    def __init__(self, ticker: str, *, active: bool = True, fund: bool = False) -> None:
        self.ticker = ticker
        self.name = f"{ticker} Inc"
        self.exchange = "NASDAQ"
        self.is_active = active
        self.is_fund = fund
        self.quote_currency = "USD"


class _Market:
    def __init__(self, bars: dict[str, list[_Bar]]) -> None:
        self._bars = bars

    def get_stock_universe(self) -> list[_Profile]:
        return [_Profile(t) for t in sorted(self._bars)]

    def get_daily_prices_batch(
        self, tickers: list[str], start: date, end: date
    ) -> dict[str, list[_Bar]]:
        return {t: self._bars.get(t, []) for t in tickers}


class _Filing:
    def __init__(self, accession: str, accepted: datetime) -> None:
        self.accession = accession
        self.accepted_at = accepted
        self.filed_date = accepted.date()
        self.form = "10-Q"
        self.source = "submissions"


class _Sec:
    def __init__(self, filings: list[_Filing]) -> None:
        self._filings = filings

    def get_filing_acceptance(self, ticker: str) -> list[_Filing]:
        return self._filings


def test_a_future_bar_is_dropped_not_admitted() -> None:
    session = date(2026, 9, 30)
    market = _Market({"AAA": [_Bar(session), _Bar(date(2026, 10, 1))]})

    result = fetch_market(
        market, ["AAA"], decision_session=session, lookback_start=date(2026, 9, 1)
    )

    assert len(result.payload["dropped_future_bars"]) == 1
    assert all(row["session"] <= session.isoformat() for row in result.records)


def test_market_semantic_as_of_is_the_session_close_not_retrieval() -> None:
    session = date(2026, 9, 30)
    market = _Market({"AAA": [_Bar(session)]})

    result = fetch_market(
        market, ["AAA"], decision_session=session, lookback_start=date(2026, 9, 1)
    )

    assert result.envelope.semantic_as_of == session_close(session)
    assert result.envelope.retrieved_after_semantic_time is True


def test_a_price_source_that_never_reaches_the_session_fails_closed() -> None:
    market = _Market({"AAA": [_Bar(date(2026, 8, 28))]})

    with pytest.raises(StaleSourceError, match="no bar reaches the decision session"):
        fetch_market(
            market,
            ["AAA"],
            decision_session=date(2026, 9, 30),
            lookback_start=date(2026, 8, 1),
        )


def test_a_post_cutoff_filing_is_excluded() -> None:
    before = _Filing("acc-before", SEPT_30 - timedelta(hours=1))
    after = _Filing("acc-after", SEPT_30 + timedelta(minutes=30))

    result = fetch_sec_availability(_Sec([before, after]), ["AAA"], decision_instant=SEPT_30)

    assert [row["accession"] for row in result.records] == ["acc-before"]
    assert len(result.payload["excluded_after_cutoff"]) == 1


def test_an_admitted_filing_traces_to_an_accession() -> None:
    result = fetch_sec_availability(
        _Sec([_Filing("acc-1", SEPT_30 - timedelta(days=5))]),
        ["AAA"],
        decision_instant=SEPT_30,
    )

    assert result.records[0]["accession"] == "acc-1"
    assert result.records[0]["accepted_at"]
    assert result.records[0]["source"]


def test_current_aggregate_fundamentals_are_refused_by_contract() -> None:
    from stock_screener.prospective_source.adapters import NON_PIT_SAFE_OPERATIONS

    for operation in ("companyfacts", "get_financial_statements"):
        assert operation in NON_PIT_SAFE_OPERATIONS


def test_a_sec_source_failure_marks_the_snapshot_incomplete() -> None:
    class _Broken:
        def get_filing_acceptance(self, ticker: str) -> list[_Filing]:
            raise RuntimeError("provider down")

    result = fetch_sec_availability(_Broken(), ["AAA"], decision_instant=SEPT_30)

    assert result.envelope.completeness is Completeness.INCOMPLETE
    assert admissible_for(result.envelope, SEPT_30)["admissible"] is False


def test_the_universe_records_tradability_provenance() -> None:
    result = fetch_universe(_Market({"AAA": []}), decision_session=date(2026, 9, 30))

    row = result.records[0]
    assert row["tradability_source"]
    assert row["tradability_semantics"] == "CURRENT_AT_RETRIEVAL"
    assert row["exchange"] == "NASDAQ"


def test_unknown_tradability_is_not_promoted_to_true() -> None:
    class _Unknown(_Market):
        def get_stock_universe(self) -> list[_Profile]:
            return [_Profile("AAA", active=False)]

    result = fetch_universe(_Unknown({}), decision_session=date(2026, 9, 30))

    assert result.records[0]["is_active"] is False


def test_the_universe_semantic_as_of_is_retrieval_not_the_decision() -> None:
    result = fetch_universe(_Market({"AAA": []}), decision_session=date(2026, 9, 30))

    assert result.envelope.semantic_as_of == result.envelope.retrieved_at
    assert any("never backdated" in note for note in result.envelope.notes)


def test_an_empty_universe_fails_closed() -> None:
    class _Empty(_Market):
        def get_stock_universe(self) -> list[_Profile]:
            return []

    with pytest.raises(StaleSourceError, match="no listings"):
        fetch_universe(_Empty({}), decision_session=date(2026, 9, 30))


def test_a_listing_without_a_ticker_is_dropped_and_recorded() -> None:
    class _Nameless(_Market):
        def get_stock_universe(self) -> list[Any]:
            good = _Profile("AAA")
            bad = _Profile("")
            return [good, bad]

    result = fetch_universe(_Nameless({}), decision_session=date(2026, 9, 30))

    assert len(result.records) == 1
    assert result.envelope.ambiguity


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


def test_a_snapshot_is_written_once(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / PROSPECTIVE_STORE_ROOT)
    payload = [{"a": 1}]
    envelope = _env()
    object.__setattr__(envelope, "content_sha256", content_digest(payload))

    first = store.put(envelope, payload)
    second = store.put(envelope, payload)

    assert first.written is True
    assert second.written is False


def test_a_payload_that_disagrees_with_its_envelope_is_refused(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / PROSPECTIVE_STORE_ROOT)

    with pytest.raises(StoreConflictError, match="must describe its own bytes"):
        store.put(_env(), [{"different": True}])


def test_a_stored_snapshot_reads_back_verified(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / PROSPECTIVE_STORE_ROOT)
    payload = [{"a": 1}]
    envelope = _env()
    object.__setattr__(envelope, "content_sha256", content_digest(payload))
    stored = store.put(envelope, payload)

    read = store.get("market", "unbound", stored.content_sha256[:16])

    assert read["verified"] is True
    assert read["payload"] == payload


def test_a_tampered_payload_is_detected_on_read(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / PROSPECTIVE_STORE_ROOT)
    payload = [{"a": 1}]
    envelope = _env()
    object.__setattr__(envelope, "content_sha256", content_digest(payload))
    stored = store.put(envelope, payload)
    stored.payload_path.write_text(json.dumps([{"a": 2}]), encoding="utf-8")

    with pytest.raises(StoreConflictError, match="no longer matches its envelope"):
        store.get("market", "unbound", stored.content_sha256[:16])


@pytest.mark.parametrize("namespace", ["filings", "submissions", "fulltext", "evidence"])
def test_a_historical_namespace_cannot_be_written(tmp_path: Path, namespace: str) -> None:
    root = tmp_path / PROSPECTIVE_STORE_ROOT

    with pytest.raises(StoreConflictError, match="historical evidence namespace"):
        refuses_historical_namespace(root / namespace / "x", root)


def test_a_write_outside_the_prospective_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StoreConflictError, match="only be written beneath"):
        refuses_historical_namespace(tmp_path / "elsewhere", tmp_path / "prospective-source")


def test_the_forbidden_prefixes_cover_the_historical_corpus() -> None:
    for name in ("filings", "submissions", "fulltext", "evidence", "research"):
        assert name in FORBIDDEN_PREFIXES


# --------------------------------------------------------------------------
# Governance
# --------------------------------------------------------------------------


@needs_frozen
def test_the_v1_audit_finds_no_missed_observation_handling() -> None:
    v1 = json.loads((R1_6 / "h2_prospective_protocol_v1.json").read_text(encoding="utf-8"))

    audit = audit_v1(v1)

    assert audit["v1_defines_missed_observation_handling"] is False
    assert audit["disposition"] == "P3_PROTOCOL_SILENT_AMENDMENT_REQUIRED"
    assert audit["disposition"] in DISPOSITIONS


@needs_frozen
def test_the_audit_records_what_v1_does_settle() -> None:
    v1 = json.loads((R1_6 / "h2_prospective_protocol_v1.json").read_text(encoding="utf-8"))

    audit = audit_v1(v1)

    settled = " ".join(row["settles"] for row in audit["v1_settles"])
    assert "can never count" in settled


@needs_frozen
def test_the_audit_explains_why_the_arming_clause_does_not_apply() -> None:
    v1 = json.loads((R1_6 / "h2_prospective_protocol_v1.json").read_text(encoding="utf-8"))

    audit = audit_v1(v1)
    clauses = {row["clause"]: row for row in audit["v1_does_not_settle"]}

    assert (
        "cutoff was satisfied"
        in clauses["first_eligible_decision.if_the_arming_requirement_is_missed"][
            "why_it_does_not_apply"
        ]
    )


def _disposition(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "audit": {
            "disposition": "P3_PROTOCOL_SILENT_AMENDMENT_REQUIRED",
            "reasoning": "V1 is silent",
            "v1_defines_missed_observation_handling": False,
        },
        "incident": {"at": "2026-09-30T21:23:16Z"},
        "v1_digest": "a" * 64,
        "semantic_fingerprint_changed": True,
    }
    body.update(overrides)
    return disposition_record(**body)


def test_september_30_remains_lost_in_the_disposition() -> None:
    record = _disposition()

    assert record["september_30_remains_lost"] is True
    assert record["september_30_recoverable"] is False
    assert record["september_30_backfilled"] is False


def test_the_disposition_keeps_v1_immutable_and_requires_v2() -> None:
    record = _disposition()

    assert record["v1_remains_immutable"] is True
    assert record["v1_edited_in_place"] is False
    assert record["new_protocol_version_required"] is True
    assert record["new_protocol_version"] == PROTOCOL_V2_ID


def test_the_disposition_does_not_blame_h2() -> None:
    record = _disposition()

    assert record["h2_status"] == "PENDING PROSPECTIVE EVIDENCE"
    assert record["h2_failed"] is False
    assert record["h2_supported"] is False


def test_a_moved_fingerprint_is_an_independent_re_arm_reason() -> None:
    moved = _disposition(semantic_fingerprint_changed=True)
    unmoved = _disposition(semantic_fingerprint_changed=False)

    assert len(moved["re_arm_reasons"]) == 2
    assert len(unmoved["re_arm_reasons"]) == 1
    assert moved["re_arm_required"] is True


def test_v2_changes_nothing_about_h2() -> None:
    successor = protocol_v2(disposition=_disposition(), code_commit="0" * 40)

    assert successor["required_decisions"] == 5
    assert successor["required_transitions"] == 4
    assert (
        successor["added_in_v2"]["missed_observation_disposition"]["window_length_unchanged"]
        is True
    )
    joined = " ".join(successor["unchanged_from_v1"])
    assert "window is not shortened" in joined


def test_v2_may_not_reinterpret_september_30() -> None:
    successor = protocol_v2(disposition=_disposition(), code_commit="0" * 40)

    assert "reinterpret September 30 as captured" in successor["may_not"]
    assert "shorten the confirmation requirement" in successor["may_not"]
    assert "authorise broker, paper or live trading" in successor["may_not"]


def test_v2_supersedes_v1_without_editing_it() -> None:
    successor = protocol_v2(disposition=_disposition(), code_commit="0" * 40)

    assert successor["supersedes"] == "H2_PROSPECTIVE_SHADOW_PROTOCOL_V1"
    assert successor["v1_remains"] == "IMMUTABLE AND UNEDITED"


def test_v2_distinguishes_a_pre_sequence_miss_from_a_mid_sequence_one() -> None:
    rule = protocol_v2(disposition=_disposition(), code_commit="0" * 40)["added_in_v2"][
        "missed_observation_disposition"
    ]

    assert "no transition is lost" in rule["if_it_occurs_before_the_sequence_begins"]
    assert "window is invalidated" in rule["if_it_occurs_mid_sequence"]


def test_v2_makes_readiness_a_precondition_of_arming() -> None:
    added = protocol_v2(disposition=_disposition(), code_commit="0" * 40)["added_in_v2"]

    assert "READY" in added["readiness_precondition"]["rule"]
    assert "before the arm" in added["readiness_precondition"]["rule"]


def test_v2_starts_unarmed_with_no_observations() -> None:
    status = protocol_v2(disposition=_disposition(), code_commit="0" * 40)["status_at_freeze"]

    assert status["prospective_runner"] == "NOT_ARMED_UNDER_V2"
    assert status["observations"] == 0
    assert status["prospective_verdict"] == "PENDING"


# --------------------------------------------------------------------------
# Preflight, and the canary's classification
# --------------------------------------------------------------------------


def test_preflight_runs_before_the_instant_and_creates_nothing() -> None:
    gate = preflight(
        armed=True,
        now=SEPT_30 - timedelta(days=1),
        decision_instant=SEPT_30,
        session_is_trading=True,
        store_writable=True,
        adapters_available={"universe": True, "market": True},
        bindings_match=True,
    )

    assert gate["result"] == "GO"
    assert gate["creates_a_decision"] is False
    assert gate["time_gate"]["instant_passed"] is False
    assert gate["time_gate"]["seconds_remaining"] > 0


def test_preflight_fails_when_an_adapter_is_unreachable() -> None:
    gate = preflight(
        armed=True,
        now=SEPT_30 - timedelta(days=1),
        decision_instant=SEPT_30,
        session_is_trading=True,
        store_writable=True,
        adapters_available={"universe": True, "market": False},
        bindings_match=True,
    )

    assert gate["result"] == "NO_GO"
    assert "adapters_available" in gate["failed_gates"]


@pytest.mark.parametrize(
    "gate_name", ["armed", "session_is_trading", "store_writable", "bindings_match"]
)
def test_every_preflight_gate_can_fail_the_check(gate_name: str) -> None:
    arguments: dict[str, Any] = {
        "armed": True,
        "now": SEPT_30 - timedelta(days=1),
        "decision_instant": SEPT_30,
        "session_is_trading": True,
        "store_writable": True,
        "adapters_available": {"universe": True},
        "bindings_match": True,
    }
    arguments[gate_name] = False

    assert preflight(**arguments)["result"] == "NO_GO"


def test_the_canary_is_classified_as_not_an_observation() -> None:
    assert "NOT_H2_DECISION" in CANARY_CLASSIFICATION
    assert "NOT_PROSPECTIVE_OBSERVATION" in CANARY_CLASSIFICATION
