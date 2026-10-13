"""P2.3.1G — the owner's NO CAP made expressible, and a deployable universe.

Two contract mismatches are closed here, and the tests are mostly about the
*additive* part. The owner approved `max_positions = NO CAP` in P2.3.1F and
`PORTFOLIO_RISK_ENGINE_V1_2` had no way to say it, so the artefact named the gap
rather than substituting a number. V1.3 can say it. The risk is therefore not
that the new behaviour is wrong — it is that introducing it quietly changes an
existing decision, so a large share of what follows pins V1.2 in place.

The second mismatch is that the venue will only take a fractional quantity for
some instruments, and the strategy's weights produce almost nothing else. V2
restricts the selection universe accordingly, **in strategy authority**. The
tests insist on that placement: a company dropped for a venue capability must be
recorded as dropped for a venue capability, never as having failed Compounder
research, and the decile must be taken from what can actually be held rather
than taken first and trimmed afterwards.

The third thing under test is the transition that would put V2 in front of an
existing set of books. It may move the strategy fingerprint — and nothing else,
and not while a decision is waiting to execute.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.paper_runtime import (
    DEPLOYMENT_UNIVERSE_VERSION,
    IMMUTABLE_ACROSS_REBIND,
    OWNER_APPROVED_PAPER_LIMITS,
    OWNER_APPROVED_PAPER_LIMITS_V2,
    PAPER_RISK_POLICY_V2_ENGINE,
    PAPER_RISK_POLICY_V2_VERSION,
    STRATEGY_REVISION_FIELDS,
    STRATEGY_REVISION_VERSION,
    Code,
    DeploymentUniverse,
    Routability,
    RuntimeBindingError,
    RuntimeBindingHistory,
    UniverseExclusion,
    active_binding,
    fractionable_universe,
    mapping_from_evidence,
    paper_risk_policy,
    paper_risk_policy_v2,
    rebind_runtime,
    revise_strategy,
)
from stock_screener.portfolio_risk import (
    PortfolioSnapshot,
    RiskPolicy,
    SecurityIdentity,
    StrategyIntent,
    evaluate,
)
from stock_screener.portfolio_risk.engine import (
    MULTI_STRATEGY_ENGINE_VERSION,
    NO_POSITION_CAP_ENGINE_VERSION,
)
from stock_screener.strategies import (
    DEPLOYMENT_EXCLUSION_REASON,
    STRATEGY_VERSION,
    STRATEGY_VERSION_V2,
    CompounderMonthlyFractionableStrategy,
    CompounderMonthlyStrategy,
    StrategyDeploymentAuthorization,
    StrategyError,
    StrategyInputBundle,
    StrategyInputRecord,
)
from stock_screener.strategies import Code as StrategyCode

if TYPE_CHECKING:
    from stock_screener.execution import ExecutionCalendar

pytestmark = pytest.mark.integration

NOW = "2026-11-02T14:35:00+00:00"
AS_OF = "2026-10-30T16:00:00-04:00"
SESSION = "2026-10-30"
SESSIONS = ("2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02")
BEFORE_CLOSE = "2026-10-30T18:00:00+00:00"
NAV = 1_000_000.0
REPO_ROOT = Path(__file__).resolve().parents[2]
ARTEFACTS = REPO_ROOT / "docs" / "reference" / "deployment"
RISK_SOURCE = REPO_ROOT / "src" / "stock_screener" / "portfolio_risk"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


@pytest.fixture
def calendar() -> ExecutionCalendar:
    """The market calendar. Authoritative for month ends."""
    from stock_screener.execution import ExecutionCalendar

    return ExecutionCalendar(sessions=SESSIONS, name="P2_3_1G")


def identity(number: int) -> SecurityIdentity:
    """A durable identity. The symbol is metadata on it."""
    return SecurityIdentity(kind="COMPANY", identity_id=number, symbol=f"T{number:02d}")


def policy(cap: int | None, **overrides: Any) -> RiskPolicy:
    """A risk policy with a chosen position cap."""
    settings: dict[str, Any] = {
        "policy_id": "P2_3_1G",
        "policy_version": "1",
        "long_only": True,
        "max_gross_exposure": 1.00,
        "max_position_weight": 0.10,
        "max_positions": cap,
        "min_cash_weight": 0.05,
        "production_authorized": False,
    }
    settings.update(overrides)
    return RiskPolicy(**settings)


def snapshot() -> PortfolioSnapshot:
    """An all-cash portfolio."""
    return PortfolioSnapshot(as_of=AS_OF, cash=NAV, positions=(), price_fingerprint="prices")


def intents(count: int, weight: float = 0.03) -> list[StrategyIntent]:
    """`count` Compounder-shaped requests, descending priority."""
    return [
        StrategyIntent(
            strategy_id="S",
            strategy_version="1",
            decision_id="d1",
            as_of=AS_OF,
            identity=identity(number),
            requested_weight=weight,
            priority=float(1000 - number),
            reason="test",
            source_fingerprint="fp",
        )
        for number in range(1, count + 1)
    ]


def provider_asset(symbol: str, **overrides: Any) -> dict[str, Any]:
    """One provider asset payload."""
    asset: dict[str, Any] = {
        "id": f"asset-{symbol.lower()}",
        "symbol": symbol,
        "class": "us_equity",
        "exchange": "NASDAQ",
        "status": "active",
        "tradable": True,
        "fractionable": True,
    }
    asset.update(overrides)
    return asset


def mapping_for(*, identities: dict[int, str], assets: dict[str, dict[str, Any]]) -> Any:
    """A mapping over explicit evidence."""
    return mapping_from_evidence(
        identities=identities,
        provider_assets=assets,
        universe="TEST",
        universe_as_of="2026-08-26",
        evidence_at=NOW,
        provider_evidence_fingerprint="e" * 64,
    )


def record(
    number: int,
    score: float | None,
    *,
    status: str = "SCORED",
    freshness: str = "CURRENT",
    calculated_at: str = BEFORE_CLOSE,
) -> StrategyInputRecord:
    """One evidence record."""
    return StrategyInputRecord(
        identity=identity(number),
        final_score=score,
        scoring_status=status,
        freshness=freshness,
        score_date=SESSION,
        score_version="COMPOUNDER_V1_2",
        calculated_at=calculated_at,
        source_fingerprint=f"fp-{number}",
    )


def bundle(*records: StrategyInputRecord) -> StrategyInputBundle:
    """A bundle over the given records."""
    return StrategyInputBundle(
        as_of=SESSION,
        score_version="COMPOUNDER_V1_2",
        universe_fingerprint="u" * 64,
        records=records,
    )


class FakeUniverse:
    """A deployment universe over an explicit member set."""

    def __init__(self, members: set[int], digest: str = "d" * 64) -> None:
        self.members = members
        self.digest = digest

    def contains(self, identity: Any) -> bool:
        """Whether this identity may be deployed to."""
        return int(identity.identity_id) in self.members

    def fingerprint(self) -> str:
        """The universe digest."""
        return self.digest


def universe_of(*, fractionable: tuple[int, ...], whole_share: tuple[int, ...] = ()) -> Any:
    """A real `DeploymentUniverse` derived from constructed provider evidence."""
    identities = {number: f"T{number:02d}" for number in (*fractionable, *whole_share)}
    assets = {
        f"T{number:02d}": provider_asset(f"T{number:02d}", fractionable=number in set(fractionable))
        for number in identities
    }
    return fractionable_universe(mapping_for(identities=identities, assets=assets))


def grant_for(strategy: Any, **overrides: Any) -> StrategyDeploymentAuthorization:
    """A paper deployment grant covering exactly this strategy."""
    settings: dict[str, Any] = {
        "authorization_id": "TEST_GRANT",
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy.strategy_version,
        "strategy_fingerprint": strategy.fingerprint(),
        "environment": "EXTERNAL_PAPER",
        "code_commit": "59e86d5",
        "created_at": NOW,
    }
    settings.update(overrides)
    return StrategyDeploymentAuthorization(**settings)


def revise(paper_runtime: Any, **overrides: Any) -> Any:
    """Revise the fixture runtime's strategy to V2."""
    strategy = CompounderMonthlyFractionableStrategy(FakeUniverse({1}))
    settings: dict[str, Any] = {
        "root": paper_runtime.root,
        "strategy": strategy,
        "authorization": grant_for(strategy),
        "risk_policy": paper_risk_policy_v2(),
        "mapping": universe_mapping(),
        "account_binding_fingerprint": paper_runtime.account,
        "reason": "deploy the fractionable-universe strategy revision",
        "now": NOW,
        "code_commit": "59e86d5",
        "confirmed": True,
    }
    settings.update(overrides)
    return revise_strategy(**settings)


def universe_mapping() -> Any:
    """A small all-fractionable mapping, for the binding artefacts."""
    return mapping_for(
        identities={1: "AAA", 2: "BBB"},
        assets={"AAA": provider_asset("AAA"), "BBB": provider_asset("BBB")},
    )


# --------------------------------------------------------------------------- #
# §2-§5 the additive engine version
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_absent_position_cap_approves_every_request_the_other_limits_allow() -> None:
    capped = evaluate(intents(25), snapshot(), policy(20)).approved
    uncapped = evaluate(intents(25), snapshot(), policy(None)).approved

    assert len(capped.targets) == 20
    assert len(uncapped.targets) == 25


@pytest.mark.integration
def test_a_numeric_position_cap_keeps_its_existing_engine_version() -> None:
    decision = evaluate(intents(25), snapshot(), policy(20)).approved

    assert decision.engine_version != NO_POSITION_CAP_ENGINE_VERSION


@pytest.mark.integration
def test_an_absent_position_cap_is_labelled_the_new_engine_version() -> None:
    decision = evaluate(intents(25), snapshot(), policy(None)).approved

    assert decision.engine_version == NO_POSITION_CAP_ENGINE_VERSION


@pytest.mark.integration
def test_an_absent_cap_changes_no_decision_a_numeric_cap_would_not_have_changed() -> None:
    """V1.2's decisions are untouched where the cap never bound.

    Twenty requests against a cap of twenty is the case where the count stage
    rejects nothing, so removing the cap must produce the identical portfolio —
    the same targets, the same weights, the same cash. Only the version label
    may differ, because the pipeline that ran is genuinely a different one.
    """
    capped = evaluate(intents(20), snapshot(), policy(20)).approved
    uncapped = evaluate(intents(20), snapshot(), policy(None)).approved

    assert uncapped.targets == capped.targets
    assert uncapped.cash_target_weight == capped.cash_target_weight
    assert uncapped.gross_exposure == capped.gross_exposure


@pytest.mark.integration
def test_an_absent_cap_still_enforces_the_gross_exposure_limit() -> None:
    decision = evaluate(intents(40), snapshot(), policy(None)).approved

    assert decision.gross_exposure == pytest.approx(0.95)


@pytest.mark.integration
def test_an_absent_cap_still_enforces_the_per_position_weight_limit() -> None:
    decision = evaluate(intents(1, weight=0.40), snapshot(), policy(None)).approved

    assert max(decision.targets.values()) == pytest.approx(0.10)


@pytest.mark.integration
def test_an_absent_cap_still_enforces_the_minimum_cash_weight() -> None:
    decision = evaluate(intents(40), snapshot(), policy(None)).approved

    assert decision.cash_target_weight == pytest.approx(0.05)


@pytest.mark.integration
def test_correlation_control_without_an_absent_cap_keeps_the_v1_2_version() -> None:
    """The newer label is claimed only by the newer capability.

    V1.2 is selected by correlation control, and a policy using it with a
    numeric cap must still read as V1.2 — otherwise every existing
    multi-strategy decision would be relabelled by this change.
    """
    correlated = policy(20, correlation_threshold=0.85, max_correlated_pair_exposure=0.03)
    decision = evaluate(intents(3), snapshot(), correlated).approved

    assert decision.engine_version == MULTI_STRATEGY_ENGINE_VERSION


@pytest.mark.integration
def test_a_zero_position_cap_is_still_refused() -> None:
    """Absence is not the same as zero, and zero remains invalid.

    If `None` had been implemented as a sentinel, the natural sentinel would be
    `0` — so the policy must still refuse it.
    """
    from stock_screener.portfolio_risk import PolicyError

    with pytest.raises(PolicyError, match="max_positions must be at least 1"):
        policy(0)


@pytest.mark.integration
def test_a_negative_position_cap_is_still_refused() -> None:
    from stock_screener.portfolio_risk import PolicyError

    with pytest.raises(PolicyError, match="max_positions must be at least 1"):
        policy(-1)


@pytest.mark.integration
def test_the_engine_default_position_cap_is_unchanged() -> None:
    """The default stays 20, and that is load-bearing.

    Defaulting it to `None` would silently make every policy written before this
    phase an uncapped policy and relabel its decisions V1.3.
    """
    from stock_screener.portfolio_risk.policy import EXAMPLE_POLICY_V1

    assert EXAMPLE_POLICY_V1.max_positions == 20


@pytest.mark.integration
def test_an_absent_cap_rejects_no_position_for_exceeding_the_count() -> None:
    from stock_screener.portfolio_risk import ReasonCode

    decision = evaluate(intents(25), snapshot(), policy(None)).approved
    counted = [
        row
        for row in decision.risk_decisions
        if ReasonCode.MAX_POSITION_COUNT in str(row.get("reason_codes", ()))
    ]

    assert counted == []


@pytest.mark.integration
def test_no_position_cap_is_encoded_as_a_sentinel_number_anywhere_in_the_engine() -> None:
    """Absence is expressed as absence, not as a number that behaves like none.

    Parsed rather than grepped: the modules under test *describe* the sentinels
    they refuse in prose, so a text scan would match its own documentation. This
    reads the AST and looks only at what is actually compared or assigned to a
    `max_positions`-shaped expression.
    """
    forbidden = {0, -1, 999, 9999, 99999, 999999, 2**31 - 1, 2**63 - 1}
    offenders: list[str] = []
    for path in sorted(RISK_SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                parts = [node.left, *node.comparators]
                names = {
                    part.attr
                    for part in parts
                    if isinstance(part, ast.Attribute) and part.attr == "max_positions"
                }
                if not names:
                    continue
                for part in parts:
                    if isinstance(part, ast.Constant) and part.value in forbidden:
                        offenders.append(f"{path.name}:{node.lineno} compares to {part.value!r}")
            if isinstance(node, ast.keyword) and node.arg == "max_positions":
                value = node.value
                if isinstance(value, ast.Constant) and value.value in forbidden:
                    offenders.append(f"{path.name}:{value.lineno} passes {value.value!r}")

    assert offenders == []


# --------------------------------------------------------------------------- #
# §6-§10 the V2 paper risk policy
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_v2_paper_policy_is_bound_to_the_new_engine_version() -> None:
    assert paper_risk_policy_v2().engine_version == PAPER_RISK_POLICY_V2_ENGINE
    assert PAPER_RISK_POLICY_V2_ENGINE == NO_POSITION_CAP_ENGINE_VERSION


@pytest.mark.integration
def test_the_v2_paper_policy_names_no_unrepresentable_field() -> None:
    assert paper_risk_policy_v2().unrepresentable == ()


@pytest.mark.integration
def test_the_v2_paper_policy_builds_an_engine_policy_with_nothing_supplied() -> None:
    """The gap is closed, so no caller has to resolve anything.

    V1 refuses `engine_policy()` with no arguments because the owner's NO CAP
    has no V1.2 representation. V2 must not need the argument at all — if it did,
    the engine change would have bought nothing.
    """
    built = paper_risk_policy_v2().engine_policy()

    assert built.max_positions is None


@pytest.mark.integration
def test_the_v2_paper_policy_expresses_no_cap_as_absence() -> None:
    assert OWNER_APPROVED_PAPER_LIMITS_V2["max_positions"] is None


@pytest.mark.integration
def test_the_v2_paper_policy_carries_every_other_owner_limit_unchanged() -> None:
    """The twelve values the owner approved in P2.3.1F are identical in V2.

    Compared field by field against V1's own dict rather than against a second
    hand-written copy: a retyped list of thirteen numbers is a second chance to
    get one of them wrong, and this is the assertion that would catch it.
    """
    differing = {
        key
        for key in OWNER_APPROVED_PAPER_LIMITS
        if OWNER_APPROVED_PAPER_LIMITS[key] != OWNER_APPROVED_PAPER_LIMITS_V2[key]
    }

    assert differing == {"max_positions"}


@pytest.mark.integration
def test_the_v2_paper_policy_is_authorised_for_paper_only() -> None:
    built = paper_risk_policy_v2()

    assert built.authorized_for_paper is True
    assert built.authorized_for_live is False
    assert built.environment == "EXTERNAL_PAPER"


@pytest.mark.integration
def test_the_v2_paper_policy_builds_an_engine_policy_not_authorised_for_production() -> None:
    assert paper_risk_policy_v2().engine_policy().production_authorized is False


@pytest.mark.integration
def test_the_v2_paper_policy_declares_its_own_contract_version() -> None:
    assert paper_risk_policy_v2().body()["version"] == PAPER_RISK_POLICY_V2_VERSION


@pytest.mark.integration
def test_the_v1_paper_policy_is_not_rewritten_by_the_v2_contract() -> None:
    """V1 keeps its engine, its named gap and its exact fingerprint.

    The fingerprint is the assertion that matters. V1 was authorised against
    `PORTFOLIO_RISK_ENGINE_V1_2` with `max_positions` named as unrepresentable,
    and editing it to look as though the gap had never existed would erase the
    record of what the owner actually approved.
    """
    built = paper_risk_policy()

    assert built.engine_version == "PORTFOLIO_RISK_ENGINE_V1_2"
    assert built.unrepresentable == ("max_positions",)
    assert built.fingerprint() == "6804fbb289cf7fb9629b622df1ab1a8198931b052f0649ea09bcd1014c0faa93"


@pytest.mark.integration
def test_the_v1_paper_policy_still_refuses_to_build_without_a_resolution() -> None:
    with pytest.raises(RuntimeBindingError, match="cannot represent") as caught:
        paper_risk_policy().engine_policy()

    assert caught.value.code is Code.RUNTIME_RISK_POLICY_NOT_AUTHORED


@pytest.mark.integration
def test_the_v2_paper_policy_fingerprints_differently_from_v1() -> None:
    assert paper_risk_policy_v2().fingerprint() != paper_risk_policy().fingerprint()


@pytest.mark.integration
def test_an_uncapped_owner_policy_runs_the_new_engine_version_end_to_end() -> None:
    """The authorised policy and the engine agree, driven rather than asserted.

    The whole contract mismatch this phase closes is between an artefact and an
    engine, so the test that matters puts the real artefact through the real
    engine and reads the version off the decision.
    """
    built = paper_risk_policy_v2().engine_policy()
    decision = evaluate(intents(3, weight=0.02), snapshot(), built).approved

    assert decision.engine_version == NO_POSITION_CAP_ENGINE_VERSION


@pytest.mark.integration
def test_no_persisted_v2_policy_artefact_carries_a_credential() -> None:
    from stock_screener.execution.external import check_no_credentials

    for name in ("paper-risk-policy-v2.json", "compounder-monthly-v2-paper-authorization.json"):
        check_no_credentials(json.loads((ARTEFACTS / name).read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# §11-§13 the fractionable deployment universe
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_deployment_universe_admits_only_routable_fractionable_identities() -> None:
    derived = universe_of(fractionable=(1, 2), whole_share=(3,))

    assert derived.members == (("COMPANY", 1), ("COMPANY", 2))


@pytest.mark.integration
def test_a_whole_share_only_identity_is_excluded_with_its_own_reason() -> None:
    derived = universe_of(fractionable=(1,), whole_share=(2,))

    assert derived.exclusion_for(identity(2)) == UniverseExclusion.WHOLE_SHARE_ONLY


@pytest.mark.integration
def test_an_identity_with_unstated_fractionability_is_not_reported_as_whole_share_only() -> None:
    """An unknown capability is not a stated absence of one.

    Two different facts about the venue: it told us the instrument needs whole
    shares, or it told us nothing. Collapsing them would make a gap in the
    evidence look like a property of the instrument.
    """
    mapping = mapping_for(
        identities={1: "AAA"},
        assets={"AAA": provider_asset("AAA", fractionable=None)},
    )
    derived = fractionable_universe(mapping)

    assert derived.exclusion_for(identity(1)) == Routability.FRACTIONABILITY_UNKNOWN


@pytest.mark.integration
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"status": "inactive"}, Routability.INACTIVE),
        ({"tradable": False}, Routability.NOT_TRADABLE),
        ({"fractionable": None}, Routability.FRACTIONABILITY_UNKNOWN),
        ({"fractionable": False}, UniverseExclusion.WHOLE_SHARE_ONLY),
    ],
)
def test_each_venue_condition_produces_its_own_distinct_exclusion_reason(
    overrides: dict[str, Any], expected: str
) -> None:
    mapping = mapping_for(
        identities={1: "AAA"},
        assets={"AAA": provider_asset("AAA", **overrides)},
    )

    assert fractionable_universe(mapping).exclusion_for(identity(1)) == expected


@pytest.mark.integration
def test_an_unmapped_identity_is_excluded_as_unmapped() -> None:
    mapping = mapping_for(identities={1: "AAA"}, assets={})

    assert fractionable_universe(mapping).exclusion_for(identity(1)) == Routability.UNMAPPED


@pytest.mark.integration
def test_an_ambiguous_symbol_excludes_both_identities() -> None:
    """Two identities sharing a symbol is the case a ticker-keyed universe
    silently merges."""
    mapping = mapping_for(identities={1: "AAA", 2: "AAA"}, assets={"AAA": provider_asset("AAA")})
    derived = fractionable_universe(mapping)

    assert derived.members == ()
    assert derived.by_reason() == {Routability.AMBIGUOUS: 2}


@pytest.mark.integration
def test_every_considered_identity_is_either_a_member_or_an_exclusion() -> None:
    """Nothing is dropped silently. A universe that lost an identity without
    recording why would make its own size unexplainable."""
    derived = universe_of(fractionable=(1, 2, 3), whole_share=(4, 5))

    assert derived.size + len(derived.exclusions) == 5


@pytest.mark.integration
def test_the_deployment_universe_body_contains_no_symbol() -> None:
    """Membership is identity, never ticker.

    A symbol is reassigned when a company delists and another takes it, so a
    universe keyed on symbols would admit the successor. Asserted over the
    serialised body because that is the form that gets persisted and reloaded.
    """
    derived = universe_of(fractionable=(1, 2), whole_share=(3,))
    rendered = json.dumps(derived.as_json())

    assert "T01" not in rendered
    assert "T02" not in rendered
    assert "T03" not in rendered


@pytest.mark.integration
def test_the_deployment_universe_binds_the_mapping_it_was_derived_from() -> None:
    mapping = universe_mapping()
    derived = fractionable_universe(mapping)

    assert derived.mapping_fingerprint == mapping.fingerprint()


@pytest.mark.integration
def test_two_universes_swapping_one_member_fingerprint_differently() -> None:
    """A digest over counts alone would collide here, which is why membership is
    in the body."""
    left = universe_of(fractionable=(1, 2), whole_share=(3,))
    right = universe_of(fractionable=(1, 3), whole_share=(2,))

    assert left.size == right.size
    assert left.fingerprint() != right.fingerprint()


@pytest.mark.integration
def test_the_persisted_universe_artefact_reproduces_from_the_authorised_mapping() -> None:
    """The stored artefact is evidence-derived, and its arithmetic closes.

    The full provider payload is not committed, so this checks what can be
    checked offline: the recorded size, the exclusion counts and the considered
    total have to be mutually consistent and to agree with the routability
    counts of the mapping the universe names. 1,821 is read from the artefact
    rather than written into the test.
    """
    stored = json.loads(
        (ARTEFACTS / "alphatrader-paper-fractionable-universe.json").read_text(encoding="utf-8")
    )
    mapping = json.loads(
        (ARTEFACTS / "alpaca-paper-instrument-mapping.json").read_text(encoding="utf-8")
    )
    counts = stored["excluded_counts"]

    assert stored["version"] == DEPLOYMENT_UNIVERSE_VERSION
    assert stored["mapping_fingerprint"] == mapping["mapping_fingerprint"]
    assert stored["considered"] == mapping["candidate_identities"]
    assert stored["size"] == len(stored["members"])
    assert stored["size"] + sum(counts.values()) == stored["considered"]
    # Every non-routable identity in the mapping is excluded here for exactly
    # the same reason, and the only reason this contract adds is the capability.
    assert (
        counts[UniverseExclusion.WHOLE_SHARE_ONLY] == mapping["counts"]["ROUTABLE"] - stored["size"]
    )


@pytest.mark.integration
def test_the_persisted_universe_artefact_holds_no_symbol() -> None:
    rendered = (ARTEFACTS / "alphatrader-paper-fractionable-universe.json").read_text(
        encoding="utf-8"
    )
    stored = json.loads(rendered)

    assert all(len(row) == 2 and row[0] == "COMPANY" for row in stored["members"])
    assert "internal_symbol" not in rendered
    assert "provider_symbol" not in rendered


@pytest.mark.integration
def test_the_deployment_universe_is_derived_and_has_no_authoring_entry_point() -> None:
    """There is no way to add a member by hand.

    The only constructor that produces membership reads a mapping. A universe
    somebody can edit is a universe that stops describing the venue, so this
    asserts the absence of an override rather than trusting that nobody adds one.
    """
    from stock_screener.paper_runtime import universe as module

    exported = set(module.__all__)
    constructors = {name for name in exported if name.islower()}

    assert constructors == {"fractionable_universe"}


@pytest.mark.integration
def test_a_universe_rejects_an_identity_it_never_considered() -> None:
    derived = universe_of(fractionable=(1,))

    assert derived.contains(identity(99)) is False
    assert derived.exclusion_for(identity(99)) == Routability.UNMAPPED


# --------------------------------------------------------------------------- #
# §14-§22, §32 the V2 strategy
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_v2_selects_only_from_the_deployment_universe(calendar: ExecutionCalendar) -> None:
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    decision = CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(3, 21)))).decide(
        offered, session=SESSION, calendar=calendar
    )

    assert [row.identity.identity_id for row in decision.selections] == [3, 4]


@pytest.mark.integration
def test_v2_counts_the_decile_after_the_intersection(calendar: ExecutionCalendar) -> None:
    """The decile is a decile of what can be held.

    Nine deployable names must ask for one, not ask for two and then drop one.
    Taking the decile first and trimming afterwards would make the position
    count depend on how many undeployable names happened to rank above the line.
    """
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    deployable = FakeUniverse(set(range(12, 21)))
    decision = CompounderMonthlyFractionableStrategy(deployable).decide(
        offered, session=SESSION, calendar=calendar
    )

    assert decision.eligible_count == 9
    assert decision.target_count == 1


@pytest.mark.integration
def test_v2_records_a_deployment_exclusion_with_its_own_reason(
    calendar: ExecutionCalendar,
) -> None:
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    decision = CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(3, 21)))).decide(
        offered, session=SESSION, calendar=calendar
    )

    assert decision.excluded_counts[DEPLOYMENT_EXCLUSION_REASON] == 2


@pytest.mark.integration
def test_v2_does_not_record_an_eligible_company_as_failing_research(
    calendar: ExecutionCalendar,
) -> None:
    """A venue capability is never reported as a research judgement.

    The company passed `is_rank_eligible`. Folding it into `INSUFFICIENT_DATA`
    or a freshness reason would put a false statement about the business into
    the decision record.
    """
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    decision = CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(3, 21)))).decide(
        offered, session=SESSION, calendar=calendar
    )
    research_reasons = {
        key for key in decision.excluded_counts if key != DEPLOYMENT_EXCLUSION_REASON
    }

    assert research_reasons == set()


@pytest.mark.integration
def test_v2_keeps_a_research_exclusion_reason_for_a_company_that_also_fails_deployment(
    calendar: ExecutionCalendar,
) -> None:
    """A company failing both is reported for the research reason.

    It never reached eligibility, so the venue was never the binding constraint
    and claiming it was would overstate how much the restriction costs.
    """
    offered = bundle(
        *(record(number, 100.0 - number) for number in range(1, 11)),
        record(50, None, status="INSUFFICIENT_DATA"),
    )
    decision = CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(1, 11)))).decide(
        offered, session=SESSION, calendar=calendar
    )

    assert decision.excluded_counts == {"INSUFFICIENT_DATA": 1}


@pytest.mark.integration
def test_v2_reports_everything_offered_as_considered(calendar: ExecutionCalendar) -> None:
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    decision = CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(3, 21)))).decide(
        offered, session=SESSION, calendar=calendar
    )

    assert decision.considered_count == 20


@pytest.mark.integration
def test_v2_binds_the_deployment_universe_into_its_decision_fingerprint(
    calendar: ExecutionCalendar,
) -> None:
    """Two decisions over the same evidence and different venue capabilities
    must not digest alike."""
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    first = CompounderMonthlyFractionableStrategy(
        FakeUniverse(set(range(1, 21)), digest="a" * 64)
    ).decide(offered, session=SESSION, calendar=calendar)
    second = CompounderMonthlyFractionableStrategy(
        FakeUniverse(set(range(1, 21)), digest="b" * 64)
    ).decide(offered, session=SESSION, calendar=calendar)

    assert first.selections == second.selections
    assert first.fingerprint() != second.fingerprint()


@pytest.mark.integration
def test_v2_stamps_its_own_version_and_fingerprint(calendar: ExecutionCalendar) -> None:
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    strategy = CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(1, 21))))
    decision = strategy.decide(offered, session=SESSION, calendar=calendar)

    assert decision.strategy_version == STRATEGY_VERSION_V2
    assert decision.strategy_fingerprint == strategy.fingerprint()


@pytest.mark.integration
def test_v2_selects_exactly_what_v1_selects_when_everything_is_deployable(
    calendar: ExecutionCalendar,
) -> None:
    """The restriction is the only difference.

    With a universe that excludes nothing, V2 must reproduce V1's selection,
    weights, ranks, target count and exits. This is what makes "all other
    semantics identical" checkable rather than asserted in a docstring.
    """
    offered = bundle(
        *(record(number, 100.0 - number) for number in range(1, 21)),
        record(50, None, status="INSUFFICIENT_DATA"),
    )
    first = CompounderMonthlyStrategy().decide(offered, session=SESSION, calendar=calendar)
    second = CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(1, 51)))).decide(
        offered, session=SESSION, calendar=calendar
    )

    assert second.selections == first.selections
    assert second.target_count == first.target_count
    assert second.eligible_count == first.eligible_count
    assert second.considered_count == first.considered_count
    assert second.excluded_counts == first.excluded_counts
    assert second.exits == first.exits


@pytest.mark.integration
def test_v2_policy_differs_from_v1_only_in_its_declared_deployment_keys() -> None:
    """Every rule V2 does not change is V1's rule, by construction."""
    first = CompounderMonthlyStrategy().policy()
    second = CompounderMonthlyFractionableStrategy(FakeUniverse({1})).policy()
    moved = {key for key in first if first[key] != second.get(key)}
    added = set(second) - set(first)

    assert moved == {"strategy_version", "sizing_authority"}
    assert added == {
        "deployment_universe",
        "selection_population",
        "deployment_exclusion_reason",
        "deployment_restriction_placement",
    }


@pytest.mark.integration
def test_v2_declares_the_restriction_in_strategy_authority() -> None:
    policy_body = CompounderMonthlyFractionableStrategy(FakeUniverse({1})).policy()

    assert policy_body["deployment_restriction_placement"].startswith("STRATEGY_AUTHORITY")


@pytest.mark.integration
def test_v2_authorises_paper_only_and_claims_no_efficacy() -> None:
    policy_body = CompounderMonthlyFractionableStrategy(FakeUniverse({1})).policy()

    assert policy_body["environments_authorised"] == ["EXTERNAL_PAPER"]
    assert policy_body["live_trading"] == "NOT_IMPLEMENTED_NOT_AUTHORIZED"
    assert policy_body["efficacy_claim"].startswith("NONE")


@pytest.mark.integration
def test_v2_still_emits_no_order_and_sizes_nothing() -> None:
    policy_body = CompounderMonthlyFractionableStrategy(FakeUniverse({1})).policy()

    assert policy_body["emits_orders"] is False
    assert policy_body["reads_portfolio_cash"] is False
    assert policy_body["reads_broker_state"] is False


@pytest.mark.integration
def test_v2_refuses_evidence_published_after_the_decision_even_when_undeployable(
    calendar: ExecutionCalendar,
) -> None:
    """Narrowing the universe is not a way to drop inadmissible evidence.

    The offending record is excluded for deployment, so a V2 that narrowed
    before checking would have decided happily on a bundle V1 refuses.
    """
    after_close = "2026-10-30T13:00:00-08:00"
    offered = bundle(
        *(record(number, 100.0 - number) for number in range(1, 11)),
        record(50, 5.0, calculated_at=after_close),
    )

    with pytest.raises(StrategyError, match="after"):
        CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(1, 11)))).decide(
            offered, session=SESSION, calendar=calendar
        )


@pytest.mark.integration
def test_v2_makes_no_selection_outside_its_monthly_decision_session(
    calendar: ExecutionCalendar,
) -> None:
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))

    with pytest.raises(StrategyError, match="not the last session") as caught:
        CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(1, 21)))).decide(
            offered, session="2026-10-29", calendar=calendar
        )

    assert caught.value.code is StrategyCode.STRATEGY_NO_DECISION_DUE


@pytest.mark.integration
def test_v2_refuses_a_session_the_calendar_does_not_hold(
    calendar: ExecutionCalendar,
) -> None:
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))

    with pytest.raises(StrategyError, match="not a session"):
        CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(1, 21)))).decide(
            offered, session="2026-10-31", calendar=calendar
        )


@pytest.mark.integration
def test_v2_refuses_intents_from_a_v1_decision(calendar: ExecutionCalendar) -> None:
    """Intents are never re-derived across versions."""
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    first = CompounderMonthlyStrategy().decide(offered, session=SESSION, calendar=calendar)

    with pytest.raises(StrategyError, match="never re-derived across versions"):
        CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(1, 21)))).intents(first)


@pytest.mark.integration
def test_v2_intents_carry_its_own_version_and_no_quantity(
    calendar: ExecutionCalendar,
) -> None:
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    strategy = CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(3, 21))))
    produced = strategy.intents(strategy.decide(offered, session=SESSION, calendar=calendar))

    assert {row.strategy_version for row in produced} == {STRATEGY_VERSION_V2}
    assert all(row.requested_notional is None for row in produced)


@pytest.mark.integration
def test_v2_names_the_excluded_identities_for_an_operator(
    calendar: ExecutionCalendar,
) -> None:
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    strategy = CompounderMonthlyFractionableStrategy(FakeUniverse(set(range(3, 21))))
    reported = strategy.deployment_exclusions(offered)

    assert [row[0].identity_id for row in reported] == [1, 2]
    assert {row[1] for row in reported} == {DEPLOYMENT_EXCLUSION_REASON}


@pytest.mark.integration
def test_v2_drives_the_real_derived_universe_end_to_end(
    calendar: ExecutionCalendar,
) -> None:
    """The strategy and the derived universe are joined, not merely declared.

    A real `DeploymentUniverse` built from provider evidence, handed to the real
    strategy. This is the test that would fail if the two sides had been written
    to fit each other on paper and never composed.
    """
    derived = universe_of(fractionable=(2, 3), whole_share=(1,))
    offered = bundle(*(record(number, 100.0 - number) for number in (1, 2, 3)))
    decision = CompounderMonthlyFractionableStrategy(derived).decide(
        offered, session=SESSION, calendar=calendar
    )

    assert isinstance(derived, DeploymentUniverse)
    assert [row.identity.identity_id for row in decision.selections] == [2]
    assert decision.excluded_counts[DEPLOYMENT_EXCLUSION_REASON] == 1
    assert decision.deployment_universe_fingerprint == derived.fingerprint()


# --------------------------------------------------------------------------- #
# §14 V1 is untouched
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_v1_decision_digests_exactly_what_it_always_did(
    calendar: ExecutionCalendar,
) -> None:
    """No key was added to a V1 decision's fingerprinted body.

    The deployment-universe binding is present only when a strategy restricts
    one. Adding it unconditionally would have changed every recorded V1
    decision fingerprint to record that nothing about V1 had moved.
    """
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    decision = CompounderMonthlyStrategy().decide(offered, session=SESSION, calendar=calendar)

    assert decision.deployment_universe_fingerprint == ""
    assert "deployment_universe_fingerprint" not in decision.body()


@pytest.mark.integration
def test_v1_still_selects_without_any_deployment_universe(
    calendar: ExecutionCalendar,
) -> None:
    offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
    decision = CompounderMonthlyStrategy().decide(offered, session=SESSION, calendar=calendar)

    assert decision.strategy_version == STRATEGY_VERSION
    assert [row.identity.identity_id for row in decision.selections] == [1, 2]


# --------------------------------------------------------------------------- #
# §20 the V2 deployment grant
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_v2_grant_covers_v2_exactly() -> None:
    strategy = CompounderMonthlyFractionableStrategy(FakeUniverse({1}))
    grant = grant_for(strategy)

    grant.require(
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        fingerprint=strategy.fingerprint(),
        environment="EXTERNAL_PAPER",
    )


@pytest.mark.integration
def test_a_v1_grant_does_not_cover_v2() -> None:
    """A grant approves one exact policy. V2 selects a different set of
    companies from the same evidence, so V1's approval cannot carry it."""
    first = CompounderMonthlyStrategy()
    second = CompounderMonthlyFractionableStrategy(FakeUniverse({1}))

    with pytest.raises(StrategyError, match=r"NOT_AUTHORIZED|does not"):
        grant_for(first).require(
            strategy_id=second.strategy_id,
            strategy_version=second.strategy_version,
            fingerprint=second.fingerprint(),
            environment="EXTERNAL_PAPER",
        )


@pytest.mark.integration
def test_the_v2_grant_refuses_a_live_environment() -> None:
    strategy = CompounderMonthlyFractionableStrategy(FakeUniverse({1}))

    with pytest.raises(StrategyError, match="not an authorisable environment"):
        grant_for(strategy, environment="EXTERNAL_LIVE")


@pytest.mark.integration
def test_the_persisted_v2_grant_names_the_current_v2_fingerprint() -> None:
    """The committed grant is the one this build would ask for.

    If a rule in V2's policy changed without the artefact being reissued, the
    grant on disk would silently stop covering the strategy — so the two are
    compared rather than trusted.
    """
    stored = json.loads(
        (ARTEFACTS / "compounder-monthly-v2-paper-authorization.json").read_text(encoding="utf-8")
    )
    strategy = CompounderMonthlyFractionableStrategy(FakeUniverse({1}))

    assert stored["strategy_version"] == STRATEGY_VERSION_V2
    assert stored["strategy_fingerprint"] == strategy.fingerprint()
    assert stored["environment"] == "EXTERNAL_PAPER"
    assert stored["live_trading"] == "NOT_IMPLEMENTED_NOT_AUTHORIZED"


# --------------------------------------------------------------------------- #
# §38-§44 the strategy-revision transition
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_strategy_revision_preserves_every_lineage(paper_runtime: Any) -> None:
    """The books survive a change of strategy.

    All three generations and the bootstrap epoch carry across. A revision that
    restarted any of them would orphan the accounting history that explains the
    positions currently held.
    """
    before = paper_runtime.binding
    after = revise(paper_runtime)

    for field in IMMUTABLE_ACROSS_REBIND:
        if field in STRATEGY_REVISION_FIELDS:
            continue
        assert getattr(after, field) == getattr(before, field), field


@pytest.mark.integration
def test_a_strategy_revision_moves_the_strategy_fingerprint(paper_runtime: Any) -> None:
    after = revise(paper_runtime)
    strategy = CompounderMonthlyFractionableStrategy(FakeUniverse({1}))

    assert after.strategy_fingerprint == strategy.fingerprint()
    assert after.strategy_version == STRATEGY_VERSION_V2


@pytest.mark.integration
def test_a_strategy_revision_records_which_transition_it_was(paper_runtime: Any) -> None:
    """An auditor reads the operation, never infers it from which fields moved."""
    after = revise(paper_runtime)

    assert after.transition == STRATEGY_REVISION_VERSION
    assert after.body()["transition"] == STRATEGY_REVISION_VERSION


@pytest.mark.integration
def test_a_plain_rebind_still_refuses_to_change_a_strategy(paper_runtime: Any) -> None:
    """The invariant is conditional on a declared transition, not weakened.

    A lineage-preserving rebind passes no exemption, so it must still refuse
    every field a revision is allowed to move. This is the test that would fail
    if the exemption had been implemented by deleting fields from
    `IMMUTABLE_ACROSS_REBIND`.
    """
    from stock_screener.paper_runtime.rebind import _require_only_deployment_changed

    revised = revise(paper_runtime)

    with pytest.raises(RuntimeBindingError, match="may not change") as caught:
        _require_only_deployment_changed(paper_runtime.binding, revised)

    assert caught.value.code is Code.RUNTIME_REBIND_REFUSED
    assert "strategy_fingerprint" in str(caught.value)


@pytest.mark.integration
def test_a_rebind_exempts_nothing_by_default() -> None:
    """Read off the signature: the default must be the empty exemption."""
    import inspect

    from stock_screener.paper_runtime.rebind import _require_only_deployment_changed

    signature = inspect.signature(_require_only_deployment_changed)

    assert signature.parameters["allowed"].default == ()


@pytest.mark.integration
def test_a_strategy_revision_without_confirmation_is_refused(paper_runtime: Any) -> None:
    with pytest.raises(RuntimeBindingError, match="requires explicit confirmation") as caught:
        revise(paper_runtime, confirmed=False)

    assert caught.value.code is Code.RUNTIME_REBIND_REFUSED


@pytest.mark.integration
def test_a_strategy_revision_refuses_a_grant_for_another_strategy(
    paper_runtime: Any,
) -> None:
    """The predecessor's approval cannot be stretched over the revision."""
    with pytest.raises(StrategyError, match=r"NOT_AUTHORIZED|does not"):
        revise(paper_runtime, authorization=grant_for(CompounderMonthlyStrategy()))


@pytest.mark.integration
def test_a_strategy_revision_refuses_an_unauthorised_risk_policy(
    paper_runtime: Any,
) -> None:
    from dataclasses import replace

    candidate = replace(paper_risk_policy_v2(), authorized_for_paper=False)

    with pytest.raises(RuntimeBindingError, match="not authorised for paper"):
        revise(paper_runtime, risk_policy=candidate)


@pytest.mark.integration
def test_a_strategy_revision_refuses_a_policy_claiming_live_authorisation(
    paper_runtime: Any,
) -> None:
    from dataclasses import replace

    claiming = replace(paper_risk_policy_v2(), authorized_for_live=True)

    with pytest.raises(RuntimeBindingError, match="NOT_IMPLEMENTED"):
        revise(paper_runtime, risk_policy=claiming)


@pytest.mark.integration
def test_a_strategy_revision_refuses_a_policy_for_another_environment(
    paper_runtime: Any,
) -> None:
    from dataclasses import replace

    other = replace(paper_risk_policy_v2(), environment="SIMULATED")

    with pytest.raises(RuntimeBindingError, match="binds EXTERNAL_PAPER only"):
        revise(paper_runtime, risk_policy=other)


@pytest.mark.integration
def test_a_strategy_revision_refuses_to_change_the_strategy_identity(
    paper_runtime: Any,
) -> None:
    """A different strategy deciding for these books is a new runtime.

    Its decisions would share an accounting lineage with decisions it never
    made, which is a different and unreviewed operation.
    """

    class OtherStrategy(CompounderMonthlyFractionableStrategy):
        strategy_id = "SOMETHING_ELSE"

    other = OtherStrategy(FakeUniverse({1}))

    with pytest.raises(RuntimeBindingError, match="never its identity") as caught:
        revise(paper_runtime, strategy=other, authorization=grant_for(other))

    assert caught.value.code is Code.RUNTIME_REBIND_REFUSED


@pytest.mark.integration
def test_a_strategy_revision_to_the_bound_fingerprint_is_refused(
    paper_runtime: Any,
) -> None:
    """A transition with no subject is not recorded.

    An entry in the chain reads as a reviewed change, so writing one for a
    revision that changed nothing would put a reviewed-looking record in front
    of a review that had nothing to look at.
    """
    revise(paper_runtime)

    with pytest.raises(RuntimeBindingError, match="nothing to revise"):
        revise(paper_runtime)


@pytest.mark.integration
def test_a_strategy_revision_is_refused_while_a_decision_awaits_execution(
    paper_runtime: Any, calendar: ExecutionCalendar
) -> None:
    """A pending decision blocks the revision.

    Revising underneath it would leave a decision in the store that the bound
    strategy did not make and cannot be re-derived from — neither executable
    nor explainable.
    """

    class PendingStore:
        """A decision store holding one non-terminal decision."""

        def pending(self) -> Any:
            offered = bundle(*(record(number, 100.0 - number) for number in range(1, 21)))
            decision = CompounderMonthlyStrategy().decide(
                offered, session=SESSION, calendar=calendar
            )
            return type("Row", (), {"decision": decision, "status": "PENDING_EXECUTION"})()

    with pytest.raises(RuntimeBindingError, match="has not reached a terminal state") as caught:
        revise(paper_runtime, decisions=PendingStore())

    assert caught.value.code is Code.RUNTIME_REBIND_PRECONDITION_FAILED


@pytest.mark.integration
def test_a_strategy_revision_proceeds_when_no_decision_is_pending(
    paper_runtime: Any,
) -> None:
    class EmptyStore:
        """A decision store with nothing outstanding."""

        def pending(self) -> Any:
            return None

    assert revise(paper_runtime, decisions=EmptyStore()).transition == STRATEGY_REVISION_VERSION


@pytest.mark.integration
def test_a_strategy_revision_appends_to_the_binding_chain(paper_runtime: Any) -> None:
    revise(paper_runtime)
    history = RuntimeBindingHistory(paper_runtime.root)

    assert [row.sequence for row in history.records()] == [1]
    assert history.verify()["chain_verified"] is True


@pytest.mark.integration
def test_a_persisted_revision_reloads_with_its_transition_intact(paper_runtime: Any) -> None:
    """The transition survives the round trip, and the chain still verifies.

    Written as a reload rather than an assertion on the in-memory object,
    because the defect this catches is the one that actually happened: the
    field was added to the write path and not the read path, so the reloaded
    binding digested to a different body and the chain stopped verifying. Any
    field added to a chained record has to make this trip.
    """
    revise(paper_runtime)
    reloaded = active_binding(paper_runtime.root)

    assert reloaded.transition == STRATEGY_REVISION_VERSION
    assert reloaded.strategy_version == STRATEGY_VERSION_V2
    assert RuntimeBindingHistory(paper_runtime.root).verify()["chain_verified"] is True


@pytest.mark.integration
def test_a_reloaded_revision_fingerprints_identically_to_the_one_written(
    paper_runtime: Any,
) -> None:
    written = revise(paper_runtime)

    assert active_binding(paper_runtime.root).fingerprint() == written.fingerprint()


@pytest.mark.integration
def test_a_strategy_revision_writes_no_opening_balance_and_reads_no_cash() -> None:
    """A revision is not a bootstrap, asserted on the code rather than the run.

    Parsed, because the module documents in prose exactly what it must not do,
    and a text scan would match its own docstrings.
    """
    source = REPO_ROOT / "src" / "stock_screener" / "paper_runtime" / "rebind.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    revision = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "revise_strategy"
    )
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(revision)
        if isinstance(node, ast.Call)
    }

    assert "OpeningBalanceEvent" not in called
    assert "bootstrap_paper_book" not in called
    assert not {name for name in called if "cash" in name.lower()}


@pytest.mark.integration
def test_the_strategy_revision_exempts_only_the_three_strategy_fields() -> None:
    """The exemption cannot quietly grow to a generation or the epoch."""
    assert STRATEGY_REVISION_FIELDS == (
        "strategy_version",
        "strategy_fingerprint",
        "strategy_authorization_fingerprint",
    )
    assert "strategy_id" not in STRATEGY_REVISION_FIELDS
    assert not {name for name in STRATEGY_REVISION_FIELDS if "generation" in name}
    assert "bootstrap_epoch" not in STRATEGY_REVISION_FIELDS


@pytest.mark.integration
def test_a_rebind_and_a_revision_are_different_operations(paper_runtime: Any) -> None:
    """Two named transitions, distinguishable in the record."""
    from stock_screener.paper_runtime.rebind import REBIND_CHAIN_VERSION

    rebound = rebind_runtime(
        root=paper_runtime.root,
        account_binding_fingerprint="c" * 64,
        risk_policy=paper_risk_policy_v2(),
        mapping=universe_mapping(),
        reason="rebind deployment inputs only",
        now=NOW,
        confirmed=True,
    )

    assert rebound.transition == REBIND_CHAIN_VERSION
    assert rebound.transition != STRATEGY_REVISION_VERSION


# --------------------------------------------------------------------------- #
# §45-§47 what this phase did not do
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_no_v3_contract_was_created() -> None:
    """V3 of anything is out of scope for this phase."""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "src" / "stock_screener").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for marker in (
            "PORTFOLIO_RISK_ENGINE_V1_4",
            "PAPER_PORTFOLIO_RISK_POLICY_V3",
            "ALPHATRADER_COMPOUNDER_MONTHLY_V3",
        ):
            if marker in text:
                offenders.append(f"{path.name}: {marker}")

    assert offenders == []


@pytest.mark.integration
def test_nothing_added_here_can_authorise_live_trading() -> None:
    policy_body = CompounderMonthlyFractionableStrategy(FakeUniverse({1})).policy()
    risk = paper_risk_policy_v2()

    assert policy_body["live_trading"] == "NOT_IMPLEMENTED_NOT_AUTHORIZED"
    assert risk.as_json()["live_trading" if "live_trading" in risk.as_json() else "environment"]
    assert risk.authorized_for_live is False
    assert risk.engine_policy().production_authorized is False


@pytest.mark.integration
def test_the_new_modules_read_no_environment_variable_directly() -> None:
    """Configuration arrives through typed settings, never `os.environ`."""
    offenders: list[str] = []
    for name in (
        REPO_ROOT / "src" / "stock_screener" / "paper_runtime" / "universe.py",
        REPO_ROOT / "src" / "stock_screener" / "strategies" / "compounder_monthly_fractionable.py",
    ):
        tree = ast.parse(name.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "environ":
                offenders.append(f"{name.name}:{node.lineno}")

    assert offenders == []
