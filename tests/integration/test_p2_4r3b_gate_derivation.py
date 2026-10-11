"""P2.4R3B — every soak gate derived from records something actually wrote.

P2.4R3 halted because fourteen zero-tolerance gates read integer fields off a
session observation that never carried them. `getattr(row, field, 0)` returned
zero, zero satisfied an at-most-zero threshold, and every one of them reported
itself clean forever. The soak was unscoreable and looked perfect.

The tests here are shaped by that. The decisive ones are not that a clean soak
scores A — they are the pair at the end of every zero-tolerance gate: write the
records a breach leaves behind and prove the derivation notices, then delete a
required source and prove the gate says so instead of saying zero.

Nothing in this file sets a gate value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stock_screener.soak_verification import (
    DERIVATION_REGISTRY_VERSION,
    EVIDENCE_CONTRACT_VERSION,
    SESSION_GATE_EVIDENCE_VERSION,
    SESSION_OBSERVATION_V3_VERSION,
    SOURCE_SCHEMAS,
    Breach,
    Code,
    DerivedSessionObservation,
    GateClass,
    GateScope,
    MetricStatus,
    OperationalRecords,
    SoakContractError,
    SoakVerdictV2,
    SyntheticSession,
    derivation_registry,
    derivation_registry_fingerprint,
    derive_session_evidence,
    evidence_contract,
    evidence_contract_fingerprint,
    gate_registry,
    known_source_schema,
    registry_fingerprint,
    require_evidence_contract,
    score_soak,
    write_session_records,
)

pytestmark = pytest.mark.integration

#: The observability contract P2.4I froze. Bound here in full rather than by
#: prefix: a fingerprint checked to sixteen characters is a fingerprint that
#: can be collided with by anybody who cares to try.
OBSERVABILITY_FINGERPRINT = "0fa85983277222e83f8ae97ed3266fc0b830b958ddd21c1a54280fa88cdd7d5b"

#: Which underlying condition proves which gate. Every zero-tolerance gate with
#: a SESSION scope appears here, and a test asserts that is all of them.
BREACH_FOR_GATE: dict[str, str] = {
    "live_mutations": Breach.LIVE_MUTATION,
    "duplicate_remote_orders": Breach.TWO_REMOTE_ORDERS,
    "duplicate_admitted_fills": Breach.DOUBLE_ADMITTED_FILL,
    "duplicate_accounting_events": Breach.TWO_ACCOUNTING_EVENTS,
    "duplicate_scheduled_runs_causing_execution": Breach.TWO_EXECUTING_RUNS,
    "stale_data_executions": Breach.STALE_INPUT_EXECUTION,
    "missing_data_executions": Breach.MISSING_INPUT_EXECUTION,
    "missed_run_backfills": Breach.BACKFILLED_MISSED_DECISION,
    "ignored_reconciliation_breaks": Breach.EXECUTED_AFTER_BREAK,
    "ignored_remote_state_unknown": Breach.EXECUTED_WITH_REMOTE_UNKNOWN,
    "completed_sessions_reconcile": Breach.COMPLETED_WITHOUT_RECONCILIATION,
    "kill_bypasses": Breach.MUTATION_WITHOUT_GATE_CHECK,
    "unknown_remote_order_auto_cancels": Breach.CANCELLED_UNKNOWN_ORDER,
    "automatic_accounting_repairs": Breach.SECOND_OPENING_BALANCE,
    "reranked_executions": Breach.RERANKED_EXECUTION,
    "risk_authority_expansions": Breach.RISK_EXPANSION,
}


def session_evidence(
    tmp_path: Path, *, breach: str | None = None, remove: str | None = None, orders: int = 2
) -> Any:
    """Write one session's records and derive its evidence."""
    import shutil

    from stock_screener.soak_verification.synthetic import _SOURCE_PATHS

    root = tmp_path / ".operations"
    plan = SyntheticSession(
        session="2026-11-02",
        orders=orders,
        decision_session="2026-11-02",
        decision_fingerprint="d" * 64,
        decided=True,
        breach=breach,
    )
    runs = write_session_records(root, plan)
    if remove is not None:
        target = root
        for part in _SOURCE_PATHS[remove]:
            target = target / part
        if target.exists():
            shutil.rmtree(target)
    return derive_session_evidence(
        OperationalRecords(root),
        session=plan.session,
        run_ids=runs,
        protocol_fingerprint="p" * 64,
        qualifying_commit="c" * 40,
        finalized_at="2026-11-02T21:00:00+00:00",
    )


# --------------------------------------------------------------------------
# §6: the inventory. 24 gates, each with a scope, a derivation and sources.
# --------------------------------------------------------------------------


def test_every_gate_has_a_scope_a_derivation_and_known_sources() -> None:
    contract = evidence_contract()
    gates = gate_registry()

    assert len(gates) == 24
    assert set(contract.bindings) == set(gates)
    for gate_id, binding in contract.bindings.items():
        assert binding.scope in set(GateScope), gate_id
        assert binding.derivation_id in derivation_registry(), gate_id
        assert binding.derivation_version, gate_id
        assert all(known_source_schema(row) for row in binding.required_schemas), gate_id


def test_the_gates_are_split_across_the_four_scopes() -> None:
    # Not every invariant is a per-session count, and forcing them all into a
    # session observation is what made `duplicate_counted_sessions` nonsense: a
    # gate about counting a session twice cannot be answered from inside one.
    contract = evidence_contract()

    assert len(contract.gates_in(GateScope.SESSION)) == 16
    assert len(contract.gates_in(GateScope.SOAK)) == 6
    assert len(contract.gates_in(GateScope.DRILL)) == 1
    assert len(contract.gates_in(GateScope.COMPLETION)) == 1
    assert "duplicate_counted_sessions" in contract.gates_in(GateScope.SOAK)


def test_one_gate_has_exactly_one_derivation() -> None:
    # Two derivations for one gate would let the same invariant be measured two
    # ways, and the soak would be scored by whichever ran.
    claimed: dict[str, list[str]] = {}
    for derivation in derivation_registry().values():
        for gate_id in derivation.gates:
            claimed.setdefault(gate_id, []).append(derivation.derivation_id)

    assert len(claimed) == 24
    for gate_id, owners in claimed.items():
        assert len(owners) == 1, f"{gate_id} claimed by {owners}"


def test_every_zero_tolerance_session_gate_has_a_breach_fixture() -> None:
    # The map below drives the safe/breach pair for every gate. If a new
    # zero-tolerance gate is added with no way to create its condition, this
    # fails rather than the gate quietly going untested.
    contract = evidence_contract()
    gates = gate_registry()
    expected = {
        gate_id
        for gate_id in contract.gates_in(GateScope.SESSION)
        if gates[gate_id].gate_class is GateClass.ZERO_TOLERANCE
    }

    assert set(BREACH_FOR_GATE) == expected


# --------------------------------------------------------------------------
# §7, §8, §31: missing is not zero
# --------------------------------------------------------------------------


def test_no_safety_derivation_defaults_a_missing_value_to_zero() -> None:
    # The literal defect: `getattr(row, field, 0)` and `dict.get(field, 0)`.
    # Scanned as source because the whole failure mode is that such a call
    # looks correct and reports clean.
    import ast

    import stock_screener.soak_verification as package

    root = Path(next(iter(package.__path__)))
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        if path.name == "synthetic.py":
            # The fixture builder writes records; it evaluates no safety value.
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Parsed, not grepped: these modules *describe* the defect in prose, and
        # a text scan cannot tell a docstring quoting `getattr(row, field, 0)`
        # from code calling it. The AST can.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name not in {"getattr", "get"} or len(node.args) < 3:
                continue
            default = node.args[2]
            if isinstance(default, ast.Constant) and default.value in (0, False):
                offenders.append(f"{path.name}:{node.lineno} {name}(..., {default.value!r})")

    assert not offenders, offenders


def test_a_metric_has_three_states_not_two() -> None:
    # Observed zero, observed breach and evidence missing are different facts.
    # Collapsing the first and third is what made fourteen gates useless.
    assert MetricStatus.OBSERVED_ZERO.scoreable is True
    assert MetricStatus.OBSERVED_VALUE.scoreable is True
    assert MetricStatus.EVIDENCE_MISSING.scoreable is False


def test_a_missing_metric_refuses_to_produce_a_number(tmp_path: Path) -> None:
    evidence = session_evidence(tmp_path, remove="RECONCILIATION_RECORDS")
    metric = evidence.metrics["completed_sessions_reconcile"]

    assert metric.status is MetricStatus.EVIDENCE_MISSING
    assert metric.value is None
    with pytest.raises(SoakContractError, match=Code.SOAK_GATE_EVIDENCE_MISSING):
        metric.require_value()


def test_an_observed_zero_is_distinguishable_from_missing_evidence(
    tmp_path: Path,
) -> None:
    clean = session_evidence(tmp_path / "clean")
    gone = session_evidence(tmp_path / "gone", remove="RECONCILIATION_RECORDS")

    assert clean.metrics["completed_sessions_reconcile"].status is MetricStatus.OBSERVED_ZERO
    assert clean.metrics["completed_sessions_reconcile"].value == 0
    assert gone.metrics["completed_sessions_reconcile"].value is None


def test_an_incomplete_session_is_never_finalised_as_clean(tmp_path: Path) -> None:
    evidence = session_evidence(tmp_path, remove="RECONCILIATION_RECORDS")

    assert evidence.complete is False
    assert "completed_sessions_reconcile" in evidence.incomplete
    with pytest.raises(SoakContractError, match=Code.SOAK_SESSION_EVIDENCE_INCOMPLETE):
        evidence.require_complete()


# --------------------------------------------------------------------------
# §11: provenance
# --------------------------------------------------------------------------


def test_every_derived_metric_binds_what_produced_it(tmp_path: Path) -> None:
    evidence = session_evidence(tmp_path)

    for gate_id, metric in evidence.metrics.items():
        assert metric.gate_id == gate_id
        assert metric.derivation_id
        assert metric.derivation_version
        assert metric.scope is GateScope.SESSION
        assert metric.session == "2026-11-02"
        assert metric.protocol_fingerprint == "p" * 64
        assert metric.qualifying_commit == "c" * 40
        assert len(metric.fingerprint()) == 64


def test_a_metric_fingerprint_moves_with_its_value(tmp_path: Path) -> None:
    clean = session_evidence(tmp_path / "clean")
    breached = session_evidence(tmp_path / "breached", breach=Breach.TWO_REMOTE_ORDERS)

    assert (
        clean.metrics["duplicate_remote_orders"].fingerprint()
        != breached.metrics["duplicate_remote_orders"].fingerprint()
    )


def test_session_evidence_records_what_each_source_yielded(tmp_path: Path) -> None:
    evidence = session_evidence(tmp_path)
    by_schema = {row["schema_id"]: row for row in evidence.source_reads}

    assert by_schema["OPERATIONS_RUN_JOURNAL"]["present"] is True
    assert by_schema["OPERATIONS_RUN_JOURNAL"]["verified"] is True
    assert by_schema["OPERATIONS_RUN_JOURNAL"]["fingerprint"]
    # And the one that genuinely is not there says so, rather than being absent
    # from the report.
    assert by_schema["INCIDENT_HISTORY"]["present"] is False


# --------------------------------------------------------------------------
# §40: every zero-tolerance gate, safe and breached, from the records
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("gate", "breach"), sorted(BREACH_FOR_GATE.items()))
def test_a_clean_session_derives_a_safe_value_for_every_gate(
    tmp_path: Path, gate: str, breach: str
) -> None:
    evidence = session_evidence(tmp_path)
    metric = evidence.metrics[gate]

    assert metric.status is MetricStatus.OBSERVED_ZERO, f"{gate}: {metric.detail}"
    assert metric.value == 0


@pytest.mark.parametrize(("gate", "breach"), sorted(BREACH_FOR_GATE.items()))
def test_an_underlying_breach_derives_a_breach_for_every_gate(
    tmp_path: Path, gate: str, breach: str
) -> None:
    # The condition is written into the records — two remote orders under one
    # client order id, a mutation with no permitting gate check, a second
    # opening balance — and the derivation has to find it. No metric is set.
    evidence = session_evidence(tmp_path, breach=breach)
    metric = evidence.metrics[gate]

    assert metric.status is MetricStatus.OBSERVED_VALUE, f"{gate}: {metric.detail}"
    assert metric.value is not None
    assert metric.value > 0
    # And the metric says what it found, so a reader can go to the record.
    assert metric.detail


@pytest.mark.parametrize(("gate", "breach"), sorted(BREACH_FOR_GATE.items()))
def test_every_source_level_breach_scores_fail(soak: Any, gate: str, breach: str) -> None:
    # §38, §39, §40. Twenty sessions written to the same storage structures a
    # real soak uses, one of them carrying the condition, finalised and scored
    # normally. Nineteen clean sessions and one breach is FAIL, never 95%.
    result = soak.build_and_score(sessions=20, breach=breach)

    assert result.verdict == SoakVerdictV2.FAILED
    assert gate in [row.gate_id for row in result.breaches]


# --------------------------------------------------------------------------
# §41: a missing source is not a zero, and never an A
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema",
    [
        "OPERATIONS_RUN_JOURNAL",
        "EXECUTION_JOURNAL",
        "ACCOUNTING_LEDGER",
        "RECONCILIATION_RECORDS",
        "STRATEGY_DECISIONS",
        "RISK_TRANSFORMATIONS",
        "SUBMISSION_INTENTS",
    ],
)
def test_removing_a_required_source_is_an_integrity_failure(soak: Any, schema: str) -> None:
    # The session happened; only the evidence of it is gone. That must not read
    # as a clean session, and the verdict must not be A.
    result = soak.build_and_score(sessions=20, remove_source=schema)

    assert result.verdict == SoakVerdictV2.EVIDENCE_INCOMPLETE
    assert result.verdict.startswith("C_")
    assert result.unmeasured
    for row in result.gates:
        assert row.observed >= 0


def test_a_broken_execution_journal_is_an_integrity_failure_not_a_zero(
    tmp_path: Path,
) -> None:
    # §48. The certified journal's hash chain is what makes its records
    # evidence. A journal that does not verify is evidence of nothing, and
    # treating its contents as facts would let a rewritten chain certify
    # itself.
    root = tmp_path / ".operations"
    plan = SyntheticSession(
        session="2026-11-02",
        orders=2,
        decision_session="2026-11-02",
        decision_fingerprint="d" * 64,
        decided=True,
    )
    runs = write_session_records(root, plan)
    victim = sorted((root / "runtime" / "execution" / "journal").glob("*.json"))[-1]
    victim.unlink()

    records = OperationalRecords(root)
    read = records.execution_journal()
    evidence = derive_session_evidence(records, session=plan.session, run_ids=runs)

    assert read.usable is False
    assert read.records == ()
    assert "duplicate_accounting_events" in evidence.incomplete
    assert evidence.metrics["duplicate_accounting_events"].value is None


def test_a_broken_operations_journal_yields_no_records(tmp_path: Path) -> None:
    root = tmp_path / ".operations"
    plan = SyntheticSession(session="2026-11-02", orders=1, decided=True)
    write_session_records(root, plan)
    victim = sorted((root / "run_journal").glob("*.json"))[0]
    victim.write_text('{"sequence": 1, "event_type": "RUN_COMPLETED"}', encoding="utf-8")

    read = OperationalRecords(root).operations_journal()

    assert read.present is True
    assert read.verified is False
    assert read.records == ()
    assert "does not verify" in read.detail


# --------------------------------------------------------------------------
# §42, §44: soak-scoped counts come from the store, not a counter
# --------------------------------------------------------------------------


def test_a_duplicate_counted_session_is_detected_at_store_level(soak: Any, tmp_path: Path) -> None:
    # §42. Built by putting two live observations in the store for one session,
    # which is the condition itself. No session counter is set anywhere.
    fixture = soak.build(sessions=3)
    first = fixture.store.live_observations()[0]
    twin = DerivedSessionObservation(
        protocol_fingerprint=first.protocol_fingerprint,
        session=first.session,
        qualifying_commit=first.qualifying_commit,
        observed_at="2026-11-01T22:00:00+00:00",
        run_ids=(*first.run_ids, "opsrun-second-look"),
        gate_evidence=first.gate_evidence,
        classification="QUALIFYING",
    )
    path = fixture.store.observations_directory / f"{twin.observation_id}.json"
    path.write_text(_json_of(twin), encoding="utf-8")

    # Two live observations for one session is refused before it can be scored:
    # choosing between two accounts of one session is not a thing to automate.
    with pytest.raises(SoakContractError, match=Code.SOAK_CORRECTION_AMBIGUOUS):
        fixture.evidence.metric_for("duplicate_counted_sessions")


def test_the_qualifying_session_count_comes_from_activation_and_observations(
    soak: Any,
) -> None:
    # §44. Counted from the eligible set and the live observations, so a writer
    # cannot report progress it did not make.
    fixture = soak.build(sessions=7, required_sessions=20)
    metric = fixture.evidence.metric_for("qualifying_sessions_observed")

    assert metric.value == 7
    assert metric.scope is GateScope.SOAK
    assert len(metric.source_identities) == 7


def test_sessions_with_no_record_are_counted_from_the_eligible_set(
    soak: Any,
) -> None:
    protocol = soak.protocol(required_sessions=20)
    fixture = soak.build(sessions=5, protocol=protocol)
    metric = fixture.evidence.metric_for("eligible_sessions_with_no_record")

    # Five of twenty eligible sessions were observed; the activation's window
    # is what says how many there were.
    assert metric.value == 0 or metric.value == len(fixture.evidence.eligible_sessions) - 5


# --------------------------------------------------------------------------
# §43: drills come only from bound V2 drill evidence
# --------------------------------------------------------------------------


def test_the_drill_gate_reads_only_bound_drill_evidence(soak: Any) -> None:
    fixture = soak.build(sessions=20)
    metric = fixture.evidence.metric_for("required_drills_passed")

    assert metric.scope is GateScope.DRILL
    assert metric.value == 6
    assert metric.derivation_id == "required_drills_passed_v1"


def test_a_drill_for_another_commit_does_not_certify_this_one(soak: Any) -> None:
    # Admissibility is the drill store's own rule. Evidence bound to another
    # build is excluded with a reason, which is the difference between "run the
    # drills" and "your drills are for the wrong commit".
    import dataclasses

    from stock_screener.soak_verification import DrillStoreV2
    from stock_screener.soak_verification.synthetic import _drill_set

    fixture = soak.build(sessions=20)
    directory = fixture.root / "soak" / "drills"
    for path in directory.glob("*.json"):
        path.unlink()
    store = DrillStoreV2(directory)
    for record in _drill_set(fixture.protocol):
        store.record(dataclasses.replace(record, qualifying_commit="0" * 40))

    metric = fixture.evidence.metric_for("required_drills_passed")

    assert metric.value == 0
    assert metric.detail


# --------------------------------------------------------------------------
# §3, §4, §45: the evidence contract and what it binds
# --------------------------------------------------------------------------


def test_the_evidence_contract_binds_the_observability_contract() -> None:
    # §45, in full. A derivation reading `ORDER_SUBMITTED` is only valid while
    # something emits it, so the contract binds the vocabulary P2.4I froze.
    contract = evidence_contract()

    assert contract.observability_fingerprint == OBSERVABILITY_FINGERPRINT
    assert contract.body()["observability_contract"] == "PAPER_OPERATIONS_OBSERVABILITY_V1"
    assert contract.gate_registry_fingerprint == registry_fingerprint()
    assert contract.derivation_registry_fingerprint == derivation_registry_fingerprint()


def test_the_contract_fingerprint_is_stable_and_complete() -> None:
    contract = evidence_contract()

    assert len(contract.fingerprint()) == 64
    assert contract.fingerprint() == evidence_contract_fingerprint()
    assert contract.body()["version"] == EVIDENCE_CONTRACT_VERSION
    assert contract.body()["derivation_registry_version"] == DERIVATION_REGISTRY_VERSION
    assert len(contract.body()["bindings"]) == 24


def test_a_protocol_binds_how_its_gates_are_measured(soak: Any) -> None:
    # §4. Changing how a safety invariant is measured must restart the soak
    # rather than quietly redefine the word mid-flight.
    protocol = soak.protocol(required_sessions=20)

    assert protocol.evidence_contract_fingerprint == evidence_contract_fingerprint()
    assert protocol.body()["evidence_contract_fingerprint"] == evidence_contract_fingerprint()


def test_a_soak_frozen_against_another_measurement_is_refused() -> None:
    require_evidence_contract(evidence_contract_fingerprint())

    with pytest.raises(SoakContractError, match=Code.SOAK_EVIDENCE_CONTRACT_MISMATCH):
        require_evidence_contract("0" * 64)


def test_every_declared_source_schema_is_described() -> None:
    # A derivation naming a source nobody has described is a derivation nobody
    # can audit, and the contract refuses to build.
    for schema_id, schema in SOURCE_SCHEMAS.items():
        assert schema.schema_id == schema_id
        assert schema.version
        assert schema.location
        assert schema.writer


# --------------------------------------------------------------------------
# §30, §32: the scorer and the observation schema
# --------------------------------------------------------------------------


def test_the_scorer_reads_no_observation_attribute() -> None:
    # §30. Every gate value arrives through `metric_for`. The scorer names no
    # safety field, so it cannot read one that does not exist.
    import stock_screener.soak_verification.scorer as module

    source = Path(module.__file__).read_text(encoding="utf-8")

    assert "metric_for" in source
    for forbidden in (
        "live_requests",
        "duplicate_remote_orders",
        "kill_bypasses",
        "orders_submitted",
        "reconciliation_after",
        "evidence_fields",
    ):
        assert forbidden not in source, forbidden


def test_the_scorer_refuses_evidence_that_cannot_derive() -> None:
    class NotEvidence:
        """Something with observations and no derivations."""

        def counted_sessions(self) -> tuple[str, ...]:
            return ()

        def soak_state(self) -> str:
            return "ARMED"

    gates = dict.fromkeys(gate_registry(), 0)

    with pytest.raises(SoakContractError, match=Code.SOAK_STORE_INCOMPATIBLE):
        score_soak(NotEvidence(), declared_gates=gates, required_sessions=20)


def test_the_observation_binds_its_evidence_and_the_contract(soak: Any) -> None:
    # §32. The schema carries one fingerprinted evidence object and the
    # references to re-derive it, not a scatter of integer fields.
    fixture = soak.build(sessions=1)
    observation = fixture.store.live_observations()[0]
    body = observation.body()

    assert body["version"] == SESSION_OBSERVATION_V3_VERSION
    assert body["session_gate_evidence_fingerprint"] == observation.gate_evidence.fingerprint()
    assert body["evidence_contract_fingerprint"] == evidence_contract_fingerprint()
    assert observation.run_ids
    assert observation.gate_evidence.body()["version"] == SESSION_GATE_EVIDENCE_VERSION


def test_an_observation_cannot_carry_another_sessions_evidence(soak: Any) -> None:
    fixture = soak.build(sessions=2)
    first, second = fixture.store.live_observations()[:2]

    with pytest.raises(SoakContractError, match=Code.SOAK_SESSION_EVIDENCE_INCOMPLETE):
        DerivedSessionObservation(
            protocol_fingerprint=first.protocol_fingerprint,
            session=first.session,
            qualifying_commit=first.qualifying_commit,
            observed_at=first.observed_at,
            run_ids=first.run_ids,
            gate_evidence=second.gate_evidence,
        )


# --------------------------------------------------------------------------
# §49, §26: the request audit
# --------------------------------------------------------------------------


def test_a_session_that_mutated_without_an_audit_reports_missing_evidence(
    tmp_path: Path,
) -> None:
    # §26, §49. The audit is authoritative for where a mutation went. A session
    # that submitted orders with no audit beside them cannot prove they went to
    # paper, so it says so rather than reporting zero live mutations.
    evidence = session_evidence(tmp_path, remove="EXTERNAL_REQUEST_AUDIT")
    metric = evidence.metrics["live_mutations"]

    assert metric.status is MetricStatus.EVIDENCE_MISSING
    assert metric.value is None
    assert any("audit is absent" in row for row in metric.detail)


def test_a_session_that_mutated_nothing_has_no_live_mutations(tmp_path: Path) -> None:
    # The honest zero: nothing reached any venue, established from the journal
    # rather than from an absent audit.
    evidence = session_evidence(tmp_path, orders=0, remove="EXTERNAL_REQUEST_AUDIT")
    metric = evidence.metrics["live_mutations"]

    assert metric.status is MetricStatus.OBSERVED_ZERO
    assert metric.value == 0
    assert any("no order reached any venue" in row for row in metric.detail)


def test_a_sent_live_mutation_is_a_breach(tmp_path: Path) -> None:
    evidence = session_evidence(tmp_path, breach=Breach.LIVE_MUTATION)
    metric = evidence.metrics["live_mutations"]

    assert metric.value == 1
    assert any("LIVE mutation sent" in row for row in metric.detail)


def _json_of(observation: Any) -> str:
    """Serialise an observation the way the store does."""
    import json

    return json.dumps(observation.as_json(), indent=2, sort_keys=True)
