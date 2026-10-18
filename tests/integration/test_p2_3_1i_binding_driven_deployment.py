"""P2.3.1I — the runtime binding decides which strategy runs, and where it may reach.

P2.3.1H measured the gap these tests close. Production constructed
`CompounderMonthlyStrategy` in eleven places and then asked the binding whether
it agreed, which has one safe outcome (refusal, and an unusable runtime) and one
unsafe one (V1 deciding under V2's label). The binding is now the authority and
this module's first job is to prove that by *driving* the production composition
rather than reading its source.

The second job is the capability contradiction. The owner's V2 deployment routes
only to instruments the venue states are fractionable, but
`BrokerCapabilities.fractional_quantities` is account-wide and the account holds
whole-share-only securities. The tests below pin the resolution: the claim is
scoped to the deployment, the account-wide claim stays false, the full mapping
goes on recording the instruments that make it false, and both the aggregate
proof and the per-instrument check stay in force — because an aggregate is not a
licence.

Nothing here touches the real runtime root, arms anything, or reaches a venue.
"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from stock_screener.paper_runtime import (
    DEPLOYABLE_STRATEGIES,
    DEPLOYMENT_CAPABILITY_VERSION,
    DEPLOYMENT_UNIVERSE_VERSION,
    IMMUTABLE_ACROSS_REBIND,
    RUNTIME_BINDING_V3_VERSION,
    STRATEGY_REVISION_FIELDS,
    V3_DEPLOYMENT_FIELDS,
    Code,
    PaperOperationsRuntimeBindingV3,
    RuntimeBindingError,
    RuntimeBindingHistory,
    active_binding,
    deployment_capability,
    fractionable_universe,
    mapping_from_evidence,
    paper_risk_policy_v2,
    read_deployment_capability,
    read_deployment_universe,
    resolve_strategy,
    revise_deployment,
    write_deployment_capability,
    write_deployment_universe,
    write_risk_policy,
    write_strategy_authorization,
    write_symbol_mapping,
)
from stock_screener.paper_runtime.composition import compose_operations, composition_readiness
from stock_screener.paper_runtime.operator_inputs import (
    DEPLOYMENT_CAPABILITY_FILENAME,
    DEPLOYMENT_UNIVERSE_FILENAME,
    OPERATOR_INPUT_FILES,
    REQUIRED_OPERATOR_INPUTS,
)
from stock_screener.strategies import (
    DEPLOYMENT_EXCLUSION_REASON,
    STRATEGY_VERSION,
    STRATEGY_VERSION_V2,
    CompounderMonthlyFractionableStrategy,
    CompounderMonthlyStrategy,
    StrategyDeploymentAuthorizationV2,
)

pytestmark = pytest.mark.integration

PAPER = "https://paper-api.alpaca.markets"
ACCOUNT_ID = "paper-account-1"
NOW = "2026-11-02T14:35:00+00:00"
DECISION_NOW = "2026-10-30T20:00:30+00:00"
EXECUTION_NOW = "2026-11-02T14:35:00+00:00"

#: The repository root. Absolute, because `tests/conftest.py` moves every test
#: to a temporary working directory — a relative `.operations` would resolve to
#: an empty temp dir and the real-runtime assertions would skip themselves into
#: being worthless.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Three identities: two fractionable and one whole-share-only. The third is the
#: whole point — it stays in the full mapping and out of the deployment, which is
#: exactly the 59-of-1,880 shape the real account has.
FRACTIONABLE: dict[int, str] = {1: "AAA", 2: "BBB"}
WHOLE_SHARE: dict[int, str] = {3: "CCC"}
ALL_SYMBOLS: dict[int, str] = {**FRACTIONABLE, **WHOLE_SHARE}


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


@dataclass
class FakeSettings:
    """Typed settings, as the CLI reads them. No environment is touched."""

    database_url: str = "sqlite:///:memory:"
    alpaca_paper_base_url: str = PAPER
    alpaca_paper_account_id: str = ACCOUNT_ID
    alpaca_paper_key_id: Any = None
    alpaca_paper_secret_key: Any = None

    def __post_init__(self) -> None:
        """Attach the masked credentials."""
        self.alpaca_paper_key_id = SecretStr("PKTESTKEY01")
        self.alpaca_paper_secret_key = SecretStr("shhh-secret-value")


@dataclass
class ScriptedVenue:
    """A scripted Alpaca PAPER endpoint under the real HTTPS transport."""

    orders: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    cash: str = "100000.00"

    @property
    def mutations(self) -> list[dict[str, Any]]:
        """Every mutating request that reached the network."""
        return [row for row in self.calls if row["method"] in {"POST", "PUT", "PATCH", "DELETE"}]

    def client(self) -> httpx.Client:
        """An HTTP client whose network is this script."""
        return httpx.Client(
            base_url=PAPER, transport=httpx.MockTransport(self._handle), timeout=5.0
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request."""
        path = request.url.path
        method = request.method
        body = json.loads(request.content) if request.content else {}
        self.calls.append({"method": method, "path": path, "json": body})
        if path == "/v2/account":
            return httpx.Response(
                200,
                json={
                    "id": ACCOUNT_ID,
                    "account_number": "PA123",
                    "status": "ACTIVE",
                    "cash": self.cash,
                    "equity": self.cash,
                    "buying_power": self.cash,
                    "portfolio_value": self.cash,
                    "trading_blocked": False,
                    "account_blocked": False,
                    "pattern_day_trader": False,
                },
            )
        if path == "/v2/positions":
            return httpx.Response(200, json=[])
        if path == "/v2/orders" and method == "POST":
            order = {
                "id": f"venue-{len(self.orders) + 1}",
                "client_order_id": body.get("client_order_id", ""),
                "symbol": body.get("symbol", ""),
                "qty": str(body.get("qty", "0")),
                "filled_qty": str(body.get("qty", "0")),
                "filled_avg_price": "100.00",
                "side": body.get("side", "buy"),
                "status": "filled",
                "submitted_at": NOW,
                "filled_at": NOW,
            }
            self.orders.append(order)
            return httpx.Response(200, json=order)
        if path == "/v2/orders":
            return httpx.Response(200, json=list(self.orders))
        if path.startswith("/v2/account/activities"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": f"act-{index}",
                        "activity_type": "FILL",
                        "order_id": row["id"],
                        "symbol": row["symbol"],
                        "side": row["side"],
                        "qty": row["qty"],
                        "price": "100.00",
                        "transaction_time": NOW,
                    }
                    for index, row in enumerate(self.orders, start=1)
                ],
            )
        if path == "/v2/orders:by_client_order_id":
            wanted = request.url.params.get("client_order_id")
            for row in self.orders:
                if row["client_order_id"] == wanted:
                    return httpx.Response(200, json=row)
            return httpx.Response(404, json={"message": "order not found"})
        return httpx.Response(200, json={})


def provider_asset(symbol: str, **overrides: Any) -> dict[str, Any]:
    """One provider asset payload."""
    asset: dict[str, Any] = {
        "id": f"asset-{symbol.lower()}",
        "symbol": symbol,
        "class": "us_equity",
        "exchange": "NASDAQ",
        "status": "active",
        "tradable": True,
        "fractionable": symbol not in set(WHOLE_SHARE.values()),
    }
    asset.update(overrides)
    return asset


def full_mapping(**overrides: Any) -> Any:
    """The full reviewed mapping: fractionable names *and* whole-share names."""
    identities = dict(overrides.pop("identities", ALL_SYMBOLS))
    assets = {
        value: provider_asset(value, **overrides.pop(value, {})) for value in identities.values()
    }
    return mapping_from_evidence(
        identities=identities,
        provider_assets=assets,
        universe="TEST",
        universe_as_of="2026-08-26",
        evidence_at=NOW,
        provider_evidence_fingerprint="e" * 64,
    )


def v2_strategy(universe: Any) -> CompounderMonthlyFractionableStrategy:
    """The reviewed V2 strategy over a universe."""
    return CompounderMonthlyFractionableStrategy(universe)


def grant_for(
    strategy: Any, universe: Any = None, **overrides: Any
) -> StrategyDeploymentAuthorizationV2:
    """A paper deployment grant covering exactly this strategy and universe.

    V2 of the authorization contract: P2.3.1I2 made the deployment universe part
    of the granted authority, so a grant for a universe-restricted strategy has
    to name the universe it approves.
    """
    settings: dict[str, Any] = {
        "authorization_id": "TEST_GRANT",
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy.strategy_version,
        "strategy_fingerprint": strategy.fingerprint(),
        "deployment_universe_version": DEPLOYMENT_UNIVERSE_VERSION,
        "deployment_universe_fingerprint": (
            strategy.universe.fingerprint() if universe is None else universe.fingerprint()
        ),
        "environment": "EXTERNAL_PAPER",
        "code_commit": "d05b3d0",
        "created_at": NOW,
    }
    settings.update(overrides)
    return StrategyDeploymentAuthorizationV2(**settings)


def author_policy(root: Path, **overrides: Any) -> None:
    """Author the risk policy an operator owes.

    Deliberately wide so the risk engine does not constrain what these tests are
    about, and `production_authorized` stays False. `max_positions: None` is the
    V1.3 no-cap the owner approved.
    """
    policy: dict[str, Any] = {
        "policy_id": "ALPHATRADER_PAPER",
        "policy_version": "V2",
        "max_gross_exposure": 1.0,
        "max_net_exposure": 1.0,
        "max_position_weight": 1.0,
        "max_positions": None,
        "min_cash_weight": 0.0,
        "production_authorized": False,
    }
    policy.update(overrides)
    write_risk_policy(root, policy)


def author_mapping(root: Path, symbols: dict[int, str] | None = None) -> None:
    """Author the venue symbol mapping, declaring fractionability per symbol."""
    rows = symbols if symbols is not None else ALL_SYMBOLS
    write_symbol_mapping(
        root,
        [
            {
                "kind": "COMPANY",
                "identity_id": key,
                "symbol": value,
                "fractionable": value not in set(WHOLE_SHARE.values()),
            }
            for key, value in rows.items()
        ],
    )


@dataclass
class V3Runtime:
    """A temporary runtime revised to the V3 deployment authority."""

    root: Path
    binding: Any
    universe: Any
    capability: Any
    mapping: Any
    strategy: Any
    predecessor: Any


def author_v3(
    paper_runtime: Any,
    *,
    universe: Any = None,
    mapping: Any = None,
    write_universe: bool = True,
    write_capability: bool = True,
    policy: bool = True,
    mapping_file: bool = True,
    write_authorization: bool = True,
) -> V3Runtime:
    """Revise a temporary V1 runtime to a V3 deployment, writing the artefacts.

    This is the operator sequence P2.3.1H could not perform, run against a
    throwaway root: author the four artefacts, then take the reviewed
    lineage-preserving transition from the active V1 binding straight to V3.
    """
    root = paper_runtime.root
    built_mapping = mapping if mapping is not None else full_mapping()
    built_universe = universe if universe is not None else fractionable_universe(built_mapping)
    capability = deployment_capability(built_mapping, built_universe)
    strategy = v2_strategy(built_universe)
    predecessor = active_binding(root)

    if policy:
        author_policy(root)
    if mapping_file:
        author_mapping(root)
    if write_universe:
        write_deployment_universe(root, built_universe)
    if write_capability:
        write_deployment_capability(root, capability)
    if write_authorization:
        write_strategy_authorization(root, grant_for(strategy))

    binding = revise_deployment(
        root=root,
        strategy=strategy,
        authorization=grant_for(strategy),
        risk_policy=paper_risk_policy_v2(),
        mapping=built_mapping,
        universe=built_universe,
        capability=capability,
        account_binding_fingerprint=paper_runtime.account,
        reason="deploy the reviewed fractionable V2 deployment",
        now=NOW,
        code_commit="d05b3d0",
        confirmed=True,
    )
    return V3Runtime(
        root=root,
        binding=binding,
        universe=built_universe,
        capability=capability,
        mapping=built_mapping,
        strategy=strategy,
        predecessor=predecessor,
    )


def marks() -> dict[str, Decimal]:
    """A sizing mark for each routable identity.

    Not a round number: at 100.00 a 100,000 book buys exactly 1,000 shares and
    the test could not tell "fractional quantity preserved" from "integer
    quantity that happened to be right".
    """
    return {f"COMPANY:{key}": Decimal("123.45") for key in ALL_SYMBOLS}


def bundle_for(session: str, count: int = 21) -> Any:
    """The research cross-section for a decision session.

    Twenty-one records so the decile rule asks for three, and the first three
    identities are the ones the mapping can route — including the whole-share
    name, so the deployment intersection has something to exclude.
    """
    from stock_screener.portfolio_risk import SecurityIdentity
    from stock_screener.strategies import StrategyInputBundle, StrategyInputRecord

    return StrategyInputBundle(
        as_of=session,
        score_version="COMPOUNDER_V1_2",
        universe_fingerprint="u" * 64,
        records=tuple(
            StrategyInputRecord(
                identity=SecurityIdentity(kind="COMPANY", identity_id=index),
                final_score=90.0 - index,
                scoring_status="SCORED",
                freshness="CURRENT",
                score_date=session,
                score_version="COMPOUNDER_V1_2",
                calculated_at="2026-10-30T07:40:00+00:00",
                source_fingerprint=f"fp-{index}",
            )
            for index in range(1, count + 1)
        ),
    )


def run_through(composition: Any, *, mode: Any = None) -> Any:
    """Drive one slot through the real coordinator, which supplies the gate.

    Called rather than invoking the planner directly: the coordinator is what
    arms, gates and journals a run, and a test that bypassed it would be
    exercising a path `run-due` does not take.
    """
    from stock_screener.paper_operations import OperationsMode

    return composition.coordinator.run_due(
        mode=mode or OperationsMode.EXTERNAL_PAPER,
        plan_and_submit=composition.planner,
    )


def composed(
    runtime: Any,
    *,
    now: str,
    venue: ScriptedVenue,
    binding: Any = None,
    mode: Any = None,
) -> Any:
    """Build a run through the real production composition root."""
    from stock_screener.paper_operations import OperationsMode

    runtime.clock.set(now)
    slot = runtime.schedule.slot_for(now)
    assert slot is not None, f"no slot is open at {now}"
    return compose_operations(
        settings=FakeSettings(),
        root=runtime.root,
        binding=binding if binding is not None else active_binding(runtime.root),
        slot=slot,
        schedule=runtime.schedule,
        calendar=runtime.calendar,
        mode=mode or OperationsMode.EXTERNAL_PAPER,
        clock=runtime.clock,
        code_commit="d05b3d0",
        bundle_for=bundle_for,
        client=venue.client(),
        marks=marks(),
    )


def readiness(runtime: Any, *, binding: Any = None, now: str = DECISION_NOW) -> dict[str, Any]:
    """The deterministic readiness report, with no network."""
    from stock_screener.paper_operations import OperationsMode

    runtime.clock.set(now)
    slot = runtime.schedule.slot_for(now)
    assert slot is not None
    return composition_readiness(
        settings=FakeSettings(),
        root=runtime.root,
        binding=binding if binding is not None else active_binding(runtime.root),
        slot=slot,
        schedule=runtime.schedule,
        calendar=runtime.calendar,
        mode=OperationsMode.EXTERNAL_PAPER,
        clock=runtime.clock,
        code_commit="d05b3d0",
    )


# --------------------------------------------------------------------------- #
# §3-§6 the resolver
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_v1_resolves_to_the_exact_historical_strategy() -> None:
    installed = CompounderMonthlyStrategy()

    resolved = resolve_strategy(
        strategy_id=installed.strategy_id,
        strategy_version=STRATEGY_VERSION,
        strategy_fingerprint=installed.fingerprint(),
    )

    assert type(resolved) is CompounderMonthlyStrategy
    assert resolved.strategy_version == STRATEGY_VERSION
    assert resolved.fingerprint() == installed.fingerprint()


@pytest.mark.integration
def test_v1_needs_no_deployment_universe() -> None:
    installed = CompounderMonthlyStrategy()

    resolved = resolve_strategy(
        strategy_id=installed.strategy_id,
        strategy_version=STRATEGY_VERSION,
        strategy_fingerprint=installed.fingerprint(),
        universe=None,
    )

    assert resolved.strategy_version == STRATEGY_VERSION


@pytest.mark.integration
def test_v2_resolves_with_the_exact_bound_universe() -> None:
    universe = fractionable_universe(full_mapping())
    installed = v2_strategy(universe)

    resolved = resolve_strategy(
        strategy_id=installed.strategy_id,
        strategy_version=STRATEGY_VERSION_V2,
        strategy_fingerprint=installed.fingerprint(),
        universe=universe,
    )

    assert type(resolved) is CompounderMonthlyFractionableStrategy
    assert resolved.universe is universe


@pytest.mark.integration
def test_v2_is_refused_without_a_deployment_universe() -> None:
    installed = v2_strategy(fractionable_universe(full_mapping()))

    with pytest.raises(RuntimeBindingError, match="deploys only across a frozen") as caught:
        resolve_strategy(
            strategy_id=installed.strategy_id,
            strategy_version=STRATEGY_VERSION_V2,
            strategy_fingerprint=installed.fingerprint(),
            universe=None,
        )

    assert caught.value.code is Code.RUNTIME_DEPLOYMENT_UNIVERSE_NOT_AUTHORED


@pytest.mark.integration
def test_an_unknown_strategy_version_is_refused() -> None:
    with pytest.raises(RuntimeBindingError, match="no production strategy is registered") as got:
        resolve_strategy(
            strategy_id="ALPHATRADER_COMPOUNDER_MONTHLY",
            strategy_version="ALPHATRADER_COMPOUNDER_MONTHLY_V999",
            strategy_fingerprint="f" * 64,
        )

    assert got.value.code is Code.RUNTIME_STRATEGY_NOT_RESOLVABLE


@pytest.mark.integration
def test_an_unknown_strategy_id_is_refused() -> None:
    with pytest.raises(RuntimeBindingError, match="no production strategy is registered"):
        resolve_strategy(
            strategy_id="SOMETHING_ELSE",
            strategy_version=STRATEGY_VERSION,
            strategy_fingerprint="f" * 64,
        )


@pytest.mark.integration
def test_an_unknown_version_does_not_fall_back_to_the_latest_or_to_v1() -> None:
    """Refusal rather than substitution, asserted on the message and the code.

    A resolver that answered "the newest I have" would make every unrecognised
    binding silently run whatever shipped last.
    """
    with pytest.raises(RuntimeBindingError) as caught:
        resolve_strategy(
            strategy_id="ALPHATRADER_COMPOUNDER_MONTHLY",
            strategy_version="ALPHATRADER_COMPOUNDER_MONTHLY_V999",
            strategy_fingerprint="f" * 64,
        )
    message = str(caught.value)

    assert "no latest strategy" in message
    assert "no fallback" in message


@pytest.mark.integration
def test_a_fingerprint_mismatch_is_refused() -> None:
    installed = CompounderMonthlyStrategy()

    with pytest.raises(RuntimeBindingError, match="code has changed since") as caught:
        resolve_strategy(
            strategy_id=installed.strategy_id,
            strategy_version=STRATEGY_VERSION,
            strategy_fingerprint="c" * 64,
        )

    assert caught.value.code is Code.RUNTIME_STRATEGY_NOT_RESOLVABLE


@pytest.mark.integration
def test_a_v2_fingerprint_mismatch_is_refused_even_with_a_valid_universe() -> None:
    universe = fractionable_universe(full_mapping())

    with pytest.raises(RuntimeBindingError, match="code has changed since"):
        resolve_strategy(
            strategy_id="ALPHATRADER_COMPOUNDER_MONTHLY",
            strategy_version=STRATEGY_VERSION_V2,
            strategy_fingerprint="c" * 64,
            universe=universe,
        )


# --------------------------------------------------------------------------- #
# §7-§8 no hardcoding, and every declared version has a resolver
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_no_binding_driven_production_path_constructs_a_strategy_directly() -> None:
    """The runtime modules name no strategy class at all.

    Parsed rather than grepped: these modules discuss the defect in prose, so a
    text scan would match their own docstrings. Only an actual call to a
    strategy constructor counts.
    """
    offenders: list[str] = []
    runtime = Path("src/stock_screener/paper_runtime")
    for path in sorted(runtime.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name in {"CompounderMonthlyStrategy", "CompounderMonthlyFractionableStrategy"}:
                # `resolver.py` is the one place allowed to construct them: it
                # is the registry the binding resolves through.
                if path.name == "resolver.py":
                    continue
                offenders.append(f"{path.name}:{node.lineno} {name}()")

    assert offenders == []


@pytest.mark.integration
def test_every_deployable_strategy_version_has_a_resolver() -> None:
    """No declared runtime-deployable version may have nothing behind it."""
    for (strategy_id, version), spec in DEPLOYABLE_STRATEGIES.items():
        resolved = spec.build(
            universe=fractionable_universe(full_mapping())
            if spec.requires_deployment_universe
            else None
        )

        assert resolved.strategy_id == strategy_id
        assert resolved.strategy_version == version


@pytest.mark.integration
def test_the_registry_covers_both_reviewed_versions() -> None:
    from stock_screener.paper_runtime import deployable_strategy_versions

    assert deployable_strategy_versions() == (STRATEGY_VERSION, STRATEGY_VERSION_V2)


@pytest.mark.integration
def test_the_planner_refuses_to_run_without_a_resolved_strategy(paper_runtime: Any) -> None:
    """A planner never picks an implementation of its own.

    Defaulting here is precisely how a runtime comes to run a policy its
    binding does not name, so the absence is a refusal rather than a fallback.
    """
    from stock_screener.paper_runtime.orchestration import RuntimePlanner

    planner = RuntimePlanner(
        root=paper_runtime.root,
        binding=paper_runtime.binding,
        slot=paper_runtime.schedule.slots_for_session("2026-10-30")[0],
        calendar=paper_runtime.calendar,
    )

    with pytest.raises(RuntimeBindingError, match="no strategy was resolved") as caught:
        planner._bound_strategy()

    assert caught.value.code is Code.RUNTIME_STRATEGY_NOT_RESOLVABLE


# --------------------------------------------------------------------------- #
# §14-§20 the deployment-scoped capability
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_fractionable_only_universe_proves_the_deployment_capability() -> None:
    mapping = full_mapping()
    universe = fractionable_universe(mapping)

    proof = deployment_capability(mapping, universe)

    assert proof.supported is True
    assert proof.eligible_identities == len(FRACTIONABLE)
    assert proof.verified_identities == len(FRACTIONABLE)


@pytest.mark.integration
def test_the_full_mapping_keeps_the_whole_share_names_the_deployment_excludes() -> None:
    """§13/§20. The provider fact survives; it is simply not deployable."""
    mapping = full_mapping()
    universe = fractionable_universe(mapping)
    mapped = {row.identity_id for row in mapping.records}

    assert mapped == set(ALL_SYMBOLS)
    assert set(WHOLE_SHARE) <= mapped
    assert all(identity_id not in dict(universe.members).values() for identity_id in WHOLE_SHARE)
    assert universe.by_reason() == {"WHOLE_SHARE_ONLY": len(WHOLE_SHARE)}


@pytest.mark.integration
def test_the_capability_never_claims_the_account_is_fractionable() -> None:
    """§16. The scope is in the record, not only in this module's docstring."""
    mapping = full_mapping()
    proof = deployment_capability(mapping, fractionable_universe(mapping))
    body = proof.body()

    assert body["account_wide_claim"] is False
    assert body["scope"].startswith("DEPLOYMENT_SCOPED")
    assert "NOT a claim that the provider" in body["scope"]


@pytest.mark.integration
def test_the_account_capability_still_reports_no_account_wide_fractional_support() -> None:
    """The two claims coexist and disagree, which is the correct answer.

    The deployment supports fractional quantities; the account does not. Making
    the second true to express the first is the false widening §16 forbids.
    """
    from stock_screener.paper_runtime import account_capability_from_mapping

    mapping = full_mapping()
    account = account_capability_from_mapping(mapping)
    deployment = deployment_capability(mapping, fractionable_universe(mapping))

    assert account["fractional_quantities"] is False
    assert account["whole_share_only_instruments"] == len(WHOLE_SHARE)
    assert deployment.supported is True


@pytest.mark.integration
def test_a_whole_share_member_makes_the_deployment_capability_unproven() -> None:
    """§45. A universe that admits a whole-share name proves nothing."""
    from stock_screener.paper_runtime import DeploymentUniverse

    mapping = full_mapping()
    good = fractionable_universe(mapping)
    bad = replace(good, members=(("COMPANY", 1), ("COMPANY", 3)))

    proof = deployment_capability(mapping, bad)

    assert isinstance(bad, DeploymentUniverse)
    assert proof.supported is False
    assert proof.non_fractionable_identities == ("COMPANY:3",)


@pytest.mark.integration
def test_unknown_fractionability_makes_the_capability_unproven() -> None:
    """§46. Unstated is not permitted, and is counted apart from refused."""
    mapping = full_mapping(AAA={"fractionable": None})
    universe = replace(fractionable_universe(mapping), members=(("COMPANY", 1), ("COMPANY", 2)))

    proof = deployment_capability(mapping, universe)

    assert proof.supported is False
    assert proof.unknown_fractionability_identities == ("COMPANY:1",)
    assert proof.non_fractionable_identities == ()


@pytest.mark.integration
def test_a_member_missing_from_the_mapping_makes_the_capability_unproven() -> None:
    mapping = full_mapping()
    universe = replace(fractionable_universe(mapping), members=(("COMPANY", 1), ("COMPANY", 99)))

    proof = deployment_capability(mapping, universe)

    assert proof.supported is False
    assert proof.missing_identities == ("COMPANY:99",)


@pytest.mark.integration
def test_a_non_tradable_member_makes_the_capability_unproven() -> None:
    mapping = full_mapping(AAA={"tradable": False})
    universe = replace(fractionable_universe(mapping), members=(("COMPANY", 1), ("COMPANY", 2)))

    proof = deployment_capability(mapping, universe)

    assert proof.supported is False
    assert "COMPANY:1" in proof.unverified


@pytest.mark.integration
def test_an_empty_universe_proves_nothing() -> None:
    """Vacuous truth is refused: nothing authorised is not everything verified."""
    mapping = full_mapping()
    universe = replace(fractionable_universe(mapping), members=())

    assert deployment_capability(mapping, universe).supported is False


@pytest.mark.integration
def test_an_unmodelled_required_capability_is_not_satisfied_by_silence() -> None:
    mapping = full_mapping()
    universe = fractionable_universe(mapping)

    proof = deployment_capability(mapping, universe, required_capability="TELEPATHY")

    assert proof.supported is False


@pytest.mark.integration
def test_the_capability_cannot_be_asserted_by_a_caller() -> None:
    """§18. There is no argument that sets the answer.

    Read off the signature rather than argued: the derivation takes the two
    frozen artefacts and the name of the requirement, and nothing else.
    """
    signature = inspect.signature(deployment_capability)

    assert list(signature.parameters) == ["mapping", "universe", "required_capability"]
    assert not {name for name in signature.parameters if "fraction" in name.lower()}


@pytest.mark.integration
def test_the_capability_derivation_reaches_no_network() -> None:
    import importlib

    capability_module = importlib.import_module(
        "stock_screener.paper_runtime.deployment_capability"
    )
    source = Path(str(capability_module.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    called = {
        getattr(node.func, "attr", "") or getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }

    assert not imported & {"httpx", "requests", "urllib", "socket", "http"}
    assert not called & {"request", "from_settings", "AlpacaPaperHttpTransport"}


@pytest.mark.integration
def test_an_unproven_capability_refuses_rather_than_warns() -> None:
    mapping = full_mapping()
    bad = replace(fractionable_universe(mapping), members=(("COMPANY", 3),))

    with pytest.raises(RuntimeBindingError, match="is not proven for this deployment") as got:
        deployment_capability(mapping, bad).require_supported()

    assert got.value.code is Code.RUNTIME_DEPLOYMENT_CAPABILITY_UNPROVEN


# --------------------------------------------------------------------------- #
# §10, §11, §54-§56 the V3 binding
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_v3_binding_binds_the_deployment_universe_and_capability(
    paper_runtime: Any,
) -> None:
    runtime = author_v3(paper_runtime)

    assert runtime.binding.version == RUNTIME_BINDING_V3_VERSION
    assert runtime.binding.deployment_universe_fingerprint == runtime.universe.fingerprint()
    assert runtime.binding.deployment_capability_fingerprint == runtime.capability.fingerprint()
    assert runtime.binding.deployment_universe_version == DEPLOYMENT_UNIVERSE_VERSION


@pytest.mark.integration
def test_a_v3_binding_records_the_risk_engine_it_is_authorised_against(
    paper_runtime: Any,
) -> None:
    runtime = author_v3(paper_runtime)

    assert runtime.binding.risk_engine_version == "PORTFOLIO_RISK_ENGINE_V1_3"


@pytest.mark.integration
def test_a_v3_binding_refuses_a_blank_deployment_field() -> None:
    """The V2 gap cannot reappear as a V3 binding that names no universe."""
    with pytest.raises(RuntimeBindingError, match="must state") as caught:
        PaperOperationsRuntimeBindingV3(
            version=RUNTIME_BINDING_V3_VERSION,
            predecessor_fingerprint="a" * 64,
            inherited={"runtime_id": "x"},
            account_binding_fingerprint="b" * 64,
            risk_policy_fingerprint="c" * 64,
            instrument_mapping_fingerprint="d" * 64,
            account_capability={},
            reason="r",
            code_commit="c",
            created_at=NOW,
            risk_engine_version="PORTFOLIO_RISK_ENGINE_V1_3",
            deployment_universe_version=DEPLOYMENT_UNIVERSE_VERSION,
            deployment_universe_fingerprint="",
            deployment_capability_fingerprint="f" * 64,
        )

    assert caught.value.code is Code.RUNTIME_REBIND_REFUSED
    assert "deployment_universe_fingerprint" in str(caught.value)


@pytest.mark.integration
def test_a_v3_binding_round_trips_its_deployment_universe_fingerprint(
    paper_runtime: Any,
) -> None:
    """§54. The specific write-path / read-path defect, guarded by a reload."""
    runtime = author_v3(paper_runtime)

    reloaded = active_binding(runtime.root)

    assert reloaded.version == RUNTIME_BINDING_V3_VERSION
    assert reloaded.deployment_universe_fingerprint == runtime.universe.fingerprint()


@pytest.mark.integration
def test_every_v3_field_round_trips_to_an_identical_body(paper_runtime: Any) -> None:
    """§55. No field may exist only on write or only on read.

    Compared as whole canonical bodies and then as fingerprints, so a field
    dropped by the reader fails here rather than when a chain stops verifying
    weeks later.
    """
    runtime = author_v3(paper_runtime)

    reloaded = active_binding(runtime.root)

    assert reloaded.body() == runtime.binding.body()
    assert reloaded.fingerprint() == runtime.binding.fingerprint()
    for name in V3_DEPLOYMENT_FIELDS:
        assert getattr(reloaded, name) == getattr(runtime.binding, name), name


@pytest.mark.integration
def test_every_v3_deployment_field_is_serialised_read_and_consumed(
    paper_runtime: Any,
) -> None:
    """§56. Each declared field has a serializer, a reader and a consumer.

    The consumer check is the one that matters: a field nothing reads is a
    declaration with nothing behind it, which is the family of defect this
    project has now found nine times.
    """
    runtime = author_v3(paper_runtime)
    body = runtime.binding.body()
    reloaded = active_binding(runtime.root)
    report = readiness(paper_runtime, binding=reloaded)
    consumers = {
        "risk_engine_version": "risk_engine_version",
        "deployment_universe_version": "deployment_universe_version",
        "deployment_universe_fingerprint": "deployment_universe_fingerprint",
        "deployment_capability_fingerprint": "deployment_capability_fingerprint",
    }

    for name in V3_DEPLOYMENT_FIELDS:
        assert name in body, f"{name} is not serialised"
        assert getattr(reloaded, name), f"{name} is not read back"
        assert report[consumers[name]], f"{name} has no production consumer"


@pytest.mark.integration
def test_the_v3_chain_verifies_after_a_revision(paper_runtime: Any) -> None:
    runtime = author_v3(paper_runtime)
    history = RuntimeBindingHistory(runtime.root)

    assert [row.sequence for row in history.records()] == [1]
    assert history.verify()["chain_verified"] is True


@pytest.mark.integration
def test_the_v1_and_v2_binding_contracts_are_unchanged() -> None:
    """§1. V3 is additive; the two older contracts keep their exact versions."""
    from stock_screener.paper_runtime import RUNTIME_BINDING_V2_VERSION
    from stock_screener.paper_runtime.binding import (
        PAPER_OPERATIONS_RUNTIME_BINDING_VERSION,
    )

    assert PAPER_OPERATIONS_RUNTIME_BINDING_VERSION == "PAPER_OPERATIONS_RUNTIME_BINDING_V1"
    assert RUNTIME_BINDING_V2_VERSION == "PAPER_OPERATIONS_RUNTIME_BINDING_V2"
    assert RUNTIME_BINDING_V3_VERSION == "PAPER_OPERATIONS_RUNTIME_BINDING_V3"


# --------------------------------------------------------------------------- #
# §26-§29, §59-§60 the lineage-preserving transition
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_v1_to_v3_transition_preserves_every_lineage(paper_runtime: Any) -> None:
    """§59. The books survive; only the deployment authority moves."""
    before = paper_runtime.binding
    runtime = author_v3(paper_runtime)

    for name in IMMUTABLE_ACROSS_REBIND:
        if name in STRATEGY_REVISION_FIELDS:
            continue
        assert getattr(runtime.binding, name) == getattr(before, name), name


@pytest.mark.integration
def test_a_v1_to_v3_transition_points_at_the_v1_binding_as_predecessor(
    paper_runtime: Any,
) -> None:
    """§28. No fake intermediate V2 binding is required to reach V3."""
    runtime = author_v3(paper_runtime)

    assert runtime.predecessor.version == "PAPER_OPERATIONS_RUNTIME_BINDING_V1"
    assert runtime.binding.predecessor_fingerprint == runtime.predecessor.fingerprint()
    assert RuntimeBindingHistory(runtime.root).records()[0].sequence == 1


@pytest.mark.integration
def test_a_v1_to_v3_transition_writes_no_opening_balance(paper_runtime: Any) -> None:
    """§23/§27. Not a bootstrap: the ledger is untouched."""
    ledger = paper_runtime.root / "runtime" / "portfolio" / "ledger"
    before = sorted(path.name for path in ledger.glob("*.json"))
    opening_before = _opening_balance_events(ledger)

    author_v3(paper_runtime)

    assert sorted(path.name for path in ledger.glob("*.json")) == before
    assert _opening_balance_events(ledger) == opening_before


def _opening_balance_events(ledger: Path) -> int:
    """How many opening-balance events the ledger holds."""
    return sum(
        1
        for path in ledger.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["event"]["event_type"] == "OPENING_BALANCE"
    )


@pytest.mark.integration
def test_a_v1_to_v3_transition_leaves_the_execution_journal_byte_identical(
    paper_runtime: Any,
) -> None:
    """§25. No synthetic entry merely because deployment authority changed."""
    execution = paper_runtime.root / "runtime" / "execution"
    before = {path.name: path.read_bytes() for path in sorted(execution.glob("**/*.json"))}

    author_v3(paper_runtime)

    after = {path.name: path.read_bytes() for path in sorted(execution.glob("**/*.json"))}

    assert after == before


@pytest.mark.integration
def test_a_v1_to_v3_transition_is_refused_without_confirmation(paper_runtime: Any) -> None:
    mapping = full_mapping()
    universe = fractionable_universe(mapping)
    strategy = v2_strategy(universe)

    with pytest.raises(RuntimeBindingError, match="requires explicit confirmation"):
        revise_deployment(
            root=paper_runtime.root,
            strategy=strategy,
            authorization=grant_for(strategy),
            risk_policy=paper_risk_policy_v2(),
            mapping=mapping,
            universe=universe,
            capability=deployment_capability(mapping, universe),
            account_binding_fingerprint=paper_runtime.account,
            reason="r",
            now=NOW,
            confirmed=False,
        )


@pytest.mark.integration
def test_a_v1_to_v3_transition_is_refused_with_an_unproven_capability(
    paper_runtime: Any,
) -> None:
    mapping = full_mapping()
    bad = replace(fractionable_universe(mapping), members=(("COMPANY", 1), ("COMPANY", 3)))
    strategy = v2_strategy(bad)

    with pytest.raises(RuntimeBindingError, match="is not proven for this deployment"):
        revise_deployment(
            root=paper_runtime.root,
            strategy=strategy,
            authorization=grant_for(strategy),
            risk_policy=paper_risk_policy_v2(),
            mapping=mapping,
            universe=bad,
            capability=deployment_capability(mapping, bad),
            account_binding_fingerprint=paper_runtime.account,
            reason="r",
            now=NOW,
            confirmed=True,
        )


@pytest.mark.integration
def test_a_v1_to_v3_transition_refuses_a_crossed_universe_and_mapping(
    paper_runtime: Any,
) -> None:
    """A universe derived from other evidence is not this deployment's universe."""
    mapping = full_mapping()
    other = full_mapping(identities={1: "AAA"})
    universe = fractionable_universe(other)
    strategy = v2_strategy(universe)

    with pytest.raises(RuntimeBindingError, match="one artefact pair") as caught:
        revise_deployment(
            root=paper_runtime.root,
            strategy=strategy,
            authorization=grant_for(strategy),
            risk_policy=paper_risk_policy_v2(),
            mapping=mapping,
            universe=universe,
            capability=deployment_capability(other, universe),
            account_binding_fingerprint=paper_runtime.account,
            reason="r",
            now=NOW,
            confirmed=True,
        )

    assert caught.value.code is Code.RUNTIME_OPERATOR_INPUT_FINGERPRINT_MISMATCH


@pytest.mark.integration
def test_a_v1_to_v3_transition_is_refused_while_a_decision_awaits_execution(
    paper_runtime: Any,
) -> None:
    """§60. A pending decision is never migrated to a new deployment."""

    class PendingStore:
        """A decision store holding one non-terminal decision."""

        def pending(self) -> Any:
            decision = type("D", (), {"decision_session": "2026-10-30"})()
            return type("Row", (), {"decision": decision, "status": "PENDING_EXECUTION"})()

    mapping = full_mapping()
    universe = fractionable_universe(mapping)
    strategy = v2_strategy(universe)

    with pytest.raises(RuntimeBindingError, match="has not reached a terminal state") as got:
        revise_deployment(
            root=paper_runtime.root,
            strategy=strategy,
            authorization=grant_for(strategy),
            risk_policy=paper_risk_policy_v2(),
            mapping=mapping,
            universe=universe,
            capability=deployment_capability(mapping, universe),
            account_binding_fingerprint=paper_runtime.account,
            reason="r",
            now=NOW,
            decisions=PendingStore(),
            confirmed=True,
        )

    assert got.value.code is Code.RUNTIME_REBIND_PRECONDITION_FAILED


@pytest.mark.integration
@pytest.mark.parametrize("probe", ["remote_state_unknown", "partially_executed"])
def test_a_v1_to_v3_transition_is_refused_with_unresolved_execution_state(
    paper_runtime: Any, probe: str
) -> None:
    """§60. Unknown remote state and a half-executed plan both refuse."""
    executions = type("E", (), {probe: lambda self: True})()
    mapping = full_mapping()
    universe = fractionable_universe(mapping)
    strategy = v2_strategy(universe)

    with pytest.raises(RuntimeBindingError, match="execution authority is unresolved") as got:
        revise_deployment(
            root=paper_runtime.root,
            strategy=strategy,
            authorization=grant_for(strategy),
            risk_policy=paper_risk_policy_v2(),
            mapping=mapping,
            universe=universe,
            capability=deployment_capability(mapping, universe),
            account_binding_fingerprint=paper_runtime.account,
            reason="r",
            now=NOW,
            executions=executions,
            confirmed=True,
        )

    assert got.value.code is Code.RUNTIME_REBIND_PRECONDITION_FAILED


# --------------------------------------------------------------------------- #
# §30-§33, §42-§43, §51-§53, §57 operator artefacts
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_v3_contract_requires_five_operator_artefacts() -> None:
    """P2.3.1I2 added the grant: production checks it, so it must be present."""
    assert REQUIRED_OPERATOR_INPUTS[RUNTIME_BINDING_V3_VERSION] == (
        "RISK_POLICY",
        "SYMBOL_MAPPING",
        "DEPLOYMENT_UNIVERSE",
        "DEPLOYMENT_CAPABILITY",
        "STRATEGY_AUTHORIZATION",
    )


@pytest.mark.integration
def test_a_v1_contract_still_requires_only_the_two_it_always_did() -> None:
    """A historical root is never told it is missing a V3 artefact."""
    assert REQUIRED_OPERATOR_INPUTS["PAPER_OPERATIONS_RUNTIME_BINDING_V1"] == (
        "RISK_POLICY",
        "SYMBOL_MAPPING",
    )


@pytest.mark.integration
def test_every_required_operator_artefact_has_a_writer_reader_and_validator() -> None:
    """§57. No dead declaration.

    The fingerprint-validator check is read off the reader's signature: an
    artefact the binding pins must accept `expected_fingerprint`, or nothing
    would stop a tampered file being loaded.
    """
    from stock_screener.paper_runtime import operator_inputs as module

    writers = {
        "RISK_POLICY": "write_risk_policy",
        "SYMBOL_MAPPING": "write_symbol_mapping",
        "DEPLOYMENT_UNIVERSE": "write_deployment_universe",
        "DEPLOYMENT_CAPABILITY": "write_deployment_capability",
        "STRATEGY_AUTHORIZATION": "write_strategy_authorization",
    }
    readers = {
        "RISK_POLICY": "read_risk_policy",
        "SYMBOL_MAPPING": "read_symbol_mapping",
        "DEPLOYMENT_UNIVERSE": "read_deployment_universe",
        "DEPLOYMENT_CAPABILITY": "read_deployment_capability",
        "STRATEGY_AUTHORIZATION": "read_strategy_authorization",
    }
    for name in REQUIRED_OPERATOR_INPUTS[RUNTIME_BINDING_V3_VERSION]:
        assert callable(getattr(module, writers[name])), name
        reader = getattr(module, readers[name])
        assert callable(reader), name
        _, pinned_field = OPERATOR_INPUT_FILES[name]
        if pinned_field:
            assert "expected_fingerprint" in inspect.signature(reader).parameters, name


@pytest.mark.integration
def test_a_missing_deployment_universe_fails_closed(paper_runtime: Any) -> None:
    """§42. Refused before the strategy is constructed."""
    runtime = author_v3(paper_runtime)
    (runtime.root / "runtime" / DEPLOYMENT_UNIVERSE_FILENAME).unlink()

    report = readiness(paper_runtime, binding=active_binding(runtime.root))

    assert report["ready"] is False
    assert "RUNTIME_DEPLOYMENT_UNIVERSE_NOT_AUTHORED" in report["detail"]


@pytest.mark.integration
def test_a_tampered_deployment_universe_fails_closed(paper_runtime: Any) -> None:
    """§43. One altered membership identity and the fingerprint no longer matches."""
    runtime = author_v3(paper_runtime)
    path = runtime.root / "runtime" / DEPLOYMENT_UNIVERSE_FILENAME
    body = json.loads(path.read_text(encoding="utf-8"))
    body["members"] = [["COMPANY", 1], ["COMPANY", 3]]
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(RuntimeBindingError, match="digests to") as caught:
        read_deployment_universe(runtime.root, expected_fingerprint=runtime.universe.fingerprint())

    assert caught.value.code is Code.RUNTIME_OPERATOR_INPUT_FINGERPRINT_MISMATCH


@pytest.mark.integration
def test_a_tampered_deployment_capability_fails_closed(paper_runtime: Any) -> None:
    """§48's companion: a proof edited to claim support is refused on its digest."""
    runtime = author_v3(paper_runtime)
    path = runtime.root / "runtime" / DEPLOYMENT_CAPABILITY_FILENAME
    body = json.loads(path.read_text(encoding="utf-8"))
    body["verified_identities"] = 999
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(RuntimeBindingError, match="digests to"):
        read_deployment_capability(
            runtime.root, expected_fingerprint=runtime.capability.fingerprint()
        )


@pytest.mark.integration
def test_a_deployment_universe_of_the_wrong_version_fails_closed(
    paper_runtime: Any,
) -> None:
    runtime = author_v3(paper_runtime)
    path = runtime.root / "runtime" / DEPLOYMENT_UNIVERSE_FILENAME
    body = json.loads(path.read_text(encoding="utf-8"))
    body["version"] = "ALPHATRADER_PAPER_FRACTIONABLE_UNIVERSE_V99"
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(RuntimeBindingError, match="declares version") as caught:
        read_deployment_universe(runtime.root)

    assert caught.value.code is Code.RUNTIME_OPERATOR_INPUT_UNREADABLE


@pytest.mark.integration
def test_an_empty_authored_universe_fails_closed(paper_runtime: Any) -> None:
    runtime = author_v3(paper_runtime)
    path = runtime.root / "runtime" / DEPLOYMENT_UNIVERSE_FILENAME
    body = json.loads(path.read_text(encoding="utf-8"))
    body["members"] = []
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(RuntimeBindingError, match="no members"):
        read_deployment_universe(runtime.root)


@pytest.mark.integration
def test_a_missing_risk_policy_fails_closed(paper_runtime: Any) -> None:
    """§51. No example and no default."""
    runtime = author_v3(paper_runtime)
    (runtime.root / "runtime" / "risk_policy.json").unlink()

    report = readiness(paper_runtime, binding=active_binding(runtime.root))

    assert report["ready"] is False
    assert "RUNTIME_RISK_POLICY_NOT_AUTHORED" in report["detail"]


@pytest.mark.integration
def test_a_missing_symbol_mapping_fails_closed(paper_runtime: Any) -> None:
    """§52."""
    runtime = author_v3(paper_runtime)
    (runtime.root / "runtime" / "symbol_mapping.json").unlink()

    report = readiness(paper_runtime, binding=active_binding(runtime.root))

    assert report["ready"] is False
    assert "RUNTIME_SYMBOL_MAPPING_NOT_AUTHORED" in report["detail"]


@pytest.mark.integration
def test_a_missing_deployment_capability_fails_closed(paper_runtime: Any) -> None:
    """§53."""
    runtime = author_v3(paper_runtime)
    (runtime.root / "runtime" / DEPLOYMENT_CAPABILITY_FILENAME).unlink()

    report = readiness(paper_runtime, binding=active_binding(runtime.root))

    assert report["ready"] is False
    assert "RUNTIME_DEPLOYMENT_CAPABILITY_NOT_AUTHORED" in report["detail"]


@pytest.mark.integration
def test_nothing_falls_back_to_a_repository_example_or_a_provider_lookup() -> None:
    """§32. The readers refuse; they never substitute.

    Parsed, because the modules name the examples they refuse in prose.
    """
    import stock_screener.paper_runtime.operator_inputs as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names = {
        getattr(node.func, "attr", "") or getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }

    assert not names & {
        "example_advanced_risk_policy",
        "paper_risk_policy",
        "paper_risk_policy_v2",
        "fractionable_universe",
        "mapping_from_evidence",
        "deployment_capability",
        "from_settings",
    }


# --------------------------------------------------------------------------- #
# §34-§41, §49 the production composition and decision path
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_production_composition_resolves_v2_not_v1(paper_runtime: Any) -> None:
    """§38. The same `compose_operations` that `run-due` calls."""
    runtime = author_v3(paper_runtime)
    venue = ScriptedVenue()
    composition = composed(paper_runtime, now=DECISION_NOW, venue=venue)
    try:
        resolved = composition.planner.strategy
    finally:
        composition.close()

    assert resolved.strategy_version == STRATEGY_VERSION_V2
    assert resolved.strategy_version != STRATEGY_VERSION
    assert resolved.fingerprint() == runtime.strategy.fingerprint()


@pytest.mark.integration
def test_the_production_composition_hands_v2_its_bound_universe(
    paper_runtime: Any,
) -> None:
    runtime = author_v3(paper_runtime)
    venue = ScriptedVenue()
    composition = composed(paper_runtime, now=DECISION_NOW, venue=venue)
    try:
        resolved = composition.planner.strategy
    finally:
        composition.close()

    assert resolved.universe.fingerprint() == runtime.universe.fingerprint()
    assert resolved.universe.size == len(FRACTIONABLE)


@pytest.mark.integration
def test_a_v1_runtime_still_composes_v1(paper_runtime: Any) -> None:
    """§39. Backward compatibility, driven rather than asserted."""
    author_policy(paper_runtime.root, max_positions=50)
    author_mapping(paper_runtime.root)
    venue = ScriptedVenue()
    composition = composed(
        paper_runtime, now=DECISION_NOW, venue=venue, binding=paper_runtime.binding
    )
    try:
        resolved = composition.planner.strategy
    finally:
        composition.close()

    assert resolved.strategy_version == STRATEGY_VERSION
    assert type(resolved) is CompounderMonthlyStrategy


@pytest.mark.integration
def test_the_readiness_report_names_the_resolved_strategy(paper_runtime: Any) -> None:
    """§36/§37, with no network."""
    runtime = author_v3(paper_runtime)

    report = readiness(paper_runtime, binding=active_binding(runtime.root))

    assert report["ready"] is True
    assert report["resolved_strategy_version"] == STRATEGY_VERSION_V2
    assert report["resolved_strategy_fingerprint"] == runtime.strategy.fingerprint()
    assert report["deployment_universe_fingerprint"] == runtime.universe.fingerprint()
    assert report["deployment_capability_fingerprint"] == runtime.capability.fingerprint()
    assert report["deployment_capability_supported"] is True
    assert report["deployment_universe_members"] == len(FRACTIONABLE)
    assert report["deployment_universe_source_mapping"] == runtime.mapping.fingerprint()
    assert report["deployment_capability_version"] == DEPLOYMENT_CAPABILITY_VERSION
    assert report["live_capability"] is False


@pytest.mark.integration
def test_the_readiness_report_carries_no_credential(paper_runtime: Any) -> None:
    from stock_screener.execution.external import check_no_credentials

    runtime = author_v3(paper_runtime)

    check_no_credentials(readiness(paper_runtime, binding=active_binding(runtime.root)))


@pytest.mark.integration
def test_the_decision_path_intersects_eligibility_with_the_deployment_universe(
    paper_runtime: Any,
) -> None:
    """§49. The decile is taken after the intersection, through production.

    Twenty-one eligible names would give V1 a target of three. Only two are
    deployable here, so V2 must ask for one — and the whole-share name must be
    recorded as a venue exclusion rather than a research failure.
    """
    author_v3(paper_runtime)
    venue = ScriptedVenue()
    composition = composed(paper_runtime, now=DECISION_NOW, venue=venue)
    try:
        outcome = run_through(composition)
    finally:
        composition.close()
    assert outcome.report.succeeded, outcome.report.blockers
    record = _pending(paper_runtime.root)
    assert record is not None
    decision = record.decision

    assert composition.planner.outcome is not None
    assert composition.planner.outcome.purpose.value == "STRATEGY_DECISION"
    assert decision.strategy_version == STRATEGY_VERSION_V2
    assert decision.eligible_count == len(FRACTIONABLE)
    assert decision.target_count == 1
    assert decision.excluded_counts[DEPLOYMENT_EXCLUSION_REASON] == 19
    assert [row.identity.identity_id for row in decision.selections] == [1]


@pytest.mark.integration
def test_the_decision_binds_the_deployment_universe_fingerprint(
    paper_runtime: Any,
) -> None:
    """§11/§22 of P2.3.1G, now through the production path."""
    runtime = author_v3(paper_runtime)
    venue = ScriptedVenue()
    composition = composed(paper_runtime, now=DECISION_NOW, venue=venue)
    try:
        run_through(composition)
    finally:
        composition.close()
    record = _pending(runtime.root)

    assert record is not None
    assert record.decision.deployment_universe_fingerprint == runtime.universe.fingerprint()


@pytest.mark.integration
def test_the_cli_composes_through_one_route(paper_runtime: Any) -> None:
    """§35. The CLI instantiates no authority of its own for a run."""
    import stock_screener.cli as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    start = source.index('@paper_ops_app.command("run-due")')
    nxt = source.find("@paper_ops_app.command(", start + 1)
    body = source[start:nxt] if nxt > 0 else source[start:]

    assert "compose_operations(" in body
    for forbidden in (
        "RuntimePlanner(",
        "AlpacaPaperAdapter(",
        "RuntimeSubmissionAuthority(",
        "CompounderMonthlyStrategy(",
        "CompounderMonthlyFractionableStrategy(",
        "fractionable_universe(",
        "deployment_capability(",
    ):
        assert forbidden not in body, forbidden


@pytest.mark.integration
def test_production_never_rebuilds_the_universe_from_the_provider() -> None:
    """§12. The frozen artefact is the authority; no run re-derives it."""
    runtime_dir = Path("src/stock_screener/paper_runtime")
    offenders: list[str] = []
    for path in sorted(runtime_dir.glob("*.py")):
        if path.name in {"universe.py", "deployment.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name in {"fractionable_universe", "mapping_from_evidence"}:
                offenders.append(f"{path.name}:{node.lineno} {name}()")

    assert offenders == []


# --------------------------------------------------------------------------- #
# §21-§23, §47-§48, §50, §58 execution
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_positive_intent_outside_the_deployment_universe_is_refused(
    paper_runtime: Any,
) -> None:
    """§47. Checked on the durable identity, never on the symbol."""
    from stock_screener.paper_runtime.submission import RuntimeSubmissionAuthority
    from stock_screener.portfolio_risk import SecurityIdentity, StrategyIntent

    runtime = author_v3(paper_runtime)
    authority = RuntimeSubmissionAuthority(
        root=runtime.root,
        adapter=None,
        risk_policy=None,
        risk_policy_fingerprint="",
        deployment_universe=runtime.universe,
        deployment_capability=runtime.capability,
    )
    outside = StrategyIntent(
        strategy_id="ALPHATRADER_COMPOUNDER_MONTHLY",
        strategy_version=STRATEGY_VERSION_V2,
        decision_id="d1",
        as_of=NOW,
        identity=SecurityIdentity(kind="COMPANY", identity_id=3, symbol="AAA"),
        requested_weight=0.5,
        priority=1.0,
        reason="tampered",
        source_fingerprint="fp",
    )

    with pytest.raises(RuntimeBindingError, match="outside this runtime's bound") as caught:
        authority._require_deployable([outside])

    assert caught.value.code is Code.RUNTIME_IDENTITY_NOT_IN_DEPLOYMENT_UNIVERSE


@pytest.mark.integration
def test_an_exit_intent_outside_the_universe_is_not_refused(paper_runtime: Any) -> None:
    """An intent at weight zero asks to stop holding something.

    Refusing to stop holding a name because it has since left the universe
    would strand the position, which is the opposite of safe.
    """
    from stock_screener.paper_runtime.submission import RuntimeSubmissionAuthority
    from stock_screener.portfolio_risk import SecurityIdentity, StrategyIntent

    runtime = author_v3(paper_runtime)
    authority = RuntimeSubmissionAuthority(
        root=runtime.root,
        adapter=None,
        risk_policy=None,
        risk_policy_fingerprint="",
        deployment_universe=runtime.universe,
        deployment_capability=runtime.capability,
    )
    exit_intent = StrategyIntent(
        strategy_id="ALPHATRADER_COMPOUNDER_MONTHLY",
        strategy_version=STRATEGY_VERSION_V2,
        decision_id="d1",
        as_of=NOW,
        identity=SecurityIdentity(kind="COMPANY", identity_id=3, symbol="CCC"),
        requested_weight=0.0,
        priority=0.0,
        reason="no longer selected",
        source_fingerprint="fp",
    )

    authority._require_deployable([exit_intent])


@pytest.mark.integration
def test_a_tampered_capability_claim_does_not_defeat_the_per_instrument_check(
    paper_runtime: Any,
) -> None:
    """§22/§48. The aggregate is not a licence.

    A capability object forged to claim support is handed to the adapter
    alongside a fractional quantity for a whole-share-only instrument. The
    instrument's own mapping entry must still refuse it.
    """
    from stock_screener.execution import ExecutionError
    from stock_screener.execution.external import (
        AlpacaPaperAdapter,
        BrokerCapabilities,
        BrokerEnvironment,
        ExternalBrokerAccountBinding,
        SubmissionIntentLog,
        SymbolMapping,
        VenueSymbol,
        prove_paper_environment,
    )

    runtime = author_v3(paper_runtime)
    forged = replace(runtime.capability, supported=True, verified_identities=999)
    venue_binding = ExternalBrokerAccountBinding.paper(
        provider="ALPACA",
        account_id=ACCOUNT_ID,
        proof=prove_paper_environment(
            environment=BrokerEnvironment.PAPER, base_url=PAPER, key_id="PKTESTKEY01"
        ),
        capabilities=BrokerCapabilities(fractional_quantities=True),
    )
    whole_share_only = SymbolMapping.of(
        [VenueSymbol(kind="COMPANY", identity_id=3, symbol="CCC", fractionable=False)]
    )
    # No transport: the per-instrument refusal happens before anything is sent,
    # which is the property under test. A transport here would be unreachable.
    adapter = AlpacaPaperAdapter(
        transport=cast("Any", None), binding=venue_binding, mapping=whole_share_only
    )

    from stock_screener.execution import Order, OrderSide
    from stock_screener.portfolio_risk import SecurityIdentity

    order = Order(
        order_id="ord-1",
        execution_plan_id="PLAN-1",
        authorization_fingerprint="f" * 64,
        identity=SecurityIdentity(kind="COMPANY", identity_id=3, symbol="CCC"),
        side=OrderSide.BUY,
        quantity=Decimal("1.5"),
        sizing_price=Decimal("100.00"),
        created_at=NOW,
    )

    assert forged.supported is True
    assert venue_binding.capabilities.fractional_quantities is True
    # The account-wide permission says yes and the forged proof says yes. The
    # instrument's own declared capability still refuses, which is the whole
    # point of keeping the per-instrument check underneath the aggregate.
    with pytest.raises(ExecutionError, match="whole-share-only"):
        adapter.submit(
            order,
            log=SubmissionIntentLog(runtime.root / "submissions"),
            recorded_at=NOW,
        )


@pytest.mark.integration
def test_the_per_instrument_check_and_the_asset_read_cannot_disagree() -> None:
    """The certified canary's own guard and the submission guard share a source.

    The canary establishes fractionability from a live asset read, refuses a
    fraction when the venue says no, and widens the binding. It must also record
    the fact on the mapping entry, because the submission path checks the
    entry per order — and two guards reading different sources about the same
    instrument is how one of them comes to be wrong.
    """
    import stock_screener.execution.external.canary as module

    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    declared = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_declare_fractionable"
    ]
    widened = source.count("fractional_quantities=True")

    assert len(declared) == widened, (
        "every site that widens the binding's fractional capability must also "
        "declare it on the mapping entry"
    )


@pytest.mark.integration
def test_a_fractional_order_for_an_undeclared_instrument_is_refused() -> None:
    """§22. The mapping's default is whole shares, and the default is enforced.

    Driven through the real `submit_order`, with the account-wide capability
    saying yes. The instrument never declared itself fractionable, so it is
    refused — an undeclared capability is not a permitted one.
    """
    from stock_screener.execution import ExecutionError, Order, OrderSide
    from stock_screener.execution.external import (
        BrokerCapabilities,
        BrokerEnvironment,
        ExternalBrokerAccountBinding,
        SymbolMapping,
        VenueSymbol,
        prove_paper_environment,
    )
    from stock_screener.portfolio_risk import SecurityIdentity

    venue_binding = ExternalBrokerAccountBinding.paper(
        provider="ALPACA",
        account_id=ACCOUNT_ID,
        proof=prove_paper_environment(
            environment=BrokerEnvironment.PAPER, base_url=PAPER, key_id="PKTESTKEY01"
        ),
        capabilities=BrokerCapabilities(fractional_quantities=True),
    )
    undeclared = SymbolMapping.of([VenueSymbol(kind="COMPANY", identity_id=1, symbol="AAA")])
    order = Order(
        order_id="ord-1",
        execution_plan_id="PLAN-1",
        authorization_fingerprint="f" * 64,
        identity=SecurityIdentity(kind="COMPANY", identity_id=1, symbol="AAA"),
        side=OrderSide.BUY,
        quantity=Decimal("1.5"),
        sizing_price=Decimal("100.00"),
        created_at=NOW,
    )

    assert next(iter(undeclared.entries)).fractionable is False
    with pytest.raises(ExecutionError, match="whole-share-only"):
        from stock_screener.execution.external import submit_order

        submit_order(
            order=order,
            binding=venue_binding,
            mapping=undeclared,
            transport=cast("Any", None),
            log=cast("Any", None),
            recorded_at=NOW,
        )


@pytest.mark.integration
def test_the_submission_authority_refuses_an_unproven_bound_capability(
    paper_runtime: Any,
) -> None:
    from stock_screener.paper_runtime.submission import RuntimeSubmissionAuthority

    runtime = author_v3(paper_runtime)
    unproven = replace(runtime.capability, supported=False)
    authority = RuntimeSubmissionAuthority(
        root=runtime.root,
        adapter=None,
        risk_policy=None,
        risk_policy_fingerprint="",
        deployment_universe=runtime.universe,
        deployment_capability=unproven,
    )

    with pytest.raises(RuntimeBindingError, match="is not proven for this deployment"):
        authority._require_deployable([])


@pytest.mark.integration
def test_no_production_path_reshapes_a_quantity(paper_runtime: Any) -> None:
    """§23. No rounding, truncation, ceil or floor of an approved quantity."""
    offenders: list[str] = []
    for directory in (
        Path("src/stock_screener/paper_runtime"),
        Path("src/stock_screener/execution/external"),
    ):
        for path in sorted(directory.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                if name in {"quantize", "ceil", "floor", "trunc"}:
                    offenders.append(f"{path.name}:{node.lineno} {name}()")
                if name == "round" and not isinstance(node.func, ast.Attribute):
                    offenders.append(f"{path.name}:{node.lineno} round()")

    assert offenders == []


@pytest.mark.integration
def test_the_execution_path_consumes_the_persisted_decision_without_reranking(
    paper_runtime: Any,
) -> None:
    """§9/§50. Decision first, then execution over the frozen record."""
    runtime = author_v3(paper_runtime)
    venue = ScriptedVenue()

    decide = composed(paper_runtime, now=DECISION_NOW, venue=venue)
    try:
        run_through(decide)
    finally:
        decide.close()
    decided = _pending(runtime.root)
    assert decided is not None
    before = decided.decision.fingerprint()
    recorded = decided.decision_fingerprint

    # The execution run re-reads the frozen record. Nothing re-ranks: the
    # decision's own digest is what the store verifies before executing, so a
    # re-derived selection would be refused as DECISION_TAMPERED rather than
    # quietly replacing the authorised one.
    execute = composed(paper_runtime, now=EXECUTION_NOW, venue=venue)
    try:
        run_through(execute)
    finally:
        execute.close()
    from stock_screener.paper_runtime.decisions import StrategyDecisionStore

    after = StrategyDecisionStore(runtime.root).all()[-1]

    assert after.decision.fingerprint() == before
    assert after.decision_fingerprint == recorded
    assert after.decision.strategy_version == STRATEGY_VERSION_V2
    assert after.decision.deployment_universe_fingerprint == runtime.universe.fingerprint()
    assert after.decision.target_count == decided.decision.target_count


def _pending(root: Path) -> Any:
    """The decision awaiting execution in this root, if any."""
    from stock_screener.paper_runtime.decisions import StrategyDecisionStore

    return StrategyDecisionStore(root).pending()


@pytest.mark.integration
def test_a_fractional_quantity_reaches_the_venue_unchanged(paper_runtime: Any) -> None:
    """§58. The whole chain, with a scripted network and nothing rounded."""
    runtime = author_v3(paper_runtime)
    venue = ScriptedVenue()

    decide = composed(paper_runtime, now=DECISION_NOW, venue=venue)
    try:
        run_through(decide)
    finally:
        decide.close()

    execute = composed(paper_runtime, now=EXECUTION_NOW, venue=venue)
    try:
        run_through(execute)
    finally:
        execute.close()

    posted = [row for row in venue.mutations if row["path"] == "/v2/orders"]

    assert len(posted) == 1
    quantity = Decimal(str(posted[0]["json"]["qty"]))
    assert quantity != quantity.to_integral_value()
    # The whole-share name is still in the full mapping, and was never ordered.
    assert {row["json"]["symbol"] for row in posted} <= set(FRACTIONABLE.values())
    assert {row.identity_id for row in runtime.mapping.records} == set(ALL_SYMBOLS)


# --------------------------------------------------------------------------- #
# §61-§66 contracts, authorities and the real runtime
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_observability_contract_is_unchanged() -> None:
    from stock_screener.paper_runtime.observability import (
        OBSERVABILITY_VERSION,
        observability_fingerprint,
    )

    assert OBSERVABILITY_VERSION == "PAPER_OPERATIONS_OBSERVABILITY_V1"
    assert (
        observability_fingerprint()
        == "0fa85983277222e83f8ae97ed3266fc0b830b958ddd21c1a54280fa88cdd7d5b"
    )


@pytest.mark.integration
def test_the_gate_evidence_contract_is_unchanged() -> None:
    from stock_screener.soak_verification.evidence_contract import (
        EVIDENCE_CONTRACT_VERSION,
        evidence_contract,
    )

    assert EVIDENCE_CONTRACT_VERSION == "SOAK_GATE_EVIDENCE_CONTRACT_V1"
    assert (
        evidence_contract().fingerprint()
        == "eb612d02dd3896f816ac31fd60a0cd448e08ec5b444690a4896aa03239e8b585"
    )


@pytest.mark.integration
def test_both_strategy_authorities_keep_their_fingerprints() -> None:
    """§63. This phase makes production resolve them, and changes neither."""
    universe = fractionable_universe(full_mapping())

    assert (
        CompounderMonthlyStrategy().fingerprint()
        == "5aa71b40b7692eaa906649330eb4901bd4a87e6d394674c1245713400d5a3ce8"
    )
    assert (
        v2_strategy(universe).fingerprint()
        == "a15887f81fa6dca1c5cd032ffbcbe70c32df0d3755c1d4b7911609ec4bd1d840"
    )


@pytest.mark.integration
def test_the_v2_paper_risk_policy_is_unchanged() -> None:
    """§64."""
    assert (
        paper_risk_policy_v2().fingerprint()
        == "9b367ed04a448ea71e19bf064d4ffb7bcc6d06fcd832fd99c246746e2cc3f04b"
    )
    assert paper_risk_policy_v2().engine_version == "PORTFOLIO_RISK_ENGINE_V1_3"


@pytest.mark.integration
def test_the_real_runtime_root_is_still_bound_to_v1() -> None:
    """§66. Nothing in this phase touched the repository's own runtime."""
    real = REPO_ROOT / ".operations"
    assert (real / "runtime" / "binding.json").exists(), (
        "this checkout has no real runtime root, so the assertion that this "
        "phase left it alone cannot be made at all"
    )
    body = json.loads((real / "runtime" / "binding.json").read_text(encoding="utf-8"))

    assert body["version"] == "PAPER_OPERATIONS_RUNTIME_BINDING_V1"
    assert body["strategy_version"] == STRATEGY_VERSION
    assert not (real / "runtime" / "bindings").exists()
    for name in (
        "risk_policy.json",
        "symbol_mapping.json",
        DEPLOYMENT_UNIVERSE_FILENAME,
        DEPLOYMENT_CAPABILITY_FILENAME,
    ):
        assert not (real / "runtime" / name).exists(), name


@pytest.mark.integration
def test_no_v3_soak_was_created() -> None:
    """§69."""
    real = REPO_ROOT / ".operations"

    assert real.exists()
    assert not (real / "soak" / "v3").exists()
    assert sorted(path.name for path in (real / "soak").iterdir()) == [
        "activation.json",
        "drills",
        "protocol.json",
        "state.json",
        "v2",
    ]
