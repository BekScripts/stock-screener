"""P2.3.1J — "is this my account?" and "may this send a fraction?" are two questions.

P2.3.1H2 established the defect by measurement. The real runtime's recorded
`account_binding_fingerprint` was computed with `fractional_quantities=False`,
and `ExternalBrokerAccountBinding.body()` digests capabilities — so when a
deployment proved fractional support for its own universe and production widened
the adapter's capability to send those fractions, the identity check compared a
different digest and reported *"the venue answered for an account this runtime is
not bound to"* about the correct account.

One fingerprint was carrying two authorities. The account's identity is a fact
about the venue; a deployment's execution capability is a fact about a frozen
universe. They have different evidence and different owners, and while they
shared a digest the only way to express a capability change was to appear to
change accounts.

These tests pin the separation, and the case that matters most is the one that
previously failed: a bootstrap-era whole-share binding and a fractionable
deployment, both proving true at once, with the historical fingerprint untouched.
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

from stock_screener.execution import ExecutionError
from stock_screener.execution.external import (
    ACCOUNT_IDENTITY_CONTRACT,
    BrokerCapabilities,
    BrokerEnvironment,
    ExternalBrokerAccountBinding,
    ExternalBrokerAccountIdentity,
    account_fingerprint,
    account_identity_of,
    prove_paper_environment,
)
from stock_screener.paper_runtime import (
    DEPLOYMENT_UNIVERSE_VERSION,
    IMMUTABLE_ACROSS_REBIND,
    RUNTIME_BINDING_V3_VERSION,
    RUNTIME_BINDING_V4_VERSION,
    STRATEGY_REVISION_FIELDS,
    V3_DEPLOYMENT_FIELDS,
    V4_IDENTITY_FIELDS,
    Code,
    PaperOperationsRuntimeBindingV4,
    RuntimeBindingError,
    RuntimeBindingHistory,
    account_capability_from_mapping,
    active_binding,
    deployment_capability,
    fractionable_universe,
    mapping_from_evidence,
    paper_risk_policy_v2,
    read_account_identity,
    revise_deployment,
    write_account_identity,
    write_deployment_capability,
    write_deployment_universe,
    write_risk_policy,
    write_strategy_authorization,
    write_symbol_mapping,
)
from stock_screener.paper_runtime.composition import compose_operations, composition_readiness
from stock_screener.paper_runtime.operator_inputs import (
    ACCOUNT_IDENTITY_FILENAME,
    DEPLOYMENT_CAPABILITY_FILENAME,
    OPERATOR_INPUT_FILES,
    REQUIRED_OPERATOR_INPUTS,
)
from stock_screener.strategies import (
    STRATEGY_VERSION_V2,
    CompounderMonthlyFractionableStrategy,
    StrategyDeploymentAuthorizationV2,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTEFACTS = REPO_ROOT / "docs" / "reference" / "deployment"
PAPER = "https://paper-api.alpaca.markets"
ACCOUNT_ID = "paper-account-1"
OTHER_ACCOUNT_ID = "paper-account-9"
NOW = "2026-11-02T14:35:00+00:00"
DECISION_NOW = "2026-10-30T20:00:30+00:00"
EXECUTION_NOW = "2026-11-02T14:35:00+00:00"

#: Two fractionable names and one whole-share-only, which is the real account's
#: shape in miniature: 1,821 of 1,880 routable instruments are fractionable.
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
    account_id: str = ACCOUNT_ID

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
                    "id": self.account_id,
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
    """The full mapping: fractionable *and* whole-share-only routable assets."""
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


def whole_share_binding(account_id: str = ACCOUNT_ID) -> ExternalBrokerAccountBinding:
    """A bootstrap-era binding at the conservative whole-shares-only default.

    This is the real runtime's shape: `fractional_quantities=False`, because
    that is what bootstrap declared before any deployment had proven otherwise.
    """
    return ExternalBrokerAccountBinding.paper(
        provider="ALPACA",
        account_id=account_id,
        proof=prove_paper_environment(
            environment=BrokerEnvironment.PAPER, base_url=PAPER, key_id="PKTESTKEY01"
        ),
        capabilities=BrokerCapabilities(fractional_quantities=False),
    )


def identity(account_id: str = ACCOUNT_ID) -> ExternalBrokerAccountIdentity:
    """The account identity for a paper account."""
    return ExternalBrokerAccountIdentity(
        provider="ALPACA",
        environment=BrokerEnvironment.PAPER,
        account_fingerprint_hex=account_fingerprint(account_id),
    )


def grant_for(strategy: Any, universe: Any) -> StrategyDeploymentAuthorizationV2:
    """A V2 grant covering exactly this strategy and universe."""
    return StrategyDeploymentAuthorizationV2(
        authorization_id="TEST_GRANT_V2",
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        strategy_fingerprint=strategy.fingerprint(),
        deployment_universe_version=DEPLOYMENT_UNIVERSE_VERSION,
        deployment_universe_fingerprint=universe.fingerprint(),
        code_commit="52db0fd",
        created_at=NOW,
    )


def author_policy(root: Path, **overrides: Any) -> None:
    """Author the risk policy an operator owes. Wide, and never authorised."""
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


@dataclass
class V4Runtime:
    """A temporary runtime revised to the V4 deployment authority."""

    root: Path
    binding: Any
    universe: Any
    capability: Any
    mapping: Any
    strategy: Any
    identity: Any
    predecessor: Any


def author_v4(
    paper_runtime: Any,
    *,
    universe: Any = None,
    mapping: Any = None,
    account_id: str = ACCOUNT_ID,
    write_capability: bool = True,
    write_identity: bool = True,
) -> V4Runtime:
    """Revise a temporary runtime to a V4 deployment, writing the six artefacts."""
    root = paper_runtime.root
    built_mapping = mapping if mapping is not None else full_mapping()
    built_universe = universe if universe is not None else fractionable_universe(built_mapping)
    capability = deployment_capability(built_mapping, built_universe)
    strategy = CompounderMonthlyFractionableStrategy(built_universe)
    account = identity(account_id)
    predecessor = active_binding(root)

    author_policy(root)
    write_symbol_mapping(
        root,
        [
            {
                "kind": "COMPANY",
                "identity_id": key,
                "symbol": value,
                "fractionable": value not in set(WHOLE_SHARE.values()),
            }
            for key, value in ALL_SYMBOLS.items()
        ],
    )
    write_deployment_universe(root, built_universe)
    if write_capability:
        write_deployment_capability(root, capability)
    write_strategy_authorization(root, grant_for(strategy, built_universe))
    if write_identity:
        write_account_identity(root, account)

    binding = revise_deployment(
        root=root,
        strategy=strategy,
        authorization=grant_for(strategy, built_universe),
        risk_policy=paper_risk_policy_v2(),
        mapping=built_mapping,
        universe=built_universe,
        capability=capability,
        account_binding_fingerprint=paper_runtime.account,
        account_identity=account,
        reason="separate account identity from deployment capability",
        now=NOW,
        code_commit="52db0fd",
        confirmed=True,
    )
    return V4Runtime(
        root=root,
        binding=binding,
        universe=built_universe,
        capability=capability,
        mapping=built_mapping,
        strategy=strategy,
        identity=account,
        predecessor=predecessor,
    )


def marks() -> dict[str, Decimal]:
    """A sizing mark that yields a genuinely fractional quantity."""
    return {f"COMPANY:{key}": Decimal("123.45") for key in ALL_SYMBOLS}


def bundle_for(session: str, count: int = 21) -> Any:
    """The research cross-section for a decision session."""
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


def composed(runtime: Any, *, now: str, venue: ScriptedVenue, binding: Any = None) -> Any:
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
        mode=OperationsMode.EXTERNAL_PAPER,
        clock=runtime.clock,
        code_commit="52db0fd",
        bundle_for=bundle_for,
        client=venue.client(),
        marks=marks(),
    )


def run_through(composition: Any) -> Any:
    """Drive one slot through the real coordinator, which supplies the gate."""
    from stock_screener.paper_operations import OperationsMode

    return composition.coordinator.run_due(
        mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=composition.planner
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
        code_commit="52db0fd",
    )


# --------------------------------------------------------------------------- #
# §1, §3, §4, §13, §23 the identity contract
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_account_identity_excludes_every_capability_field() -> None:
    """§3. Four keys, and each omission is load-bearing."""
    body = identity().body()

    assert set(body) == {"contract", "provider", "environment", "account_fingerprint"}
    for forbidden in (
        "capabilities",
        "fractional_quantities",
        "deployment_universe_fingerprint",
        "instrument_mapping_fingerprint",
        "risk_policy_fingerprint",
        "strategy_fingerprint",
    ):
        assert forbidden not in body, forbidden


@pytest.mark.integration
def test_account_identity_is_unchanged_by_widening_a_capability() -> None:
    """The measurement that defines this phase.

    The same account under whole-share and fractional capabilities: the full
    binding digest moves, the identity digest does not.
    """
    conservative = whole_share_binding()
    widened = replace(
        conservative,
        capabilities=replace(conservative.capabilities, fractional_quantities=True),
    )

    assert conservative.binding_fingerprint() != widened.binding_fingerprint()
    assert (
        account_identity_of(conservative).fingerprint()
        == account_identity_of(widened).fingerprint()
    )


@pytest.mark.integration
def test_a_different_account_still_fingerprints_differently() -> None:
    """The separation narrows what identity depends on, not how strictly."""
    assert identity().fingerprint() != identity(OTHER_ACCOUNT_ID).fingerprint()


@pytest.mark.integration
def test_account_identity_carries_no_account_number_or_credential() -> None:
    """§4."""
    from stock_screener.execution.external import check_no_credentials

    rendered = json.dumps(identity().as_json())

    check_no_credentials(identity().as_json())
    assert ACCOUNT_ID not in rendered
    assert "PA123" not in rendered
    assert "PKTESTKEY01" not in rendered


@pytest.mark.integration
def test_account_identity_names_its_own_scope() -> None:
    """§13. A stored identity cannot be mistaken for a capability claim."""
    rendered = identity().as_json()

    assert rendered["scope"].startswith("ACCOUNT_IDENTITY_ONLY")
    assert rendered["capability_fields_included"] == []
    assert rendered["live_trading"] == "NOT_IMPLEMENTED_NOT_AUTHORIZED"


@pytest.mark.integration
def test_the_contract_is_additive_and_named() -> None:
    assert ACCOUNT_IDENTITY_CONTRACT == "EXTERNAL_BROKER_ACCOUNT_IDENTITY_V1"
    assert identity().body()["contract"] == ACCOUNT_IDENTITY_CONTRACT


@pytest.mark.integration
def test_the_historical_account_binding_digest_is_unchanged() -> None:
    """§1/§23. The bootstrap-era digest still means what it meant.

    Reproduced from the same inputs the real bootstrap used — a whole-share
    binding over this account — so a change to `body()` would fail here rather
    than silently rewrite recorded evidence.
    """
    expected = whole_share_binding().binding_fingerprint()

    assert "capabilities" in whole_share_binding().body()
    assert whole_share_binding().binding_fingerprint() == expected
    assert whole_share_binding().body()["capabilities"]["fractional_quantities"] is False


@pytest.mark.integration
def test_a_live_account_identity_cannot_be_constructed() -> None:
    """§17."""
    with pytest.raises(ExecutionError, match=r"LIVE|live"):
        ExternalBrokerAccountIdentity(
            provider="ALPACA",
            environment=BrokerEnvironment.LIVE,
            account_fingerprint_hex=account_fingerprint(ACCOUNT_ID),
        )


@pytest.mark.integration
def test_a_short_account_fingerprint_is_refused() -> None:
    with pytest.raises(ExecutionError, match="not a sha256 digest"):
        ExternalBrokerAccountIdentity(
            provider="ALPACA", environment=BrokerEnvironment.PAPER, account_fingerprint_hex="abc"
        )


@pytest.mark.integration
def test_require_matches_names_a_different_provider() -> None:
    """§17. A provider change is never inferred from a deployment change."""
    other = replace(identity(), provider="OTHER_BROKER")

    with pytest.raises(ExecutionError, match="provider"):
        identity().require_matches(other, context="probing")


@pytest.mark.integration
def test_require_matches_names_a_different_account() -> None:
    """§16. "Wrong account" and "wrong venue" need different responses."""
    other = replace(identity(), account_fingerprint_hex=account_fingerprint(OTHER_ACCOUNT_ID))

    with pytest.raises(ExecutionError, match="not bound to"):
        identity().require_matches(other, context="probing")


@pytest.mark.integration
def test_require_matches_accepts_the_same_account() -> None:
    identity().require_matches(identity(), context="probing")


# --------------------------------------------------------------------------- #
# §26 round trip
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_identity_round_trips_through_its_own_serialisation() -> None:
    issued = identity()

    reloaded = ExternalBrokerAccountIdentity.from_body(issued.as_json())

    assert reloaded == issued
    assert reloaded.fingerprint() == issued.fingerprint()


@pytest.mark.integration
def test_the_identity_round_trips_through_the_operator_artefact(paper_runtime: Any) -> None:
    issued = identity()
    path = write_account_identity(paper_runtime.root, issued)

    reloaded = read_account_identity(paper_runtime.root, expected_fingerprint=issued.fingerprint())

    assert path.name == ACCOUNT_IDENTITY_FILENAME
    assert reloaded.fingerprint() == issued.fingerprint()


@pytest.mark.integration
def test_every_identity_field_is_read_back_by_name() -> None:
    """No field may exist only on the writer."""
    issued = identity()
    perturbations: dict[str, Any] = {
        "provider": "OTHER_BROKER",
        "account_fingerprint_hex": account_fingerprint(OTHER_ACCOUNT_ID),
    }

    for name, value in perturbations.items():
        changed = replace(issued, **{name: value})
        reloaded = ExternalBrokerAccountIdentity.from_body(changed.as_json())

        assert getattr(reloaded, name) == value, name
        assert reloaded.fingerprint() != issued.fingerprint(), name


@pytest.mark.integration
def test_a_body_of_another_contract_is_refused() -> None:
    body = identity().as_json()
    body["contract"] = "EXTERNAL_BROKER_ACCOUNT_IDENTITY_V99"

    with pytest.raises(ExecutionError, match="declares contract"):
        ExternalBrokerAccountIdentity.from_body(body)


@pytest.mark.integration
def test_a_body_missing_a_required_field_is_refused() -> None:
    body = identity().as_json()
    del body["account_fingerprint"]

    with pytest.raises(ExecutionError, match="omits"):
        ExternalBrokerAccountIdentity.from_body(body)


@pytest.mark.integration
def test_a_body_with_an_unknown_environment_is_refused() -> None:
    body = identity().as_json()
    body["environment"] = "SOMETHING_ELSE"

    with pytest.raises(ExecutionError, match="not a known environment"):
        ExternalBrokerAccountIdentity.from_body(body)


@pytest.mark.integration
def test_a_tampered_identity_artefact_fails_closed(paper_runtime: Any) -> None:
    issued = identity()
    path = write_account_identity(paper_runtime.root, issued)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["account_fingerprint"] = account_fingerprint(OTHER_ACCOUNT_ID)
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(RuntimeBindingError, match="digests to") as caught:
        read_account_identity(paper_runtime.root, expected_fingerprint=issued.fingerprint())

    assert caught.value.code is Code.RUNTIME_OPERATOR_INPUT_FINGERPRINT_MISMATCH


@pytest.mark.integration
def test_a_missing_identity_artefact_fails_closed(paper_runtime: Any) -> None:
    with pytest.raises(RuntimeBindingError, match="has not been authored") as caught:
        read_account_identity(paper_runtime.root)

    assert caught.value.code is Code.RUNTIME_ACCOUNT_IDENTITY_NOT_AUTHORED


# --------------------------------------------------------------------------- #
# §11, §12, §27 the V4 binding
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_prior_binding_contracts_are_unchanged() -> None:
    """§11."""
    from stock_screener.paper_runtime import RUNTIME_BINDING_V2_VERSION
    from stock_screener.paper_runtime.binding import PAPER_OPERATIONS_RUNTIME_BINDING_VERSION

    assert PAPER_OPERATIONS_RUNTIME_BINDING_VERSION == "PAPER_OPERATIONS_RUNTIME_BINDING_V1"
    assert RUNTIME_BINDING_V2_VERSION == "PAPER_OPERATIONS_RUNTIME_BINDING_V2"
    assert RUNTIME_BINDING_V3_VERSION == "PAPER_OPERATIONS_RUNTIME_BINDING_V3"
    assert RUNTIME_BINDING_V4_VERSION == "PAPER_OPERATIONS_RUNTIME_BINDING_V4"


@pytest.mark.integration
def test_a_v4_binding_distinguishes_identity_from_bootstrap_provenance(
    paper_runtime: Any,
) -> None:
    """§6/§12. Two fields, two meanings, both recorded."""
    runtime = author_v4(paper_runtime)

    assert runtime.binding.version == RUNTIME_BINDING_V4_VERSION
    assert runtime.binding.account_identity_fingerprint == runtime.identity.fingerprint()
    assert (
        runtime.binding.bootstrap_account_binding_fingerprint
        == runtime.predecessor.account_binding_fingerprint
    )


@pytest.mark.integration
def test_a_v4_binding_makes_no_account_wide_fractional_claim(paper_runtime: Any) -> None:
    """§13."""
    runtime = author_v4(paper_runtime)
    body = runtime.binding.body()

    assert body["account_wide_fractional_claim"] is False
    assert body["account_capability"]["fractional_quantities"] is False
    assert body["account_capability"]["whole_share_only_instruments"] == len(WHOLE_SHARE)


@pytest.mark.integration
def test_a_v4_binding_refuses_a_blank_identity_field() -> None:
    """An absent identity would silently fall back to the legacy digest."""
    with pytest.raises(RuntimeBindingError, match="must state") as caught:
        PaperOperationsRuntimeBindingV4(
            version=RUNTIME_BINDING_V4_VERSION,
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
            deployment_universe_fingerprint="e" * 64,
            deployment_capability_fingerprint="f" * 64,
            account_identity_fingerprint="",
            bootstrap_account_binding_fingerprint="g" * 64,
        )

    assert "account_identity_fingerprint" in str(caught.value)
    assert caught.value.code is Code.RUNTIME_REBIND_REFUSED


@pytest.mark.integration
def test_every_v4_field_round_trips_to_an_identical_body(paper_runtime: Any) -> None:
    """§27. No field exists only on write or only on read."""
    runtime = author_v4(paper_runtime)

    reloaded = active_binding(runtime.root)

    assert reloaded.version == RUNTIME_BINDING_V4_VERSION
    assert reloaded.body() == runtime.binding.body()
    assert reloaded.fingerprint() == runtime.binding.fingerprint()
    for name in (*V3_DEPLOYMENT_FIELDS, *V4_IDENTITY_FIELDS):
        assert getattr(reloaded, name) == getattr(runtime.binding, name), name


@pytest.mark.integration
def test_the_v4_chain_verifies(paper_runtime: Any) -> None:
    runtime = author_v4(paper_runtime)
    history = RuntimeBindingHistory(runtime.root)

    assert [row.sequence for row in history.records()] == [1]
    assert history.verify()["chain_verified"] is True


@pytest.mark.integration
def test_a_v3_transition_is_still_available_unchanged(paper_runtime: Any) -> None:
    """Additive: omitting the identity produces exactly the V3 binding."""
    mapping = full_mapping()
    universe = fractionable_universe(mapping)
    strategy = CompounderMonthlyFractionableStrategy(universe)
    author_policy(paper_runtime.root)

    binding = revise_deployment(
        root=paper_runtime.root,
        strategy=strategy,
        authorization=grant_for(strategy, universe),
        risk_policy=paper_risk_policy_v2(),
        mapping=mapping,
        universe=universe,
        capability=deployment_capability(mapping, universe),
        account_binding_fingerprint=paper_runtime.account,
        reason="r",
        now=NOW,
        confirmed=True,
    )

    assert binding.version == RUNTIME_BINDING_V3_VERSION
    assert not hasattr(binding, "account_identity_fingerprint")


# --------------------------------------------------------------------------- #
# §15, §16 lineage
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_separation_needs_no_economic_rebootstrap(paper_runtime: Any) -> None:
    """§15. Capability metadata is not economic state."""
    before = paper_runtime.binding
    ledger = paper_runtime.root / "runtime" / "portfolio" / "ledger"
    events_before = sorted(path.name for path in ledger.glob("*.json"))
    opening_before = _opening_balance_events(ledger)

    runtime = author_v4(paper_runtime)

    assert sorted(path.name for path in ledger.glob("*.json")) == events_before
    assert _opening_balance_events(ledger) == opening_before
    for name in IMMUTABLE_ACROSS_REBIND:
        if name in STRATEGY_REVISION_FIELDS:
            continue
        assert getattr(runtime.binding, name) == getattr(before, name), name


def _opening_balance_events(ledger: Path) -> int:
    """How many opening-balance events the ledger holds."""
    return sum(
        1
        for path in ledger.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["event"]["event_type"] == "OPENING_BALANCE"
    )


@pytest.mark.integration
def test_the_execution_journal_is_untouched_by_the_separation(paper_runtime: Any) -> None:
    execution = paper_runtime.root / "runtime" / "execution"
    before = {path.name: path.read_bytes() for path in sorted(execution.glob("**/*.json"))}

    author_v4(paper_runtime)

    assert {path.name: path.read_bytes() for path in sorted(execution.glob("**/*.json"))} == before


# --------------------------------------------------------------------------- #
# §18-§22, §24, §25 the two proofs, separately
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_real_historical_shape_proves_both_authorities(paper_runtime: Any) -> None:
    """§18. The case that previously failed.

    A bootstrap-era whole-share binding, a mapping holding both kinds of
    instrument, a universe of only the fractionable ones. Account identity and
    deployment capability must both be true at once, with no account mismatch.
    """
    runtime = author_v4(paper_runtime)
    venue = ScriptedVenue()
    composition = composed(paper_runtime, now=DECISION_NOW, venue=venue)
    try:
        proof = composition.environment.prove_paper_environment()
        resolved = composition.planner.strategy
    finally:
        composition.close()

    assert whole_share_binding().capabilities.fractional_quantities is False
    assert proof["proven"] is True
    assert proof["account_identity_authority"] == ACCOUNT_IDENTITY_CONTRACT
    assert runtime.capability.supported is True
    assert resolved.strategy_version == STRATEGY_VERSION_V2


@pytest.mark.integration
def test_the_identity_proof_reads_identity_not_the_binding_digest(
    paper_runtime: Any,
) -> None:
    """§5. The proof compares the identity authority, and says so."""
    runtime = author_v4(paper_runtime)
    venue = ScriptedVenue()
    composition = composed(paper_runtime, now=DECISION_NOW, venue=venue)
    try:
        proof = composition.environment.prove_paper_environment()
        widened = composition.adapter.binding
    finally:
        composition.close()

    # The adapter's binding *was* widened for the deployment, which is what used
    # to break the proof.
    assert widened.capabilities.fractional_quantities is True
    assert proof["account_identity_fingerprint"] == runtime.identity.fingerprint()
    assert proof["account_identity_fingerprint"] != widened.binding_fingerprint()
    # And the key every other consumer reads still means the account binding
    # this runtime is bound to, so an operating grant goes on matching.
    assert proof["account_binding_fingerprint"] == runtime.binding.account_binding_fingerprint


@pytest.mark.integration
def test_a_legacy_binding_is_still_proven_against_its_own_digest(
    paper_runtime: Any,
) -> None:
    """§1. A V1 root keeps the semantics it was written under."""
    author_policy(paper_runtime.root)
    write_symbol_mapping(
        paper_runtime.root,
        [
            {"kind": "COMPANY", "identity_id": key, "symbol": value, "fractionable": True}
            for key, value in ALL_SYMBOLS.items()
        ],
    )
    venue = ScriptedVenue()
    composition = composed(
        paper_runtime, now=DECISION_NOW, venue=venue, binding=paper_runtime.binding
    )
    try:
        proof = composition.environment.prove_paper_environment()
    finally:
        composition.close()

    assert proof["account_identity_authority"] == "ACCOUNT_BINDING_DIGEST_LEGACY"
    assert proof["proven"] is True


@pytest.mark.integration
def test_a_wrong_account_still_fails_and_capability_does_not_mask_it(
    paper_runtime: Any,
) -> None:
    """§19. Deployment capability is proven and the account is still refused."""
    runtime = author_v4(paper_runtime)
    wrong = identity(OTHER_ACCOUNT_ID)

    assert runtime.capability.supported is True
    with pytest.raises(ExecutionError, match="not bound to"):
        wrong.require_matches(runtime.identity, context="probing")


@pytest.mark.integration
def test_composition_refuses_an_identity_for_another_account(paper_runtime: Any) -> None:
    """The configured venue must be the account the binding names."""
    runtime = author_v4(paper_runtime, account_id=OTHER_ACCOUNT_ID)
    venue = ScriptedVenue()

    with pytest.raises(RuntimeBindingError, match="configuration does not reach") as caught:
        composed(paper_runtime, now=DECISION_NOW, venue=venue).close()

    assert caught.value.code is Code.RUNTIME_BINDING_INVALID
    assert runtime.identity.fingerprint() != identity().fingerprint()


@pytest.mark.integration
def test_a_whole_share_universe_member_fails_capability_while_identity_passes(
    paper_runtime: Any,
) -> None:
    """§20. The test that proves the authorities are genuinely separate."""
    mapping = full_mapping()
    good = fractionable_universe(mapping)
    bad = replace(good, members=(("COMPANY", 1), ("COMPANY", 3)))

    account = identity()
    capability = deployment_capability(mapping, bad)

    # Identity is unaffected by the deployment being wrong.
    account.require_matches(identity(), context="probing")
    assert capability.supported is False
    assert capability.non_fractionable_identities == ("COMPANY:3",)
    with pytest.raises(RuntimeBindingError, match="is not proven for this deployment"):
        capability.require_supported()


@pytest.mark.integration
def test_a_whole_share_member_blocks_the_v4_transition(paper_runtime: Any) -> None:
    mapping = full_mapping()
    bad = replace(fractionable_universe(mapping), members=(("COMPANY", 1), ("COMPANY", 3)))

    with pytest.raises(RuntimeBindingError, match="is not proven for this deployment"):
        author_v4(paper_runtime, mapping=mapping, universe=bad)


@pytest.mark.integration
def test_a_missing_deployment_capability_fails_closed(paper_runtime: Any) -> None:
    """§21."""
    runtime = author_v4(paper_runtime)
    (runtime.root / "runtime" / DEPLOYMENT_CAPABILITY_FILENAME).unlink()

    report = readiness(paper_runtime, binding=active_binding(runtime.root))

    assert report["ready"] is False
    assert "RUNTIME_DEPLOYMENT_CAPABILITY_NOT_AUTHORED" in report["detail"]


@pytest.mark.integration
def test_a_missing_account_identity_fails_closed(paper_runtime: Any) -> None:
    runtime = author_v4(paper_runtime)
    (runtime.root / "runtime" / ACCOUNT_IDENTITY_FILENAME).unlink()

    report = readiness(paper_runtime, binding=active_binding(runtime.root))

    assert report["ready"] is False
    assert "RUNTIME_ACCOUNT_IDENTITY_NOT_AUTHORED" in report["detail"]


@pytest.mark.integration
def test_a_tampered_capability_claiming_support_is_refused(paper_runtime: Any) -> None:
    """§22. A proof is refused on its digest before its claim is believed."""
    runtime = author_v4(paper_runtime)
    path = runtime.root / "runtime" / DEPLOYMENT_CAPABILITY_FILENAME
    body = json.loads(path.read_text(encoding="utf-8"))
    body["supported"] = True
    body["non_fractionable_identities"] = []
    body["verified_identities"] = 999
    path.write_text(json.dumps(body), encoding="utf-8")

    report = readiness(paper_runtime, binding=active_binding(runtime.root))

    assert report["ready"] is False
    assert "RUNTIME_OPERATOR_INPUT_FINGERPRINT_MISMATCH" in report["detail"]


@pytest.mark.integration
def test_the_production_chain_sends_a_fractional_quantity_unchanged(
    paper_runtime: Any,
) -> None:
    """§24. Whole-share bootstrap, fractionable deployment, nothing rounded."""
    runtime = author_v4(paper_runtime)
    venue = ScriptedVenue()

    decide = composed(paper_runtime, now=DECISION_NOW, venue=venue)
    try:
        decided = run_through(decide)
    finally:
        decide.close()
    assert decided.report.succeeded, decided.report.blockers

    execute = composed(paper_runtime, now=EXECUTION_NOW, venue=venue)
    try:
        executed = run_through(execute)
    finally:
        execute.close()

    posted = [row for row in venue.mutations if row["path"] == "/v2/orders"]

    assert executed.report.succeeded, executed.report.blockers
    assert len(posted) == 1
    quantity = Decimal(str(posted[0]["json"]["qty"]))
    assert quantity != quantity.to_integral_value()
    assert {row["json"]["symbol"] for row in posted} <= set(FRACTIONABLE.values())
    # The whole-share name is still in the full mapping and was never ordered.
    assert {row.identity_id for row in runtime.mapping.records} == set(ALL_SYMBOLS)


@pytest.mark.integration
def test_a_fractional_order_for_a_whole_share_instrument_is_refused(
    paper_runtime: Any,
) -> None:
    """§10/§25. The per-instrument check survives a proven deployment."""
    from stock_screener.execution import Order, OrderSide
    from stock_screener.execution.external import SymbolMapping, VenueSymbol, submit_order
    from stock_screener.portfolio_risk import SecurityIdentity

    author_v4(paper_runtime)
    widened = replace(
        whole_share_binding(),
        capabilities=BrokerCapabilities(fractional_quantities=True),
    )
    whole_share_only = SymbolMapping.of(
        [VenueSymbol(kind="COMPANY", identity_id=3, symbol="CCC", fractionable=False)]
    )
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

    assert widened.capabilities.fractional_quantities is True
    with pytest.raises(ExecutionError, match="whole-share-only"):
        submit_order(
            order=order,
            binding=widened,
            mapping=whole_share_only,
            transport=cast("Any", None),
            log=cast("Any", None),
            recorded_at=NOW,
        )


@pytest.mark.integration
def test_no_production_path_reshapes_an_order_quantity() -> None:
    """§10. A quantity is sent as authorised or the order is refused.

    Two separate assertions, because "no arithmetic anywhere" would be false and
    a test that asserts something false gets weakened until it passes. There is
    no `quantize`, `ceil`, `floor` or `trunc` on these paths at all — those are
    how a quantity gets silently reshaped. `round` is permitted only where its
    argument is plainly not a quantity: the one call is on a reported coverage
    ratio, and a new one touching a quantity fails here.
    """
    reshapers: list[str] = []
    rounded_quantities: list[str] = []
    for directory in (
        REPO_ROOT / "src" / "stock_screener" / "paper_runtime",
        REPO_ROOT / "src" / "stock_screener" / "execution" / "external",
    ):
        for path in sorted(directory.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                if name in {"quantize", "ceil", "floor", "trunc"}:
                    reshapers.append(f"{path.name}:{node.lineno} {name}()")
                if name == "round" and any(
                    "quantity" in ast.unparse(argument).lower() or "qty" in ast.unparse(argument)
                    for argument in node.args
                ):
                    rounded_quantities.append(f"{path.name}:{node.lineno} round()")

    assert reshapers == []
    assert rounded_quantities == []


# --------------------------------------------------------------------------- #
# §28 declaration / consumer guard
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_v4_contract_requires_six_operator_artefacts() -> None:
    assert REQUIRED_OPERATOR_INPUTS[RUNTIME_BINDING_V4_VERSION] == (
        "RISK_POLICY",
        "SYMBOL_MAPPING",
        "DEPLOYMENT_UNIVERSE",
        "DEPLOYMENT_CAPABILITY",
        "STRATEGY_AUTHORIZATION",
        "ACCOUNT_IDENTITY",
    )
    assert "ACCOUNT_IDENTITY" not in REQUIRED_OPERATOR_INPUTS["PAPER_OPERATIONS_RUNTIME_BINDING_V3"]


@pytest.mark.integration
def test_the_account_identity_artefact_has_writer_reader_validator_consumer() -> None:
    """§28. Four things, each checked."""
    from stock_screener.paper_runtime import composition, operator_inputs

    reader = operator_inputs.read_account_identity
    source = Path(str(composition.__file__)).read_text(encoding="utf-8")
    filename, pinned = OPERATOR_INPUT_FILES["ACCOUNT_IDENTITY"]

    assert callable(operator_inputs.write_account_identity)
    assert callable(reader)
    assert "expected_fingerprint" in inspect.signature(reader).parameters
    assert filename == ACCOUNT_IDENTITY_FILENAME
    assert pinned == "account_identity_fingerprint"
    assert "read_account_identity(" in source


@pytest.mark.integration
def test_every_v4_identity_field_has_a_production_consumer(paper_runtime: Any) -> None:
    """§28. A declared field nothing reads is a declaration with nothing behind it."""
    runtime = author_v4(paper_runtime)
    reloaded = active_binding(runtime.root)
    body = reloaded.body()
    environment_source = Path(
        str(__import__("stock_screener.paper_runtime.environment", fromlist=["x"]).__file__)
    ).read_text(encoding="utf-8")
    rebind_source = Path(
        str(__import__("stock_screener.paper_runtime.rebind", fromlist=["x"]).__file__)
    ).read_text(encoding="utf-8")

    for name in V4_IDENTITY_FIELDS:
        assert name in body, f"{name} is not serialised"
        assert getattr(reloaded, name), f"{name} is not read back"
    # The identity fingerprint is consumed by the proof; the bootstrap digest is
    # provenance, carried by the transition and nothing is proven against it.
    assert "account_identity_fingerprint" in environment_source
    assert "bootstrap_account_binding_fingerprint" in rebind_source
    assert "bootstrap_account_binding_fingerprint" not in environment_source


@pytest.mark.integration
def test_the_bootstrap_digest_is_never_recomputed(paper_runtime: Any) -> None:
    """§6. Provenance is carried, not re-derived under today's capabilities."""
    from stock_screener.paper_runtime import rebind as module

    tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_bootstrap_account_digest"
    )
    called = {
        getattr(node.func, "attr", "") or getattr(node.func, "id", "")
        for node in ast.walk(helper)
        if isinstance(node, ast.Call)
    }
    runtime = author_v4(paper_runtime)

    assert not called & {"binding_fingerprint", "account_fingerprint", "paper"}
    assert (
        runtime.binding.bootstrap_account_binding_fingerprint
        == runtime.predecessor.account_binding_fingerprint
    )


@pytest.mark.integration
def test_the_readiness_report_names_both_authorities(paper_runtime: Any) -> None:
    runtime = author_v4(paper_runtime)

    report = readiness(paper_runtime, binding=active_binding(runtime.root))

    assert report["ready"] is True
    assert report["deployment_capability_supported"] is True
    assert report["deployment_capability_scope"].startswith("DEPLOYMENT_SCOPED")
    assert report["resolved_strategy_version"] == STRATEGY_VERSION_V2


# --------------------------------------------------------------------------- #
# §9, §31, §35-§40 evidence, drift and what did not move
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_whole_share_names_outside_the_universe_do_not_break_capability() -> None:
    """§9. Truthful provider evidence, simply not deployable."""
    mapping = full_mapping()
    universe = fractionable_universe(mapping)

    proof = deployment_capability(mapping, universe)
    account = account_capability_from_mapping(mapping)

    assert {row.identity_id for row in mapping.records} == set(ALL_SYMBOLS)
    assert account["whole_share_only_instruments"] == len(WHOLE_SHARE)
    assert account["fractional_quantities"] is False
    assert proof.supported is True


@pytest.mark.integration
def test_the_cco_drift_is_not_special_cased_in_code() -> None:
    """§31. A provider-data change, recorded in docs and absent from source.

    Matched on the quoted symbol and the identity literal rather than the bare
    letters: `CCO` occurs inside ordinary words — "a**cco**unt", "a**cco**unting" —
    and a substring scan would report the whole package.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for marker in ('"CCO"', "'CCO'", "4496"):
            if marker in text:
                offenders.append(f"{path.name}: {marker}")

    assert offenders == []


@pytest.mark.integration
def test_no_mapping_universe_or_grant_was_refreshed() -> None:
    """§30. The frozen artefacts are untouched by this phase."""
    mapping = json.loads(
        (ARTEFACTS / "alpaca-paper-instrument-mapping.json").read_text(encoding="utf-8")
    )
    universe = json.loads(
        (ARTEFACTS / "alphatrader-paper-fractionable-universe.json").read_text(encoding="utf-8")
    )
    grant = json.loads(
        (ARTEFACTS / "compounder-monthly-v2-paper-authorization-v2.json").read_text(
            encoding="utf-8"
        )
    )

    assert (
        mapping["mapping_fingerprint"]
        == "4ae99e6120446bb1e58990c6b9c4b34cdf7956a224f48b8070513c8440ecbec3"
    )
    assert (
        universe["universe_fingerprint"]
        == "b5e4e946b3716c1d4c1e5cf79029eca81e6de9156de690d3585a5eb7a2b883ec"
    )
    assert (
        grant["authorization_fingerprint"]
        == "2e009948019e6541a26c373940742034300f1034dec6a84662e43ec7616bd8c9"
    )


@pytest.mark.integration
def test_the_preserved_contracts_are_unchanged() -> None:
    """§35/§36."""
    from stock_screener.paper_runtime.observability import observability_fingerprint
    from stock_screener.soak_verification.evidence_contract import evidence_contract
    from stock_screener.strategies import CompounderMonthlyStrategy

    assert (
        observability_fingerprint()
        == "0fa85983277222e83f8ae97ed3266fc0b830b958ddd21c1a54280fa88cdd7d5b"
    )
    assert (
        evidence_contract().fingerprint()
        == "eb612d02dd3896f816ac31fd60a0cd448e08ec5b444690a4896aa03239e8b585"
    )
    assert (
        paper_risk_policy_v2().fingerprint()
        == "9b367ed04a448ea71e19bf064d4ffb7bcc6d06fcd832fd99c246746e2cc3f04b"
    )
    assert paper_risk_policy_v2().engine_version == "PORTFOLIO_RISK_ENGINE_V1_3"
    assert (
        CompounderMonthlyStrategy().fingerprint()
        == "5aa71b40b7692eaa906649330eb4901bd4a87e6d394674c1245713400d5a3ce8"
    )
    assert (
        CompounderMonthlyFractionableStrategy(fractionable_universe(full_mapping())).fingerprint()
        == "a15887f81fa6dca1c5cd032ffbcbe70c32df0d3755c1d4b7911609ec4bd1d840"
    )


@pytest.mark.integration
def test_the_real_runtime_root_was_not_written_to() -> None:
    """§38."""
    real = REPO_ROOT / ".operations"
    body = json.loads((real / "runtime" / "binding.json").read_text(encoding="utf-8"))

    assert body["version"] == "PAPER_OPERATIONS_RUNTIME_BINDING_V1"
    assert not (real / "runtime" / "bindings").exists()
    for name in (
        "risk_policy.json",
        "symbol_mapping.json",
        "deployment_universe.json",
        DEPLOYMENT_CAPABILITY_FILENAME,
        "strategy_authorization.json",
        ACCOUNT_IDENTITY_FILENAME,
    ):
        assert not (real / "runtime" / name).exists(), name


@pytest.mark.integration
def test_no_soak_was_created_or_armed() -> None:
    """§39."""
    assert not (REPO_ROOT / ".operations" / "soak" / "v3").exists()
