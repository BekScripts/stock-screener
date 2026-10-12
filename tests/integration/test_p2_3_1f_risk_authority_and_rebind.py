"""P2.3.1F — the owner's paper risk limits, and superseding a binding without a reset.

Two things here, joined by one rule: a change to deployment metadata must not
cost the book its history.

The **risk policy** is now authorised for paper. Twelve of the owner's thirteen
fields are represented exactly and two of those are mechanical restatements in
the engine's own schema — `min_position_weight: 0.0` is the engine's `None`
("keep every target"), and a drawdown band is a non-positive threshold with a
multiplier on the base gross limit. The thirteenth, `max_positions: NO CAP`,
`PORTFOLIO_RISK_ENGINE_V1_2` cannot express at all, and the tests below pin that
it is named rather than substituted — because `RiskPolicy` omitting
`max_positions` silently adopts `EXAMPLE_POLICY_V1`'s 20.

The **rebind** supersedes a binding in place of bootstrapping a new root.
`bootstrap_paper_book` and `RuntimeBindingStore.write` keep their refusals
untouched; this is a different operation beside them. What the tests insist on is
that it preserves every lineage, that its history is append-only and
tamper-evident, that it cannot change an account or a generation, and that it
grants no permission of its own.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.paper_runtime import (
    IMMUTABLE_ACROSS_REBIND,
    NO_CAP,
    OWNER_APPROVED_PAPER_LIMITS,
    PAPER_RISK_POLICY_VERSION,
    RUNTIME_BINDING_V2_VERSION,
    Code,
    RuntimeBindingError,
    RuntimeBindingHistory,
    account_capability_from_mapping,
    active_binding,
    mapping_from_evidence,
    paper_risk_policy,
    rebind_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

NOW = "2026-11-02T14:35:00+00:00"


def provider_asset(symbol: str, *, fractionable: bool = True) -> dict[str, Any]:
    """One provider asset payload."""
    return {
        "id": f"asset-{symbol.lower()}",
        "symbol": symbol,
        "class": "us_equity",
        "exchange": "NASDAQ",
        "status": "active",
        "tradable": True,
        "fractionable": fractionable,
    }


def mapping_for(**fractionable: bool) -> Any:
    """A mapping over named symbols with chosen fractionability."""
    identities = {index + 1: name for index, name in enumerate(fractionable)}
    assets = {
        name: provider_asset(name, fractionable=value) for name, value in fractionable.items()
    }
    return mapping_from_evidence(
        identities=identities,
        provider_assets=assets,
        universe="TEST",
        universe_as_of="2026-08-26",
        evidence_at=NOW,
        provider_evidence_fingerprint="e" * 64,
    )


def engine_policy(**resolve: Any) -> Any:
    """The owner-approved policy, with `max_positions` supplied as a test value.

    Supplying it here is what the artefact requires: the engine cannot express
    the owner's NO CAP, so a caller must state a representable substitute
    explicitly. 190 is this test's number and carries no authority.
    """
    settings: dict[str, Any] = {"max_positions": 190}
    settings.update(resolve)
    return paper_risk_policy().engine_policy(**settings)


def intents(count: int = 3, weight: float = 0.02) -> list[Any]:
    """Representative Compounder-shaped intents."""
    from stock_screener.portfolio_risk import SecurityIdentity, StrategyIntent
    from stock_screener.portfolio_risk.intents import Direction

    return [
        StrategyIntent(
            strategy_id="ALPHATRADER_COMPOUNDER_MONTHLY",
            strategy_version="V1",
            decision_id="d1",
            as_of="2026-11-02",
            identity=SecurityIdentity(kind="COMPANY", identity_id=index),
            direction=Direction.LONG,
            requested_weight=weight,
            source_fingerprint=f"fp-{index}",
        )
        for index in range(1, count + 1)
    ]


def snapshot(cash: float = 100_000.0) -> Any:
    """A clean book."""
    from stock_screener.portfolio_risk import PortfolioSnapshot

    return PortfolioSnapshot(as_of=NOW, cash=cash, positions=())


# --------------------------------------------------------------------------
# §0, §3, §4, §5: the owner's limits, represented exactly
# --------------------------------------------------------------------------


def test_every_owner_approved_limit_is_represented() -> None:
    limits = OWNER_APPROVED_PAPER_LIMITS

    assert limits["max_position_weight"] == 0.02
    assert limits["min_cash_weight"] == 0.05
    assert limits["max_trade_notional_weight"] == 0.02
    assert limits["max_portfolio_volatility"] == 0.25
    assert limits["max_position_adv_fraction"] == 0.01
    assert limits["max_trade_adv_fraction"] == 0.0025
    assert limits["max_risk_metric_age_seconds"] == 345600.0
    assert limits["correlation_threshold"] == 0.85
    assert limits["max_correlated_pair_exposure"] == 0.03
    assert limits["construction_mode"] == "PRESERVE_ABSOLUTE_WEIGHTS"
    assert limits["max_positions"] == NO_CAP


def test_the_construction_mode_preserves_and_never_expands() -> None:
    # §0. The owner's intent forbids introducing exposure to unrequested
    # identities, and the frozen RISK_MAY_REDUCE_NEVER_EXPAND rule forbids
    # expanding beyond the request at all. This is the measurement the choice
    # rests on: the other mode scales an under-invested request *up*.
    from stock_screener.portfolio_risk import evaluate
    from stock_screener.portfolio_risk.policy import ConstructionMode

    # Only the construction mode differs: the per-name and per-trade caps are
    # lifted so that neither binds, isolating the one field under test. With
    # the owner's real 0.02 caps in place both modes land at 0.02 per name and
    # the difference would be invisible.
    requested = 0.10
    under_invested = intents(count=3, weight=requested)
    unbound = {"max_position_weight": 1.0, "max_trade_notional_weight": None}

    preserved = dict(
        evaluate(under_invested, snapshot(), engine_policy(**unbound)).approved.targets
    )
    normalised = dict(
        evaluate(
            under_invested,
            snapshot(),
            engine_policy(
                **unbound,
                construction_mode=ConstructionMode.NORMALIZE_TO_INVESTABLE_BUDGET,
            ),
        ).approved.targets
    )

    assert all(value <= requested + 1e-9 for value in preserved.values())
    assert sum(preserved.values()) == pytest.approx(0.30, abs=1e-9)
    # The rejected mode, measured rather than asserted.
    assert any(value > requested + 1e-9 for value in normalised.values())
    assert sum(normalised.values()) == pytest.approx(0.95, abs=1e-9)


def test_min_position_weight_zero_is_the_engines_none() -> None:
    # §0's mechanical translation. The engine refuses a non-positive
    # min_position_weight and documents None as "keep every target", which is
    # the owner's 0.0 in the engine's own schema.
    from stock_screener.portfolio_risk import RiskPolicy
    from stock_screener.portfolio_risk.policy import PolicyError

    assert OWNER_APPROVED_PAPER_LIMITS["min_position_weight"] is None
    assert engine_policy().min_position_weight is None
    with pytest.raises(PolicyError, match="min_position_weight"):
        RiskPolicy(
            policy_id="PROBE",
            policy_version="V1",
            max_positions=10,
            min_position_weight=0.0,
        )


def test_the_drawdown_ladder_is_the_owners_exactly() -> None:
    # §0's other translation: a drawdown is a non-positive decimal and a band
    # multiplies the base gross limit. With a base of 1.0 the multiplier is the
    # owner's exposure.
    bands = engine_policy().drawdown_bands

    assert [(row.threshold, row.max_gross_multiplier) for row in bands] == [
        (-0.05, 0.75),
        (-0.10, 0.50),
        (-0.15, 0.25),
        (-0.20, 0.00),
    ]
    assert engine_policy().max_gross_exposure == 1.0
    # No band imposes a mode the owner did not ask for.
    assert {str(row.mode) for row in bands} == {"NORMAL"}


def test_no_cap_is_named_and_never_substituted() -> None:
    # §5. The engine has no representation for an absent max_positions, and
    # inventing one is the thing that must not happen.
    policy = paper_risk_policy()

    assert policy.unrepresentable == ("max_positions",)
    assert policy.limits["max_positions"] == "NO_CAP"
    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_RISK_POLICY_NOT_AUTHORED):
        policy.engine_policy()


def test_an_omitted_max_positions_would_have_inherited_the_example() -> None:
    # Why the refusal above matters. The engine's default is not neutral: it is
    # EXAMPLE_POLICY_V1's 20, and omitting the field adopts it invisibly.
    from stock_screener.portfolio_risk import RiskPolicy
    from stock_screener.portfolio_risk.policy import EXAMPLE_POLICY_V1

    silent = RiskPolicy(policy_id="PROBE", policy_version="V1")

    assert silent.max_positions == EXAMPLE_POLICY_V1.max_positions == 20
    assert EXAMPLE_POLICY_V1.production_authorized is False


def test_the_policy_is_authorised_for_paper_and_never_for_live() -> None:
    # §2, §7.
    policy = paper_risk_policy()
    body = policy.as_json()

    assert policy.authorized_for_paper is True
    assert policy.authorized_for_live is False
    assert policy.environment == "EXTERNAL_PAPER"
    assert body["version"] == PAPER_RISK_POLICY_VERSION
    assert any("EXTERNAL_PAPER only" in row for row in body["limitations"])
    assert any("NOT_IMPLEMENTED" in row for row in body["limitations"])
    assert engine_policy().production_authorized is False


def test_the_policy_fingerprint_is_canonical_and_material() -> None:
    # §6.
    import dataclasses

    policy = paper_risk_policy()
    reordered = dataclasses.replace(policy, limits=dict(reversed(list(policy.limits.items()))))
    changed = dataclasses.replace(policy, limits={**policy.limits, "max_position_weight": 0.03})

    assert reordered.fingerprint() == policy.fingerprint()
    assert changed.fingerprint() != policy.fingerprint()
    assert len(policy.fingerprint()) == 64


def test_the_metric_age_is_wall_clock_seconds() -> None:
    # §9. 345600 seconds is 96 hours — four days of wall clock, tolerating a
    # long weekend. Not sessions, not market hours.
    age = engine_policy().max_risk_metric_age_seconds

    assert age == 345600.0
    assert age / 3600 == 96.0
    assert age / 86400 == 4.0


# --------------------------------------------------------------------------
# §8: the approved limits, against the real engine
# --------------------------------------------------------------------------


def test_no_approved_position_exceeds_two_percent() -> None:
    from stock_screener.portfolio_risk import evaluate

    approved = evaluate(intents(count=60, weight=0.05), snapshot(), engine_policy()).approved

    assert approved.targets
    for label, weight in dict(approved.targets).items():
        assert weight <= 0.02 + 1e-9, f"{label} at {weight}"


def test_the_book_retains_the_approved_cash_reserve() -> None:
    from stock_screener.portfolio_risk import evaluate

    # Enough names at the per-name cap to want more than the budget allows.
    approved = evaluate(intents(count=80, weight=0.02), snapshot(), engine_policy()).approved
    gross = sum(dict(approved.targets).values())

    assert gross <= 0.95 + 1e-9
    assert engine_policy().investable_budget == pytest.approx(0.95)


def test_risk_reduces_the_strategy_deterministically() -> None:
    from stock_screener.portfolio_risk import evaluate

    first = dict(evaluate(intents(count=40), snapshot(), engine_policy()).approved.targets)
    second = dict(evaluate(intents(count=40), snapshot(), engine_policy()).approved.targets)

    assert first == second


def test_risk_never_introduces_an_unrequested_identity() -> None:
    from stock_screener.portfolio_risk import evaluate

    requested = intents(count=5)
    wanted = {f"COMPANY:{index}" for index in range(1, 6)}

    approved = evaluate(requested, snapshot(), engine_policy()).approved

    assert set(dict(approved.targets)) <= wanted


def test_risk_can_zero_an_exposure_and_reject_all_of_it() -> None:
    from stock_screener.portfolio_risk import evaluate
    from stock_screener.portfolio_risk.policy import PolicyMode

    halted = evaluate(intents(count=3), snapshot(), engine_policy(mode=PolicyMode.HALTED)).approved
    weights = dict(halted.targets)

    assert all(value == 0.0 for value in weights.values()) or weights == {}


def test_the_drawdown_ladder_reduces_exposure_as_approved() -> None:
    # §8. Each band's multiplier against the base gross limit of 1.0, so the
    # effective cap is the owner's stated exposure exactly.
    from stock_screener.portfolio_risk.drawdown import derive_effective_policy

    policy = engine_policy()
    for drawdown, expected in (
        (-0.04, 1.00),
        (-0.05, 0.75),
        (-0.12, 0.50),
        (-0.17, 0.25),
        (-0.25, 0.00),
    ):
        effective = derive_effective_policy(policy, _risk_snapshot(drawdown))
        assert effective.max_gross_exposure == pytest.approx(expected), drawdown


def test_the_strategy_decision_is_unchanged_by_risk() -> None:
    # The frozen separation: risk reduces a portfolio, it does not edit a
    # decision.
    from stock_screener.paper_runtime.risk_transformation import transformation_for
    from stock_screener.portfolio_risk import evaluate
    from stock_screener.strategies import CompounderMonthlyStrategy

    decision = _frozen_decision()
    before = decision.fingerprint()
    approved = evaluate(
        list(CompounderMonthlyStrategy().intents(decision)), snapshot(), engine_policy()
    ).approved

    transformation = transformation_for(
        decision=decision, approved=approved, risk_policy_fingerprint="p" * 64
    )

    assert decision.fingerprint() == before
    assert transformation.body()["strategy_decision_mutated"] is False


def _frozen_decision(count: int = 21) -> Any:
    """One frozen month-end decision, from the real strategy."""
    from stock_screener.execution import ExecutionCalendar
    from stock_screener.portfolio_risk import SecurityIdentity
    from stock_screener.strategies import (
        StrategyInputBundle,
        StrategyInputRecord,
        evaluate_session,
    )

    session = "2026-10-30"
    calendar = ExecutionCalendar(sessions=("2026-10-29", session, "2026-11-02"), name="P2_3_1F")
    report = evaluate_session(
        StrategyInputBundle(
            as_of=session,
            score_version="COMPOUNDER_V1_2",
            universe_fingerprint="universe-1",
            records=tuple(
                StrategyInputRecord(
                    identity=SecurityIdentity(kind="COMPANY", identity_id=index),
                    final_score=90.0 - index,
                    scoring_status="SCORED",
                    freshness="CURRENT",
                    score_date=session,
                    score_version="COMPOUNDER_V1_2",
                    calculated_at=f"{session}T07:40:00+00:00",
                    source_fingerprint=f"fp-{index}",
                )
                for index in range(1, count + 1)
            ),
        ),
        session=session,
        calendar=calendar,
    )
    assert report.decision is not None
    return report.decision


def _risk_snapshot(drawdown: float) -> Any:
    """A risk snapshot at one drawdown, for exercising the approved ladder.

    The snapshot carries NAV and its peak and derives the drawdown, so the
    requested drawdown is expressed as a NAV below a peak of 100.
    """
    from stock_screener.portfolio_risk import PortfolioRiskSnapshot

    peak = 100.0
    return PortfolioRiskSnapshot(
        as_of=NOW,
        portfolio_nav=peak * (1.0 + drawdown),
        portfolio_peak_nav=peak,
        source_fingerprint="s" * 64,
    )


# --------------------------------------------------------------------------
# §13-§16, §31-§34: the rebind preserves every lineage
# --------------------------------------------------------------------------


def ledger_events(root: Path) -> list[dict[str, Any]]:
    """Every committed accounting event, as stored."""
    from stock_screener.paper_runtime.binding import RUNTIME_DIRECTORY

    directory = root / RUNTIME_DIRECTORY / "portfolio" / "ledger"
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))
    ]


def rebind(paper_runtime: Any, **overrides: Any) -> Any:
    """Supersede the fixture's binding with the owner-approved artefacts."""
    settings: dict[str, Any] = {
        "root": paper_runtime.root,
        "account_binding_fingerprint": "c" * 64,
        "risk_policy": paper_risk_policy(),
        "mapping": mapping_for(AAA=True, BBB=True, CCC=True),
        "reason": "enable provider-verified fractional capability",
        "now": NOW,
        "code_commit": "17e5c9f",
        "confirmed": True,
    }
    settings.update(overrides)
    return rebind_runtime(**settings)


def test_a_rebind_preserves_every_lineage(paper_runtime: Any) -> None:
    # §31, §33, §34. The whole reason this operation exists.
    before = ledger_events(paper_runtime.root)
    predecessor = paper_runtime.binding

    binding = rebind(paper_runtime)

    assert binding.version == RUNTIME_BINDING_V2_VERSION
    assert binding.predecessor_fingerprint == predecessor.fingerprint()
    assert binding.accounting_generation == predecessor.accounting_generation
    assert binding.execution_journal_generation == (predecessor.execution_journal_generation)
    assert binding.bootstrap_epoch == predecessor.bootstrap_epoch
    assert binding.runtime_id == predecessor.runtime_id
    # No opening balance was written and no event moved.
    after = ledger_events(paper_runtime.root)
    assert after == before
    assert [row["event"]["event_type"] for row in after].count("OPENING_BALANCE") == 1


def test_the_superseded_binding_is_untouched(paper_runtime: Any) -> None:
    # §21. The V1 record stays byte-identical and independently readable.
    import hashlib

    from stock_screener.paper_runtime import RuntimeBindingStore
    from stock_screener.paper_runtime.binding import RUNTIME_DIRECTORY

    path = paper_runtime.root / RUNTIME_DIRECTORY / "binding.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    rebind(paper_runtime)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert RuntimeBindingStore(paper_runtime.root).require().fingerprint() == (
        paper_runtime.binding.fingerprint()
    )


def test_the_active_binding_is_deterministically_the_successor(
    paper_runtime: Any,
) -> None:
    # §23, §32.
    assert active_binding(paper_runtime.root).fingerprint() == (paper_runtime.binding.fingerprint())

    binding = rebind(paper_runtime)

    assert active_binding(paper_runtime.root).fingerprint() == binding.fingerprint()
    assert active_binding(paper_runtime.root).version == RUNTIME_BINDING_V2_VERSION


def test_the_history_is_append_only_and_chained(paper_runtime: Any) -> None:
    # §22, §32. A reviewer can establish binding A -> authorised rebind ->
    # binding B, with both fingerprints and the reason.
    first = rebind(paper_runtime)
    second = rebind(paper_runtime, account_binding_fingerprint="d" * 64)

    history = RuntimeBindingHistory(paper_runtime.root)
    records = history.records()
    report = history.verify()

    assert [row.sequence for row in records] == [1, 2]
    assert records[0].binding.predecessor_fingerprint == (paper_runtime.binding.fingerprint())
    assert records[1].binding.predecessor_fingerprint == first.fingerprint()
    assert records[1].previous_hash == records[0].record_hash
    assert report["chain_verified"] is True
    assert report["anchor_verified"] is True
    assert active_binding(paper_runtime.root).fingerprint() == second.fingerprint()
    assert all(row.binding.reason for row in records)


def test_a_tampered_history_fails_closed(paper_runtime: Any) -> None:
    # §23. And it does not silently reactivate the superseded binding: that
    # binding was replaced deliberately.
    rebind(paper_runtime)
    history = RuntimeBindingHistory(paper_runtime.root)
    victim = sorted(history.directory.glob("*.json"))[0]
    body = json.loads(victim.read_text(encoding="utf-8"))
    body["binding"]["account_binding_fingerprint"] = "f" * 64
    victim.write_text(json.dumps(body), encoding="utf-8")

    assert history.verify()["chain_verified"] is False
    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_REBIND_HISTORY_BROKEN):
        active_binding(paper_runtime.root)


def test_a_truncated_history_is_detected(paper_runtime: Any) -> None:
    rebind(paper_runtime)
    rebind(paper_runtime, account_binding_fingerprint="d" * 64)
    history = RuntimeBindingHistory(paper_runtime.root)
    sorted(history.directory.glob("*.json"))[-1].unlink()

    report = history.verify()

    assert report["records"] == 1
    assert report["anchor_verified"] is False


def test_the_execution_journal_is_unchanged_by_a_rebind(paper_runtime: Any) -> None:
    # §34.
    from stock_screener.execution import ExecutionStore
    from stock_screener.paper_runtime.binding import RUNTIME_DIRECTORY

    store = ExecutionStore(
        paper_runtime.root / RUNTIME_DIRECTORY / "execution",
        generation=paper_runtime.binding.execution_journal_generation,
    )
    before = store.verify()

    rebind(paper_runtime)

    after = ExecutionStore(
        paper_runtime.root / RUNTIME_DIRECTORY / "execution",
        generation=paper_runtime.binding.execution_journal_generation,
    ).verify()
    assert after["generation"] == before["generation"]
    assert after["record_count"] == before["record_count"]
    assert after["verified"] is before["verified"]


# --------------------------------------------------------------------------
# §20, §24, §35-§39: what a rebind may not do
# --------------------------------------------------------------------------


def test_a_rebind_refuses_without_confirmation(paper_runtime: Any) -> None:
    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_REBIND_REFUSED):
        rebind(paper_runtime, confirmed=False)
    assert RuntimeBindingHistory(paper_runtime.root).records() == ()


def test_a_rebind_refuses_an_unauthorised_policy(paper_runtime: Any) -> None:
    # §38.
    import dataclasses

    candidate = dataclasses.replace(paper_risk_policy(), authorized_for_paper=False)

    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_REBIND_REFUSED):
        rebind(paper_runtime, risk_policy=candidate)


def test_a_rebind_refuses_a_live_policy(paper_runtime: Any) -> None:
    # §39. Live is refused whether it claims authorisation or merely names the
    # environment.
    import dataclasses

    claiming = dataclasses.replace(paper_risk_policy(), authorized_for_live=True)
    naming = dataclasses.replace(paper_risk_policy(), environment="EXTERNAL_LIVE")

    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_REBIND_REFUSED):
        rebind(paper_runtime, risk_policy=claiming)
    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_REBIND_REFUSED):
        rebind(paper_runtime, risk_policy=naming)


@pytest.mark.parametrize("field", list(IMMUTABLE_ACROSS_REBIND))
def test_a_rebind_cannot_change_an_immutable_field(paper_runtime: Any, field: str) -> None:
    # §20, §35, §36, §37. Account identity, strategy, the three generations and
    # the bootstrap epoch all need their own reviewed path.
    import dataclasses

    from stock_screener.paper_runtime import rebind as module

    original = module._inherit

    def tampered(predecessor: Any) -> dict[str, Any]:
        carried = original(predecessor)
        current = carried.get(field)
        carried[field] = "CHANGED" if isinstance(current, str) else 999
        return carried

    module._inherit = tampered
    try:
        with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_REBIND_REFUSED):
            rebind(paper_runtime)
    finally:
        module._inherit = original
    assert dataclasses.is_dataclass(paper_runtime.binding)


def test_a_failed_precondition_blocks_the_rebind(paper_runtime: Any) -> None:
    # §26, §27. A rebind in the middle of unresolved operational state would
    # leave the execution authority ambiguous.
    def unresolved() -> dict[str, Any]:
        return {"failures": ("a submission is unresolved: REMOTE_STATE_UNKNOWN",)}

    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_REBIND_PRECONDITION_FAILED):
        rebind(paper_runtime, preconditions=unresolved)
    assert RuntimeBindingHistory(paper_runtime.root).records() == ()


def test_a_rebind_writes_no_opening_balance_and_reads_no_cash(paper_runtime: Any) -> None:
    # §24. Rebind is not bootstrap.
    import ast
    from pathlib import Path as _Path

    import stock_screener.paper_runtime.rebind as module

    # Parsed, not grepped: the module's own docstring states that it writes no
    # OpeningBalanceEvent, and a text scan cannot tell that promise from a call.
    tree = ast.parse(_Path(module.__file__).read_text(encoding="utf-8"))
    referenced = (
        {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        | {
            alias.name.rsplit(".", 1)[-1]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom | ast.Import)
            for alias in node.names
        }
    )

    for forbidden in (
        "OpeningBalanceEvent",
        "bootstrap_paper_book",
        "account_evidence",
        "require_cash",
        "read_account",
        "PortfolioStore",
    ):
        assert forbidden not in referenced, forbidden


def test_a_rebind_does_not_arm_operations(paper_runtime: Any) -> None:
    # §43, §44. The control state is whatever it was, and a binding is not a
    # permission.
    from stock_screener.paper_operations import ArmingStore, KillSwitchStore

    before_kill = KillSwitchStore(paper_runtime.root).current().state
    before_arm = ArmingStore(paper_runtime.root).current()

    rebind(paper_runtime)

    assert KillSwitchStore(paper_runtime.root).current().state == before_kill
    assert (ArmingStore(paper_runtime.root).current() is None) == (before_arm is None)


# --------------------------------------------------------------------------
# §29, §30: the capability a mapping actually establishes
# --------------------------------------------------------------------------


def test_account_capability_needs_every_routable_instrument() -> None:
    # §29, §30. The capability check is account-wide and fractionability is per
    # instrument, so a partial claim would permit a fractional order for an
    # instrument that cannot take one.
    whole = account_capability_from_mapping(mapping_for(AAA=True, BBB=False))
    every = account_capability_from_mapping(mapping_for(AAA=True, BBB=True))

    assert whole["fractional_quantities"] is False
    assert whole["whole_share_only_instruments"] == 1
    assert every["fractional_quantities"] is True
    assert every["fractionable_instruments"] == 2
    assert every["derived_from_mapping"] == mapping_for(AAA=True, BBB=True).fingerprint()


def test_an_unknown_capability_never_becomes_true() -> None:
    # §30. An instrument whose fractionability the venue did not state is not
    # routable at all, so it cannot contribute to an account-wide claim.
    mapping = mapping_from_evidence(
        identities={1: "QUIET"},
        provider_assets={
            "QUIET": {
                "id": "asset-quiet",
                "symbol": "QUIET",
                "class": "us_equity",
                "exchange": "NASDAQ",
                "status": "active",
                "tradable": True,
            }
        },
        universe="TEST",
        universe_as_of="2026-08-26",
        evidence_at=NOW,
        provider_evidence_fingerprint="e" * 64,
    )

    capability = account_capability_from_mapping(mapping)

    assert capability["routable_instruments"] == 0
    assert capability["fractional_quantities"] is False


# --------------------------------------------------------------------------
# §41, §42, §45: the rebound capability through the production composition
# --------------------------------------------------------------------------


def composed_after_rebind(paper_runtime: Any, mapping: Any, **overrides: Any) -> Any:
    """Rebind, author the artefacts, and compose through the production root."""
    import httpx

    from stock_screener.paper_operations import OperationsMode
    from stock_screener.paper_runtime import (
        active_binding as _active,
    )
    from stock_screener.paper_runtime import (
        compose_operations,
        write_risk_policy,
        write_symbol_mapping,
    )

    rebind(paper_runtime, mapping=mapping, **overrides)
    policy = engine_policy()
    write_risk_policy(
        paper_runtime.root,
        {
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "long_only": policy.long_only,
            "max_gross_exposure": policy.max_gross_exposure,
            "max_net_exposure": policy.max_net_exposure,
            "max_position_weight": policy.max_position_weight,
            "max_positions": policy.max_positions,
            "min_cash_weight": policy.min_cash_weight,
            "production_authorized": policy.production_authorized,
        },
    )
    write_symbol_mapping(paper_runtime.root, mapping.authored_symbols())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    slot = paper_runtime.schedule.slot_for(NOW)
    paper_runtime.clock.set(NOW)
    return compose_operations(
        settings=_composition_settings(),
        root=paper_runtime.root,
        binding=_active(paper_runtime.root),
        slot=slot,
        schedule=paper_runtime.schedule,
        calendar=paper_runtime.calendar,
        mode=OperationsMode.EXTERNAL_PAPER,
        clock=paper_runtime.clock,
        client=httpx.Client(
            base_url="https://paper-api.alpaca.markets",
            transport=httpx.MockTransport(handler),
            timeout=5.0,
        ),
        marks={},
    )


def _composition_settings() -> Any:
    """Typed settings for a composition over a scripted network."""
    from dataclasses import dataclass
    from dataclasses import field as _field

    from pydantic import SecretStr

    @dataclass
    class Settings:
        """The fields the composition reads."""

        database_url: str = "sqlite:///:memory:"
        alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
        alpaca_paper_account_id: str = "paper-account-1"
        alpaca_paper_key_id: Any = _field(default_factory=lambda: SecretStr("PKTESTKEY01"))
        alpaca_paper_secret_key: Any = _field(default_factory=lambda: SecretStr("shhh"))

    return Settings()


def test_a_rebound_runtime_accepts_a_fractional_quantity(paper_runtime: Any) -> None:
    # §41. The refusal P2.3.1D hit, now gone — and the quantity is unchanged.
    from decimal import Decimal

    mapping = mapping_for(AAA=True, BBB=True, CCC=True)
    composition = composed_after_rebind(paper_runtime, mapping)
    try:
        binding = composition.adapter.binding
        quantity = Decimal("333.33333330")

        assert binding.capabilities.fractional_quantities is True
        # Does not raise, and reshapes nothing.
        binding.capabilities.require_quantity(quantity)
        assert quantity == Decimal("333.33333330")
        assert composition.submit is not None
    finally:
        composition.close()


def test_a_whole_share_mapping_still_refuses_a_fractional_quantity(
    paper_runtime: Any,
) -> None:
    # §42. No rounding anywhere: the existing refusal stands, because the
    # capability was never widened for a mapping containing a whole-share
    # instrument.
    from decimal import Decimal

    from stock_screener.execution.errors import ExecutionError

    mapping = mapping_for(AAA=True, WHOLE=False)
    composition = composed_after_rebind(paper_runtime, mapping)
    try:
        binding = composition.adapter.binding

        assert binding.capabilities.fractional_quantities is False
        with pytest.raises(ExecutionError, match="BROKER_FRACTIONAL_NOT_SUPPORTED"):
            binding.capabilities.require_quantity(Decimal("333.33333330"))
        # A whole quantity is still permitted.
        binding.capabilities.require_quantity(Decimal(333))
    finally:
        composition.close()


def test_the_composition_consumes_the_v2_binding(paper_runtime: Any) -> None:
    # §45. And it reports what it bound, without a secret.
    mapping = mapping_for(AAA=True, BBB=True)
    composition = composed_after_rebind(paper_runtime, mapping)
    try:
        readiness = composition.readiness()

        assert readiness["binding_version"] == RUNTIME_BINDING_V2_VERSION
        assert readiness["predecessor_fingerprint"] == (paper_runtime.binding.fingerprint())
        assert readiness["risk_policy_fingerprint"] == paper_risk_policy().fingerprint()
        assert readiness["instrument_mapping_fingerprint"] == mapping.fingerprint()
        assert readiness["account_capability"]["fractional_quantities"] is True
        assert readiness["live_capability"] is False
        text = json.dumps(readiness).lower()
        for forbidden in ("pktestkey01", "shhh", "secret", "paper-account-1"):
            assert forbidden not in text, forbidden
    finally:
        composition.close()


def test_a_historical_v1_root_still_composes(paper_runtime: Any) -> None:
    # §46. Backward compatibility: a root that was never rebound keeps working
    # under the existing semantics, with nothing rewritten.
    from stock_screener.paper_runtime import (
        active_binding as _active,
    )
    from stock_screener.paper_runtime import (
        write_risk_policy,
        write_symbol_mapping,
    )

    policy = engine_policy()
    write_risk_policy(
        paper_runtime.root,
        {
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "max_gross_exposure": policy.max_gross_exposure,
            "max_position_weight": policy.max_position_weight,
            "max_positions": policy.max_positions,
            "min_cash_weight": policy.min_cash_weight,
        },
    )
    write_symbol_mapping(paper_runtime.root, mapping_for(AAA=True).authored_symbols())

    binding = _active(paper_runtime.root)

    assert binding.version == "PAPER_OPERATIONS_RUNTIME_BINDING_V1"
    assert RuntimeBindingHistory(paper_runtime.root).records() == ()
    assert binding.fingerprint() == paper_runtime.binding.fingerprint()


def test_the_active_account_fingerprint_is_the_new_one(paper_runtime: Any) -> None:
    # The inherited block keeps the predecessor's account fingerprint as
    # provenance, and the binding's own declared field shadows it. Reading the
    # old one as active would answer the question the supersession exists to
    # change.
    binding = rebind(paper_runtime, account_binding_fingerprint="c" * 64)

    assert binding.account_binding_fingerprint == "c" * 64
    assert binding.inherited["account_binding_fingerprint"] == paper_runtime.account
    assert binding.inherited["account_binding_fingerprint"] != "c" * 64
