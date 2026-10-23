"""P2.4R — the machinery that must exist before a soak is armed.

The defect this repairs is specific and worth stating, because the tests are
shaped by it: `PAPER_SHADOW_SOAK_PROTOCOL_V2` declared twenty-two promotion and
safety gates, was frozen, tagged and armed against a real paper account, and the
qualifying commit contained no code able to evaluate a single one. The protocol
was unscoreable from the moment it was sealed. Nothing objected, because a gate
declared as a string in JSON looks exactly like a gate that works.

So the decisive test here is not that a clean soak scores A. It is
`test_the_preflight_would_have_refused_to_arm_v2` — proof that this machinery
catches the thing that actually happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from stock_screener.execution import fingerprint_of
from stock_screener.paper_operations import (
    PAPER_OPERATIONS_ENGINE_VERSION,
    operations_policy,
)
from stock_screener.soak_verification import (
    REQUIRED_DRILL_TYPES,
    Breach,
    Code,
    DrillRecordV2,
    DrillStoreV2,
    DrillTypeV2,
    GateBound,
    GateClass,
    SoakContractError,
    SoakVerdictV2,
    admissible_drills,
    gate_registry,
    registry_fingerprint,
    require_bijection,
    run_soak_preflight,
    score_soak,
)

pytestmark = pytest.mark.integration

PROTOCOL = "p" * 64
COMMIT = "c" * 40
ENGINE = PAPER_OPERATIONS_ENGINE_VERSION
POLICY = fingerprint_of(operations_policy())


def declared_gates(**overrides: int) -> dict[str, int]:
    """Every gate the registry knows, with sensible protocol thresholds."""
    gates: dict[str, int] = {}
    for gate_id, definition in gate_registry().items():
        if (
            definition.gate_class is GateClass.ZERO_TOLERANCE
            or definition.bound is GateBound.AT_MOST
        ):
            gates[gate_id] = 0
        elif gate_id == "qualifying_sessions_observed":
            gates[gate_id] = 20
        elif gate_id == "required_drills_passed":
            gates[gate_id] = len(REQUIRED_DRILL_TYPES)
        else:
            gates[gate_id] = 1
    gates.update(overrides)
    return gates


def drill(drill_type: DrillTypeV2, **overrides: Any) -> DrillRecordV2:
    """One fully bound, passing drill record."""
    fields: dict[str, Any] = {
        "drill_type": drill_type,
        "protocol_fingerprint": PROTOCOL,
        "qualifying_commit": COMMIT,
        "operations_engine_version": ENGINE,
        "operations_policy_fingerprint": POLICY,
        "performed_at": "2026-10-10T12:00:00+00:00",
        "pre_state_fingerprint": "before",
        "action": "injected fault",
        "expected": "refused",
        "observed": "refused",
        "post_state_fingerprint": "after",
        "passed": True,
    }
    fields.update(overrides)
    return DrillRecordV2(**fields)


def all_drills() -> tuple[DrillRecordV2, ...]:
    """A passing bound record for each required type."""
    return tuple(drill(row) for row in REQUIRED_DRILL_TYPES)


@dataclass
class _FixedClock:
    """An operational clock pinned to one instant."""

    moment: str

    def iso(self) -> str:
        """The instant."""
        return self.moment

    def now(self) -> Any:
        """The instant as an aware datetime."""
        from stock_screener.paper_operations.clock import parse_instant

        return parse_instant(self.moment)


# --------------------------------------------------------------------------
# The decisive test: would this have caught V2?
# --------------------------------------------------------------------------


def test_the_preflight_would_have_refused_to_arm_v2() -> None:
    # V2's own declared gates, taken from the frozen protocol class rather than
    # the live operations root — the class is what was armed, and reading it
    # keeps the test independent of any machine's filesystem.
    #
    # Four of the names it declares have no evaluator, which is exactly why the
    # protocol could never be scored and exactly what this refuses.
    from stock_screener.paper_runtime.soak_v2 import PaperShadowSoakProtocolV2

    frozen = PaperShadowSoakProtocolV2(
        protocol_id="P2_4_V2_SESSION_SOAK",
        qualifying_commit=COMMIT,
        runtime_binding_fingerprint="rb",
        account_binding_fingerprint="acct",
        strategy_fingerprint="sf",
        strategy_authorization_fingerprint="sa",
        risk_policy_fingerprint="rp",
        operations_policy_fingerprint="op",
        calendar_fingerprint="cal",
        schedule_fingerprint="sch",
        score_availability_fingerprint="sav",
        execution_timing_fingerprint="et",
        accounting_generation="ag",
        execution_journal_generation="eg",
    )
    gates = dict(frozen.promotion_gates["zero_tolerance"])
    gates.update(
        {
            key: (value if isinstance(value, int) else 1)
            for key, value in frozen.promotion_gates["required_not_zero_tolerance"].items()
        }
    )

    report = run_soak_preflight(
        protocol_fingerprint=frozen.fingerprint(),
        qualifying_commit=frozen.qualifying_commit,
        declared_gates=gates,
        required_sessions=20,
        required_drill_types=[str(row) for row in REQUIRED_DRILL_TYPES],
        operations_engine_version=ENGINE,
        operations_policy_fingerprint=POLICY,
    )

    assert report.passed is False
    assert any("SOAK_GATE_WITHOUT_EVALUATOR" in row for row in report.failures)


# --------------------------------------------------------------------------
# §6, §7: the gate registry and its bijection
# --------------------------------------------------------------------------


def test_no_gate_exists_only_as_a_string() -> None:
    # The original form of this test asserted every gate carried a callable
    # evaluator. It now asserts the stronger thing: every gate is bound to a
    # versioned derivation over named authoritative sources. A callable was
    # never enough — fourteen of them were lambdas reading fields no record
    # carried.
    from stock_screener.soak_verification import evidence_contract, known_source_schema

    contract = evidence_contract()

    for gate_id, definition in gate_registry().items():
        assert definition.gate_id == gate_id
        assert definition.evaluator_id
        assert definition.rule
        binding = contract.bindings[gate_id]
        assert binding.derivation_id
        assert binding.derivation_version
        assert all(known_source_schema(row) for row in binding.required_schemas), gate_id


def test_a_declared_gate_with_no_evaluator_is_refused() -> None:
    with pytest.raises(SoakContractError, match=Code.SOAK_GATE_WITHOUT_EVALUATOR):
        require_bijection({**declared_gates(), "invented_gate": 0})


def test_a_known_check_the_protocol_omits_is_refused() -> None:
    # The other direction, and the one easier to miss: a check the scorer can
    # perform but the protocol never declared is a safety check switched off.
    gates = declared_gates()
    del gates["kill_bypasses"]

    with pytest.raises(SoakContractError, match=Code.SOAK_EVALUATOR_UNDECLARED):
        require_bijection(gates)


def test_the_registry_has_a_stable_fingerprint() -> None:
    assert len(registry_fingerprint()) == 64
    assert registry_fingerprint() == registry_fingerprint()


def test_every_zero_tolerance_gate_is_a_ceiling() -> None:
    for definition in gate_registry().values():
        if definition.gate_class is GateClass.ZERO_TOLERANCE:
            assert definition.bound is GateBound.AT_MOST, definition.gate_id


def test_a_promotion_gate_may_be_a_ceiling_too() -> None:
    # "No unresolved CRITICAL incident" is an at-most rule that is not a
    # breach. The first draft faked this with negative counts.
    definition = gate_registry()["unresolved_critical_incidents"]

    assert definition.gate_class is GateClass.PROMOTION_REQUIRED
    assert definition.bound is GateBound.AT_MOST


# --------------------------------------------------------------------------
# §9-§14, §61: the four verdicts
# --------------------------------------------------------------------------


def test_an_empty_soak_is_in_progress(soak: Any) -> None:
    result = soak.build_and_score(sessions=0, required_sessions=20)

    assert result.verdict == SoakVerdictV2.IN_PROGRESS
    assert result.counted_sessions == 0
    assert result.remaining_sessions == 20


def test_nineteen_clean_sessions_are_in_progress(soak: Any) -> None:
    result = soak.build_and_score(sessions=19, required_sessions=20)

    assert result.verdict == SoakVerdictV2.IN_PROGRESS
    assert result.counted_sessions == 19


def test_twenty_clean_sessions_with_a_cycle_and_drills_pass(soak: Any) -> None:
    # Twenty sessions of real operational records — journal entries, request
    # audit, reconciliation reports, decision records and a verified execution
    # journal — derived through the evidence contract and scored.
    result = soak.build_and_score(sessions=20)

    assert result.verdict == SoakVerdictV2.PASSED
    assert result.completed_cycles >= 1
    assert result.breaches == ()
    assert result.outstanding == ()
    assert result.unmeasured == ()


def test_a_pass_is_built_on_derived_metrics_not_declared_ones(
    soak: Any,
) -> None:
    # Every gate that contributed to the A came from a derivation over records,
    # and each one names the sources behind it.
    fixture = soak.build(sessions=20)
    result = soak.score(fixture)

    assert result.verdict == SoakVerdictV2.PASSED
    for gate_id in dict(fixture.protocol.gate_thresholds):
        metric = fixture.evidence.metric_for(gate_id)
        assert metric.status.scoreable, gate_id
        assert metric.derivation_id, gate_id
        assert metric.derivation_version, gate_id


def test_missing_one_drill_blocks_the_pass(soak: Any) -> None:
    result = soak.build_and_score(sessions=20, drop_drill=str(REQUIRED_DRILL_TYPES[-1]))

    assert result.verdict == SoakVerdictV2.IN_PROGRESS
    assert "required_drills_passed" in [row.gate_id for row in result.outstanding]


def test_a_failed_drill_does_not_count_as_passed(soak: Any) -> None:
    result = soak.build_and_score(sessions=20, failed_drill=str(REQUIRED_DRILL_TYPES[-1]))

    assert result.verdict == SoakVerdictV2.IN_PROGRESS
    assert "required_drills_passed" in [row.gate_id for row in result.outstanding]


def test_an_unresolved_blocking_incident_blocks_the_pass(soak: Any) -> None:
    # The incident is written to the real incident store, so the gate reads
    # incident history rather than a count somebody passed in.
    from stock_screener.paper_operations import IncidentSeverity, IncidentStore

    fixture = soak.build(sessions=20)
    clock = _FixedClock("2026-11-20T15:00:00+00:00")
    IncidentStore(fixture.root).record(
        clock=clock,
        reason_code="BLOCKED_RECONCILIATION_BREAK",
        severity=IncidentSeverity.BLOCKING,
        subject="STARTUP_RECONCILIATION_CLEAN",
        account_fingerprint="a" * 64,
        detail="an unexplained remote position",
        run_id="opsrun-20261120",
        local_evidence_fingerprint="e" * 64,
    )
    result = soak.score(fixture)

    assert result.verdict == SoakVerdictV2.IN_PROGRESS
    assert "unresolved_blocking_incidents" in [row.gate_id for row in result.outstanding]


@pytest.mark.parametrize(
    ("breach", "gate"),
    [
        (Breach.TWO_REMOTE_ORDERS, "duplicate_remote_orders"),
        (Breach.DOUBLE_ADMITTED_FILL, "duplicate_admitted_fills"),
        (Breach.TWO_ACCOUNTING_EVENTS, "duplicate_accounting_events"),
        (Breach.TWO_EXECUTING_RUNS, "duplicate_scheduled_runs_causing_execution"),
        (Breach.STALE_INPUT_EXECUTION, "stale_data_executions"),
        (Breach.MISSING_INPUT_EXECUTION, "missing_data_executions"),
        (Breach.BACKFILLED_MISSED_DECISION, "missed_run_backfills"),
        (Breach.EXECUTED_AFTER_BREAK, "ignored_reconciliation_breaks"),
        (Breach.EXECUTED_WITH_REMOTE_UNKNOWN, "ignored_remote_state_unknown"),
        (Breach.COMPLETED_WITHOUT_RECONCILIATION, "completed_sessions_reconcile"),
        (Breach.MUTATION_WITHOUT_GATE_CHECK, "kill_bypasses"),
        (Breach.CANCELLED_UNKNOWN_ORDER, "unknown_remote_order_auto_cancels"),
        (Breach.SECOND_OPENING_BALANCE, "automatic_accounting_repairs"),
        (Breach.RERANKED_EXECUTION, "reranked_executions"),
        (Breach.RISK_EXPANSION, "risk_authority_expansions"),
        (Breach.LIVE_MUTATION, "live_mutations"),
    ],
)
def test_one_safety_breach_fails_the_whole_soak(soak: Any, breach: str, gate: str) -> None:
    # §10. Nineteen clean sessions and one breach is FAIL, never 95%.
    #
    # The breach is created by writing the records a breach leaves behind — two
    # remote orders under one client order id, a mutation with no permitting
    # gate check, a second opening balance — and the derivation has to notice.
    # No metric is set anywhere in this test.
    result = soak.build_and_score(sessions=20, breach=breach)

    assert result.verdict == SoakVerdictV2.FAILED
    assert gate in [row.gate_id for row in result.breaches]


def test_a_halted_soak_is_c_even_when_the_evidence_is_clean(soak: Any) -> None:
    # §12. A halted protocol can never later become A: the defect that halted
    # it is a statement about the semantics the evidence was gathered under.
    result = soak.build_and_score(sessions=20, state="HALTED")

    assert result.verdict == SoakVerdictV2.HALTED


def test_a_breach_outranks_a_halt(soak: Any) -> None:
    # A halted soak that also breached safety reports the breach. The halt is
    # procedural; the breach is the thing somebody must look at.
    result = soak.build_and_score(
        sessions=20, breach=Breach.MUTATION_WITHOUT_GATE_CHECK, state="HALTED"
    )

    assert result.verdict == SoakVerdictV2.FAILED


def test_the_score_has_no_pass_rate(soak: Any) -> None:
    result = soak.build_and_score(sessions=19, required_sessions=20)

    assert result.body()["pass_rate"] is None
    assert result.body()["performance_measures_read"] == []


def test_no_threshold_can_be_passed_to_the_scorer() -> None:
    import inspect

    names = set(inspect.signature(score_soak).parameters)

    assert names == {"evidence", "declared_gates", "required_sessions"}


# --------------------------------------------------------------------------
# §24-§34, §62: drill evidence that can prove what it tested
# --------------------------------------------------------------------------


def test_a_fully_bound_drill_set_is_admissible() -> None:
    admitted, rejected = admissible_drills(
        all_drills(),
        protocol_fingerprint=PROTOCOL,
        qualifying_commit=COMMIT,
        operations_engine_version=ENGINE,
        operations_policy_fingerprint=POLICY,
    )

    assert len(admitted) == len(REQUIRED_DRILL_TYPES)
    assert rejected == ()


@pytest.mark.parametrize(
    ("field_name", "value", "label"),
    [
        ("protocol_fingerprint", "x" * 64, "protocol"),
        ("qualifying_commit", "x" * 40, "qualifying commit"),
        ("operations_engine_version", "SOMETHING_ELSE", "operations engine"),
        ("operations_policy_fingerprint", "x" * 64, "operations policy"),
    ],
)
def test_a_drill_bound_to_another_system_is_inadmissible(
    field_name: str, value: str, label: str
) -> None:
    # §25-§28. A drill for one protocol, commit, engine or policy must never
    # certify another.
    wrong = drill(DrillTypeV2.CRASH_RECOVERY, **{field_name: value})

    admitted, rejected = admissible_drills(
        (wrong,),
        protocol_fingerprint=PROTOCOL,
        qualifying_commit=COMMIT,
        operations_engine_version=ENGINE,
        operations_policy_fingerprint=POLICY,
    )

    assert admitted == ()
    assert len(rejected) == 1
    assert label in rejected[0]


def test_a_drill_record_cannot_omit_its_bindings() -> None:
    # Refused at construction, so an unbound record can never reach a store
    # and later be mistaken for evidence.
    with pytest.raises(SoakContractError, match=Code.SOAK_DRILL_INADMISSIBLE):
        drill(DrillTypeV2.STALE_INPUT, protocol_fingerprint="   ")


def test_the_old_v1_drill_records_cannot_certify_anything() -> None:
    # §34. Their complete field set names no protocol, commit or engine, so
    # reading one as V2 evidence is refused rather than retrofitted.
    from stock_screener.soak_verification.drills_v2 import _from_body

    v1_shaped = {
        "drill_type": "CRASH_RECOVERY",
        "passed": True,
        "injected_fault": True,
        "performed_at": "2026-10-07T22:59:51.383937+00:00",
        "drill_fingerprint": "2da3774295470bf0",
    }

    with pytest.raises(SoakContractError, match=Code.SOAK_DRILL_INADMISSIBLE):
        _from_body(v1_shaped)


def test_recording_the_identical_drill_twice_is_idempotent(tmp_path: Path) -> None:
    store = DrillStoreV2(tmp_path / "drills")
    record = drill(
        DrillTypeV2.KILL_RESTART
        if hasattr(DrillTypeV2, "KILL_RESTART")
        else DrillTypeV2.PERSISTENT_KILL_RESTART
    )

    store.record(record)
    store.record(record)

    assert len(store.all()) == 1


def test_a_conflicting_drill_result_is_refused(tmp_path: Path) -> None:
    # §32. Two incompatible claims about one system means neither is evidence.
    store = DrillStoreV2(tmp_path / "drills")
    store.record(drill(DrillTypeV2.PROVIDER_UNAVAILABLE, passed=True))

    with pytest.raises(SoakContractError, match=Code.SOAK_DRILL_CONFLICT):
        store.record(drill(DrillTypeV2.PROVIDER_UNAVAILABLE, passed=False))


def test_a_drill_round_trips_through_its_store(tmp_path: Path) -> None:
    store = DrillStoreV2(tmp_path / "drills")
    for row in all_drills():
        store.record(row)

    reloaded = store.all()

    assert {row.drill_type for row in reloaded} == set(REQUIRED_DRILL_TYPES)
    assert all(row.protocol_fingerprint == PROTOCOL for row in reloaded)


def test_a_drill_declares_whether_its_fault_was_injected() -> None:
    # §30. An injected outage and a real one are different evidence.
    record = drill(DrillTypeV2.REMOTE_STATE_UNKNOWN)

    assert record.injected_fault is True
    assert record.body()["provider_mode"] == "INJECTED_FAULT"


def test_a_drill_identity_ignores_when_it_was_performed() -> None:
    # Otherwise an identical re-run would be a different drill and the store
    # would accumulate duplicates the scorer might count.
    one = drill(DrillTypeV2.DUPLICATE_SCHEDULER, performed_at="2026-01-01T00:00:00+00:00")
    two = drill(DrillTypeV2.DUPLICATE_SCHEDULER, performed_at="2026-06-01T00:00:00+00:00")

    assert one.drill_id == two.drill_id
    assert one.fingerprint() == two.fingerprint()


# --------------------------------------------------------------------------
# §35-§47, §63: the pre-arming self-check
# --------------------------------------------------------------------------


def preflight(**overrides: Any) -> Any:
    """Run the self-check over a complete protocol.

    The protocol object is passed, not just its fingerprint: reachability is
    now established by writing real operational records and deriving from them,
    which needs the protocol a session would be bound to. There is no longer a
    path that asserts a verdict from an injected metric.
    """
    from stock_screener.soak_verification import gate_registry

    bound = _protocol_for_preflight(**overrides.pop("protocol_overrides", {}))
    settings: dict[str, Any] = {
        "protocol": bound,
        "protocol_fingerprint": bound.fingerprint(),
        "qualifying_commit": bound.qualifying_commit,
        "declared_gates": dict(bound.gate_thresholds),
        "required_sessions": bound.required_sessions,
        "required_drill_types": [str(row) for row in bound.required_drill_types],
        "operations_engine_version": ENGINE,
        "operations_policy_fingerprint": bound.operations_policy_fingerprint,
    }
    settings.update(overrides)
    assert set(gate_registry()) == set(settings["declared_gates"]) or overrides
    return run_soak_preflight(**settings)


def _protocol_for_preflight(**overrides: Any) -> Any:
    """A complete protocol for the self-check, declaring every canonical gate."""
    from stock_screener.soak_verification import PaperShadowSoakProtocol, gate_registry

    thresholds = declared_gates()
    thresholds.update(overrides.pop("gate_thresholds", {}))
    settings: dict[str, Any] = {
        "protocol_version": "P2_4R_PREFLIGHT_V1",
        "protocol_id": "P2_4R_PREFLIGHT",
        "qualifying_commit": COMMIT,
        "declared_gate_ids": tuple(sorted(gate_registry())),
        "gate_thresholds": thresholds,
        "required_sessions": 20,
        "required_drill_types": tuple(REQUIRED_DRILL_TYPES),
        "observation_schema_version": "PAPER_SHADOW_SESSION_OBSERVATION_V3",
        "runtime_binding_fingerprint": "r" * 64,
        "paper_account_fingerprint": "a" * 64,
        "strategy_id": "ALPHATRADER_COMPOUNDER_MONTHLY",
        "strategy_version": "V1",
        "strategy_fingerprint": "s" * 64,
        "strategy_authorization_fingerprint": "g" * 64,
        "risk_policy_fingerprint": "k" * 64,
        "operations_policy_fingerprint": POLICY,
        "calendar_version": "PAPER_OPERATIONS_CALENDAR_V1",
        "calendar_fingerprint": "l" * 64,
        "schedule_version": "ALPHATRADER_PAPER_V1_1",
        "schedule_fingerprint": "h" * 64,
        "score_availability_profile_fingerprint": "v" * 64,
        "execution_timing_profile_fingerprint": "e" * 64,
    }
    settings.update(overrides)
    return PaperShadowSoakProtocol(**settings)


def test_a_complete_protocol_passes_the_self_check() -> None:
    report = preflight()

    assert report.passed is True
    assert report.failures == ()
    report.require()


def test_all_four_verdicts_are_proven_reachable() -> None:
    # §40. A protocol whose A is mechanically impossible must not arm — it
    # would collect twenty sessions toward a conclusion nothing can draw.
    report = preflight()

    # Five now, not four: the evidence-incomplete verdict is C-class and must
    # be provably reachable too, or a protocol could be armed whose gates can
    # never report that their sources went missing.
    assert set(report.reachable_verdicts) == {
        SoakVerdictV2.IN_PROGRESS,
        SoakVerdictV2.PASSED,
        SoakVerdictV2.FAILED,
        SoakVerdictV2.HALTED,
        SoakVerdictV2.EVIDENCE_INCOMPLETE,
    }


def test_a_missing_evaluator_refuses_activation() -> None:
    report = preflight(declared_gates={**declared_gates(), "invented": 0})

    assert report.passed is False
    with pytest.raises(SoakContractError, match=Code.SOAK_PREFLIGHT_FAILED):
        report.require()


def test_an_undeclared_known_check_refuses_activation() -> None:
    gates = declared_gates()
    del gates["live_mutations"]

    report = preflight(declared_gates=gates)

    assert report.passed is False


def test_an_unsupported_drill_type_refuses_activation() -> None:
    report = preflight(required_drill_types=["TELEPORT_FAILURE"])

    assert report.passed is False
    assert any("unsupported drill types" in row for row in report.failures)


def test_an_unreachable_pass_refuses_activation() -> None:
    # A protocol requiring more sessions than its own fixtures can produce has
    # no reachable A. Expressed here as an impossible drill requirement.
    report = preflight(declared_gates={**declared_gates(), "required_drills_passed": 99})

    assert report.passed is False
    assert any("not mechanically reachable" in row for row in report.failures)


def test_the_self_check_has_no_escape_hatch() -> None:
    # §46.
    import inspect

    source = inspect.getsource(run_soak_preflight)
    body = preflight().body()

    assert body["escape_hatch"].startswith("NONE")
    for forbidden in ("force", "skip", "ignore_gate", "override"):
        assert forbidden not in source.lower()


def test_the_self_check_states_what_it_cannot_establish() -> None:
    # A reachability proof over synthetic evidence is not a proof that the real
    # soak will behave, and the artefact says so rather than implying more.
    report = preflight()

    assert any("synthetic" in row for row in report.limitations)
    assert any("measures what its name" in row for row in report.limitations)


def test_the_preflight_artefact_binds_its_scorer_and_registry() -> None:
    report = preflight()
    artefact = report.as_json()

    assert artefact["scorer_version"] == "PAPER_SHADOW_SOAK_SCORER_V1"
    assert artefact["gate_registry_fingerprint"] == registry_fingerprint()
    assert artefact["drill_evidence_version"] == "PAPER_OPERATIONS_DRILL_EVIDENCE_V2"
    assert len(artefact["preflight_fingerprint"]) == 64


# --------------------------------------------------------------------------
# §65: what this package may not contain
# --------------------------------------------------------------------------


def test_the_verification_package_evaluates_evidence_and_nothing_else() -> None:
    import stock_screener.soak_verification as package

    root = Path(next(iter(package.__path__)))
    source = "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py")))

    # Precise forms. The source legitimately *names* the components that write
    # the records it reads — `writer="AlpacaPaperAdapter.submit"` in the source
    # schema registry is documentation of provenance, not a dependency. What
    # must not appear is an import of, or a call into, any of them.
    for forbidden in (
        "import httpx",
        "from stock_screener.execution.external import Alpaca",
        "AlpacaPaperHttpTransport(",
        "AlpacaPaperAdapter(",
        "submit_order(",
        ".post(",
        ".delete(",
        "admit_fill(",
        "plan_execution(",
        "RiskPolicy(",
        "CompounderMonthlyStrategy(",
        "evaluate_session(",
    ):
        assert forbidden not in source, forbidden


def test_the_verification_package_references_no_credential() -> None:
    import stock_screener.soak_verification as package

    root = Path(next(iter(package.__path__)))
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py"))
    ).lower()

    for forbidden in ("api_key", "apca", "secret_key", "bearer ", "os.environ"):
        assert forbidden not in source, forbidden
