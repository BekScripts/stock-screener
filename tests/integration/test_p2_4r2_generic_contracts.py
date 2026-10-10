"""P2.4R2 — gates as data, so a protocol that cannot be scored cannot exist.

Three protocol versions failed the same way. V1 and V2 each baked their gate set
into a hardcoded property, which made the set a property of the class rather
than of the instance. V2 declared four gates no evaluator covered and was frozen
and armed anyway; V3 could not be constructed at all, because expressing a
different gate set meant writing another class.

The test this file exists for is `test_a_protocol_gate_set_equals_the_registry`
and its two reverse-direction partners. They are the assertions whose absence
let an unscoreable protocol be sealed, and
`test_the_historical_v2_gate_set_is_detectably_wrong` proves the specific prior
defect is now caught.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.paper_operations import PAPER_OPERATIONS_ENGINE_VERSION
from stock_screener.soak_verification import (
    REQUIRED_DRILL_TYPES,
    Code,
    DrillRecordV2,
    DrillTypeV2,
    PaperShadowSoakActivation,
    PaperShadowSoakProtocol,
    SoakContractError,
    SoakVerdictV2,
    VersionedSoakStore,
    canonical_gate_ids,
    canonical_thresholds,
    gate_registry,
    preflight_protocol,
    registry_fingerprint,
    score_soak,
    validate_namespace,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

COMMIT = "f05239b9948d7a1149f476084962fd965aad7786"
V3 = "PAPER_SHADOW_SOAK_PROTOCOL_V3"
ACTIVATION_V3 = "PAPER_SHADOW_SOAK_ACTIVATION_V3"
POLICY = "op" * 32

BINDINGS: dict[str, Any] = {
    "runtime_binding_fingerprint": "57852f2d" * 8,
    "paper_account_fingerprint": "a894a8da" * 8,
    "strategy_id": "ALPHATRADER_COMPOUNDER_MONTHLY",
    "strategy_version": "ALPHATRADER_COMPOUNDER_MONTHLY_V1",
    "strategy_fingerprint": "5aa71b40" * 8,
    "strategy_authorization_fingerprint": "b" * 64,
    "risk_policy_fingerprint": "rp" * 32,
    "operations_policy_fingerprint": POLICY,
    "calendar_version": "US_EQUITY_OPERATIONAL_CALENDAR_2026Q4_2027Q1_V1",
    "calendar_fingerprint": "582eebd1" * 8,
    "schedule_version": "PAPER_OPERATIONS_SCHEDULE_V1_1",
    "schedule_fingerprint": "sch" * 20,
    "score_availability_profile_fingerprint": "sav" * 20,
    "execution_timing_profile_fingerprint": "et" * 32,
}


def protocol(**overrides: Any) -> PaperShadowSoakProtocol:
    """A V3-shaped protocol declaring exactly the canonical registry."""
    settings: dict[str, Any] = {
        "protocol_version": V3,
        "protocol_id": "P2_4_V3",
        "qualifying_commit": COMMIT,
        "required_sessions": 20,
        "required_drill_types": [str(row) for row in REQUIRED_DRILL_TYPES],
        **BINDINGS,
    }
    settings.update(overrides)
    return PaperShadowSoakProtocol.canonical(**settings)


def activation(frozen: PaperShadowSoakProtocol, **overrides: Any) -> PaperShadowSoakActivation:
    """A matching V3 activation."""
    settings: dict[str, Any] = {
        "activation_version": ACTIVATION_V3,
        "protocol_version": frozen.protocol_version,
        "protocol_fingerprint": frozen.fingerprint(),
        "qualifying_commit": frozen.qualifying_commit,
        "activated_at": "2026-10-10T06:00:00+00:00",
        "required_sessions": frozen.required_sessions,
        "window": tuple(f"S{index:03d}" for index in range(20)),
        "decision_sessions": ("S000",),
        "execution_sessions": ("S001",),
    }
    settings.update(overrides)
    return PaperShadowSoakActivation(**settings)


# --------------------------------------------------------------------------
# §9, §43: the mandatory regression
# --------------------------------------------------------------------------


def test_a_protocol_gate_set_equals_the_registry() -> None:
    # This is the assertion whose absence let V2 be sealed unscoreable.
    frozen = protocol()

    assert set(frozen.declared_gate_ids) == set(gate_registry())


def test_the_registry_has_the_expected_gate_count() -> None:
    # Bound to the registry fingerprint rather than the bare number, so a
    # legitimate future change to the canonical set has to be acknowledged
    # instead of silently accepted.
    assert len(gate_registry()) == 24
    assert registry_fingerprint() == protocol().gate_registry_fingerprint


def test_removing_one_gate_makes_the_protocol_unconstructable() -> None:
    frozen = protocol()

    with pytest.raises(SoakContractError, match=Code.SOAK_GATE_REGISTRY_MISMATCH):
        PaperShadowSoakProtocol(
            protocol_version=V3,
            protocol_id="x",
            qualifying_commit=COMMIT,
            required_sessions=20,
            declared_gate_ids=canonical_gate_ids()[1:],
            gate_thresholds=dict(frozen.gate_thresholds),
            **BINDINGS,
        )


def test_adding_an_unknown_gate_makes_the_protocol_unconstructable() -> None:
    frozen = protocol()

    with pytest.raises(SoakContractError, match=Code.SOAK_GATE_REGISTRY_MISMATCH):
        PaperShadowSoakProtocol(
            protocol_version=V3,
            protocol_id="x",
            qualifying_commit=COMMIT,
            required_sessions=20,
            declared_gate_ids=(*canonical_gate_ids(), "invented_gate"),
            gate_thresholds={**frozen.gate_thresholds, "invented_gate": 0},
            **BINDINGS,
        )


def test_a_duplicate_gate_is_refused_not_deduplicated() -> None:
    frozen = protocol()

    with pytest.raises(SoakContractError, match=Code.SOAK_GATE_REGISTRY_MISMATCH):
        PaperShadowSoakProtocol(
            protocol_version=V3,
            protocol_id="x",
            qualifying_commit=COMMIT,
            required_sessions=20,
            declared_gate_ids=(*canonical_gate_ids(), canonical_gate_ids()[0]),
            gate_thresholds=dict(frozen.gate_thresholds),
            **BINDINGS,
        )


def test_a_gate_without_a_threshold_is_refused() -> None:
    with pytest.raises(SoakContractError, match=Code.SOAK_GATE_REGISTRY_MISMATCH):
        PaperShadowSoakProtocol(
            protocol_version=V3,
            protocol_id="x",
            qualifying_commit=COMMIT,
            required_sessions=20,
            declared_gate_ids=canonical_gate_ids(),
            gate_thresholds={},
            **BINDINGS,
        )


def test_a_protocol_must_state_its_own_version() -> None:
    # §21. A generic contract with a default version stamps the wrong one.
    with pytest.raises(SoakContractError, match=Code.SOAK_GATE_REGISTRY_MISMATCH):
        protocol(protocol_version="   ")


def test_the_historical_v2_gate_set_is_detectably_wrong() -> None:
    # §46. The exact prior defect: V2's 22 declared gates, four of which name
    # no evaluator and six registry evaluators of which it declares none.
    from stock_screener.paper_runtime.soak_v2 import PaperShadowSoakProtocolV2

    v2 = PaperShadowSoakProtocolV2(
        protocol_id="x",
        qualifying_commit=COMMIT,
        runtime_binding_fingerprint="r",
        account_binding_fingerprint="a",
        strategy_fingerprint="s",
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
    declared = set(v2.promotion_gates["zero_tolerance"]) | set(
        v2.promotion_gates["required_not_zero_tolerance"]
    )

    assert declared != set(gate_registry())
    assert sorted(declared - set(gate_registry())) == [
        "execution_eligible_decisions_accounted_for",
        "missing_data_trades",
        "pending_executions_at_completion",
        "stale_data_trades",
    ]
    # And a protocol declaring that set cannot be constructed at all.
    with pytest.raises(SoakContractError, match=Code.SOAK_GATE_REGISTRY_MISMATCH):
        PaperShadowSoakProtocol(
            protocol_version=V3,
            protocol_id="x",
            qualifying_commit=COMMIT,
            required_sessions=20,
            declared_gate_ids=tuple(sorted(declared)),
            gate_thresholds=dict.fromkeys(declared, 0),
            **BINDINGS,
        )


# --------------------------------------------------------------------------
# §6, §11, §32: gates are data, and order is not identity
# --------------------------------------------------------------------------


def test_the_gate_set_is_a_data_field_not_a_property() -> None:
    assert "declared_gate_ids" in PaperShadowSoakProtocol.__dataclass_fields__
    assert "gate_thresholds" in PaperShadowSoakProtocol.__dataclass_fields__


def test_the_gate_set_participates_in_the_fingerprint() -> None:
    frozen = protocol()

    assert "declared_gate_ids" in frozen.body()
    assert frozen.body()["declared_gate_ids"] == sorted(canonical_gate_ids())


def test_the_input_order_of_gates_does_not_change_the_fingerprint() -> None:
    frozen = protocol()
    shuffled = list(frozen.declared_gate_ids)
    random.Random(7).shuffle(shuffled)  # noqa: S311 - shuffling a list, not keying

    other = PaperShadowSoakProtocol(
        protocol_version=V3,
        protocol_id=frozen.protocol_id,
        qualifying_commit=COMMIT,
        required_sessions=20,
        declared_gate_ids=tuple(shuffled),
        gate_thresholds=dict(frozen.gate_thresholds),
        required_drill_types=tuple(reversed(frozen.required_drill_types)),
        **BINDINGS,
    )

    assert other.fingerprint() == frozen.fingerprint()


def test_the_canonical_thresholds_cover_every_registry_gate() -> None:
    thresholds = canonical_thresholds(required_sessions=20, required_drills=6)

    assert set(thresholds) == set(gate_registry())


def test_the_protocol_binds_the_scorer() -> None:
    # §12. A different scorer must not evaluate it silently.
    assert protocol().scorer_version == "PAPER_SHADOW_SOAK_SCORER_V1"


def test_observation_and_protocol_schema_versions_are_independent() -> None:
    # §39. A new protocol does not imply a new observation schema, and
    # conflating them is what produced a class per soak.
    frozen = protocol()

    assert frozen.protocol_version == V3
    assert frozen.observation_schema_version == "PAPER_SHADOW_SESSION_OBSERVATION_V2"


def test_a_protocol_round_trips_through_its_stored_form() -> None:
    frozen = protocol()

    reloaded = PaperShadowSoakProtocol.from_body(frozen.as_json())

    assert reloaded.fingerprint() == frozen.fingerprint()


def test_a_historical_protocol_is_never_reinterpreted_as_the_generic_one() -> None:
    with pytest.raises(SoakContractError, match=Code.SOAK_GATE_REGISTRY_MISMATCH):
        PaperShadowSoakProtocol.from_body({"protocol_family": "SOMETHING_ELSE"})


# --------------------------------------------------------------------------
# §13-§19, §47, §48: the generic namespaced store
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "namespace",
    ["", "   ", "../foo", "a/b", "a\\b", "..", "v1", "v2", ".hidden", "-x", "v3 "],
)
def test_an_unsafe_namespace_is_refused(namespace: str) -> None:
    # §15, §48. A namespace becomes a directory under the soak root, so an
    # unchecked one lets a new soak write over a halted protocol's evidence —
    # the only record of why it halted.
    with pytest.raises(SoakContractError, match=Code.SOAK_NAMESPACE_INVALID):
        validate_namespace(namespace)


def test_a_historical_namespace_cannot_be_reused() -> None:
    for reserved in ("v1", "v2"):
        with pytest.raises(SoakContractError, match=Code.SOAK_NAMESPACE_INVALID):
            validate_namespace(reserved)


def test_a_fresh_namespace_is_accepted() -> None:
    assert validate_namespace("v3") == "v3"
    assert validate_namespace("v3-rehearsal") == "v3-rehearsal"


def test_two_namespaces_keep_their_evidence_isolated(tmp_path: Path) -> None:
    # §47. One store implementation, two soaks, no leakage.
    root = tmp_path / ".operations"
    first = VersionedSoakStore(root, "v3-test-a")
    second = VersionedSoakStore(root, "v3-test-b")
    frozen = protocol()
    other = protocol(protocol_id="P2_4_V3_OTHER")

    first.freeze_protocol(frozen)
    second.freeze_protocol(other)

    first_frozen = first.protocol()
    second_frozen = second.protocol()
    assert first_frozen is not None
    assert second_frozen is not None
    assert first_frozen.protocol_id == "P2_4_V3"
    assert second_frozen.protocol_id == "P2_4_V3_OTHER"
    assert first.directory != second.directory


def test_the_store_is_not_typed_to_a_version_class(tmp_path: Path) -> None:
    # §19, §23. freeze_protocol and activate take the generic contracts.
    import inspect

    freeze = inspect.signature(VersionedSoakStore.freeze_protocol)
    activate = inspect.signature(VersionedSoakStore.activate)

    assert freeze.parameters["protocol"].annotation == "PaperShadowSoakProtocol"
    assert activate.parameters["activation"].annotation == "PaperShadowSoakActivation"


def test_freezing_the_same_protocol_twice_is_idempotent(tmp_path: Path) -> None:
    store = VersionedSoakStore(tmp_path / ".operations", "v3")
    frozen = protocol()

    store.freeze_protocol(frozen)
    store.freeze_protocol(frozen)

    reloaded = store.protocol()
    assert reloaded is not None
    assert reloaded.fingerprint() == frozen.fingerprint()


def test_freezing_a_different_protocol_is_refused(tmp_path: Path) -> None:
    store = VersionedSoakStore(tmp_path / ".operations", "v3")
    store.freeze_protocol(protocol())

    with pytest.raises(SoakContractError, match=Code.SOAK_PROTOCOL_ALREADY_FROZEN):
        store.freeze_protocol(protocol(required_sessions=10))


# --------------------------------------------------------------------------
# §20-§24, §49: the generic versioned activation
# --------------------------------------------------------------------------


def test_the_activation_version_is_explicit_data() -> None:
    # §21. No default to the V2 contract.
    assert "activation_version" in PaperShadowSoakActivation.__dataclass_fields__
    with pytest.raises(SoakContractError, match=Code.SOAK_ACTIVATION_VERSION_MISMATCH):
        activation(protocol(), activation_version="  ")


def test_a_matching_activation_is_accepted(tmp_path: Path) -> None:
    store = VersionedSoakStore(tmp_path / ".operations", "v3")
    frozen = protocol()
    store.freeze_protocol(frozen)

    store.activate(activation(frozen))

    recorded = store.activation()
    assert recorded is not None
    assert recorded.activation_version == ACTIVATION_V3
    assert store.state() == "ARMED"


def test_a_v2_activation_version_cannot_activate_a_v3_protocol(tmp_path: Path) -> None:
    # §22, §49.
    store = VersionedSoakStore(tmp_path / ".operations", "v3")
    frozen = protocol()
    store.freeze_protocol(frozen)

    with pytest.raises(SoakContractError, match=Code.SOAK_ACTIVATION_VERSION_MISMATCH):
        store.activate(activation(frozen, protocol_version="PAPER_SHADOW_SOAK_PROTOCOL_V2"))


def test_an_activation_naming_another_protocol_instance_is_refused(tmp_path: Path) -> None:
    store = VersionedSoakStore(tmp_path / ".operations", "v3")
    frozen = protocol()
    store.freeze_protocol(frozen)

    with pytest.raises(SoakContractError, match=Code.SOAK_ACTIVATION_VERSION_MISMATCH):
        store.activate(activation(frozen, protocol_fingerprint="x" * 64))


def test_an_orphan_activation_is_refused(tmp_path: Path) -> None:
    # §24. An activation naming a protocol nobody froze binds nothing.
    store = VersionedSoakStore(tmp_path / ".operations", "v3")

    with pytest.raises(SoakContractError, match=Code.SOAK_ACTIVATION_ORPHANED):
        store.activate(activation(protocol()))


def test_a_second_different_activation_is_refused(tmp_path: Path) -> None:
    # §36. The prospective boundary is set once.
    store = VersionedSoakStore(tmp_path / ".operations", "v3")
    frozen = protocol()
    store.freeze_protocol(frozen)
    store.activate(activation(frozen))

    with pytest.raises(SoakContractError, match=Code.SOAK_ALREADY_ACTIVATED):
        store.activate(activation(frozen, activated_at="2026-10-11T00:00:00+00:00"))


# --------------------------------------------------------------------------
# §18, §28, §30, §50-§54: scorer and preflight over the generic store
# --------------------------------------------------------------------------


def armed(tmp_path: Path) -> tuple[Any, PaperShadowSoakProtocol]:
    """An armed generic store."""
    store = VersionedSoakStore(tmp_path / ".operations", "v3")
    frozen = protocol()
    store.freeze_protocol(frozen)
    store.activate(activation(frozen))
    return store, frozen


def observation(frozen: PaperShadowSoakProtocol, session: str, **kwargs: Any) -> Any:
    """One session observation bound to this protocol and build."""
    from stock_screener.paper_runtime.soak_v2 import PaperShadowSessionObservationV2

    return PaperShadowSessionObservationV2(
        protocol_fingerprint=frozen.fingerprint(),
        session=session,
        qualifying_commit=frozen.qualifying_commit,
        observed_at=f"{session}T21:00:00+00:00",
        **kwargs,
    )


def bound_drill(frozen: PaperShadowSoakProtocol, kind: DrillTypeV2) -> DrillRecordV2:
    """A passing drill bound to this protocol, commit, engine and policy."""
    return DrillRecordV2(
        drill_type=kind,
        protocol_fingerprint=frozen.fingerprint(),
        qualifying_commit=frozen.qualifying_commit,
        operations_engine_version=PAPER_OPERATIONS_ENGINE_VERSION,
        operations_policy_fingerprint=frozen.operations_policy_fingerprint,
        performed_at="2026-10-10T12:00:00+00:00",
        pre_state_fingerprint="before",
        action="injected fault",
        expected="refused",
        observed="refused",
        post_state_fingerprint="after",
        passed=True,
    )


def test_an_empty_generic_store_scores_in_progress(tmp_path: Path) -> None:
    # §18, §50. The exact failure V3 hit: AttributeError instead of a verdict.
    store, frozen = armed(tmp_path)

    result = score_soak(store, declared_gates=dict(frozen.gate_thresholds), required_sessions=20)

    assert result.verdict == SoakVerdictV2.IN_PROGRESS
    assert result.counted_sessions == 0


def test_the_preflight_consumes_the_exact_protocol_and_store(tmp_path: Path) -> None:
    # §26, §28, §29. Not a reduced descriptor: the thing that will be frozen.
    store, frozen = armed(tmp_path)

    report = preflight_protocol(frozen, activation=activation(frozen), store=store)

    names = {name for name, _, _ in report.checks}
    assert "declared_gates_equal_registry" in names
    assert "gate_registry_fingerprint_matches" in names
    assert "activation_contract_matches" in names
    assert "real_store_scores_in_progress" in names
    assert "observation_schema_carries_gate_evidence" in names
    # Every contract check passes; the one that does not is the observation
    # schema, which is a real outstanding gap and not a contract defect.
    for name, ok, detail in report.checks:
        if name != "observation_schema_carries_gate_evidence":
            assert ok, f"{name}: {detail}"


def test_the_preflight_refuses_because_the_observation_schema_is_insufficient(
    tmp_path: Path,
) -> None:
    # The finding this phase uncovered. Fourteen zero-tolerance gates read
    # fields PaperShadowSessionObservationV2 does not carry, so each would
    # return the attribute default — zero — and report itself clean forever.
    # Arming is blocked until the schema can express a breach.
    store, frozen = armed(tmp_path)

    report = preflight_protocol(frozen, activation=activation(frozen), store=store)

    assert report.passed is False
    assert any("report clean forever" in row for row in report.failures)
    with pytest.raises(SoakContractError, match=Code.SOAK_PREFLIGHT_FAILED):
        report.require()


def test_every_session_gate_declares_the_fields_it_reads() -> None:
    # Declared rather than inferred: an evaluator reading an undeclared field
    # is indistinguishable from one reading a field that is always zero, which
    # is exactly how fourteen gates reported themselves clean forever.
    from stock_screener.soak_verification.gates import EvidenceKind

    session_gates = [
        (gate_id, definition)
        for gate_id, definition in gate_registry().items()
        if definition.evidence is EvidenceKind.SESSION_OBSERVATIONS
    ]

    assert session_gates
    for gate_id, definition in session_gates:
        assert definition.evidence_fields, gate_id


def test_the_observation_schema_cannot_currently_express_a_breach() -> None:
    # Stated as a test so the gap is a recorded fact rather than a note.
    from stock_screener.paper_runtime.soak_v2 import PaperShadowSessionObservationV2
    from stock_screener.soak_verification.gates import EvidenceKind

    fields = set(PaperShadowSessionObservationV2.__dataclass_fields__)
    unreadable = [
        gate_id
        for gate_id, definition in gate_registry().items()
        if definition.evidence is EvidenceKind.SESSION_OBSERVATIONS
        and any(row not in fields for row in definition.evidence_fields)
    ]

    assert len(unreadable) == 14


def test_all_four_verdicts_remain_reachable_with_the_generic_types(
    tmp_path: Path,
) -> None:
    store, frozen = armed(tmp_path)

    report = preflight_protocol(frozen, store=store)

    # Reachability itself is proven; the schema check is what blocks arming.
    assert set(report.reachable_verdicts) == {
        SoakVerdictV2.IN_PROGRESS,
        SoakVerdictV2.PASSED,
        SoakVerdictV2.FAILED,
        SoakVerdictV2.HALTED,
    }


def test_a_clean_generic_soak_scores_a_pass(tmp_path: Path) -> None:
    # §51. Twenty sessions, a real decision cycle, six bound drills.
    store, frozen = armed(tmp_path)
    digest = "decision-" + "f" * 56
    store.record_observation(
        observation(frozen, "S000", decision_created=True, decision_fingerprint=digest)
    )
    store.record_observation(observation(frozen, "S001", executed_decision_fingerprint=digest))
    for index in range(2, 20):
        store.record_observation(observation(frozen, f"S{index:03d}"))
    for kind in REQUIRED_DRILL_TYPES:
        store.record_drill(bound_drill(frozen, DrillTypeV2(str(kind))))

    result = score_soak(store, declared_gates=dict(frozen.gate_thresholds), required_sessions=20)

    assert result.counted_sessions == 20
    assert result.completed_cycles == 1
    assert result.verdict == SoakVerdictV2.PASSED


def test_a_breach_cannot_yet_be_recorded_through_the_generic_store(
    tmp_path: Path,
) -> None:
    # §52 asks for a synthetic FAIL through the generic store, and it cannot be
    # produced: PaperShadowSessionObservationV2 has no field for any duplicate,
    # bypass or stale-data counter, so a breach is not expressible. This is the
    # gap, asserted rather than worked around with a stub that would hide it.
    store, frozen = armed(tmp_path)

    with pytest.raises(TypeError, match="duplicate_remote_orders"):
        observation(frozen, "S000", duplicate_remote_orders=1)

    assert store.counted_sessions() == ()


def test_fail_remains_reachable_for_an_evidence_shape_that_carries_counters(
    tmp_path: Path,
) -> None:
    # The scorer's FAIL logic is sound; it is the record that cannot feed it.
    # Proven here against the evidence interface directly, which is what a
    # repaired observation schema will satisfy.
    store, frozen = armed(tmp_path)
    report = preflight_protocol(frozen, store=store)

    assert SoakVerdictV2.FAILED in report.reachable_verdicts


def test_a_halted_generic_soak_scores_c(tmp_path: Path) -> None:
    # §35, §53. Terminal: it can never become A.
    store, frozen = armed(tmp_path)
    store.record_observation(observation(frozen, "S000"))

    store.halt(
        reason="SYNTHETIC_DEFECT",
        classification="PROGRAM_INTEGRATION_GAP",
        at="2026-10-10T07:00:00+00:00",
    )

    assert store.state() == "HALTED"
    result = score_soak(store, declared_gates=dict(frozen.gate_thresholds), required_sessions=20)
    assert result.verdict == SoakVerdictV2.HALTED


def test_an_observation_from_another_build_is_refused(tmp_path: Path) -> None:
    # §40.
    store, frozen = armed(tmp_path)
    from stock_screener.paper_runtime.soak_v2 import PaperShadowSessionObservationV2

    wrong = PaperShadowSessionObservationV2(
        protocol_fingerprint=frozen.fingerprint(),
        session="S000",
        qualifying_commit="b" * 40,
        observed_at="S000T21:00:00+00:00",
    )

    with pytest.raises(SoakContractError, match=Code.SOAK_DRILL_INADMISSIBLE):
        store.record_observation(wrong)


def test_competing_live_observations_refuse_scoring(tmp_path: Path) -> None:
    # §15 of P2.4R2's spirit: never resolve ambiguity by picking the newest
    # file. Two live accounts of one session means neither is evidence.
    store, frozen = armed(tmp_path)
    store.record_observation(observation(frozen, "S000"))
    # Write a second live observation for the same session directly, as a
    # damaged store would hold.
    from stock_screener.paper_operations import atomic_write_verified

    other = observation(frozen, "S000", orders_submitted=3)
    atomic_write_verified(
        store.observations_directory / f"{other.observation_id}.json", other.as_json()
    )

    with pytest.raises(SoakContractError, match=Code.SOAK_CORRECTION_AMBIGUOUS):
        store.live_observations()


def test_a_correction_keeps_both_records_and_counts_once(tmp_path: Path) -> None:
    store, frozen = armed(tmp_path)
    original = observation(frozen, "S000")
    store.record_observation(original)
    correction = observation(frozen, "S000", orders_submitted=1)

    store.supersede(original, correction, reason="reconciliation re-read")

    assert len(store.observations_for("S000")) == 2
    assert store.counted_sessions() == ("S000",)


def test_a_failed_preflight_leaves_no_activation(tmp_path: Path) -> None:
    # §54. A protocol with a bad gate set cannot even be constructed, so the
    # store never receives one — and no activation is written.
    store = VersionedSoakStore(tmp_path / ".operations", "v3")

    with pytest.raises(SoakContractError):
        PaperShadowSoakProtocol(
            protocol_version=V3,
            protocol_id="x",
            qualifying_commit=COMMIT,
            required_sessions=20,
            declared_gate_ids=("invented",),
            gate_thresholds={"invented": 0},
            **BINDINGS,
        )

    assert store.protocol() is None
    assert store.activation() is None
    assert not store.activation_path.exists()


def test_the_generic_store_satisfies_the_scorer_evidence_interface(
    tmp_path: Path,
) -> None:
    store, _ = armed(tmp_path)

    for name in (
        "live_observations",
        "counted_sessions",
        "drill_records",
        "open_incidents",
        "eligible_elapsed_sessions",
        "soak_state",
    ):
        assert callable(getattr(store, name)), name
