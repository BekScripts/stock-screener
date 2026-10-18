"""P2.3.1I2 — a grant that names the universe it approves.

V1 of the authorization contract bound the strategy's fingerprint and the
environment. That is sufficient authority for a strategy whose behaviour is a
function of its own rules, and insufficient for V2 of the Compounder, whose
selectable population is an *input*: it ranks the intersection of research
eligibility and a frozen deployment universe. Swap the universe and the same
approved policy can choose a different portfolio.

So the owner's judgement, recorded in P2.3.1H2, was that the earlier grant was
too weak. These tests pin what replaced it: both the strategy and the universe
are bound, neither has a wildcard, and production asks the grant rather than
settling for the runtime binding's agreement with itself.

The earlier grant is not reinterpreted, promoted or edited. It stays exactly as
issued, as the record of what was approved when a universe was not yet part of
the granted authority.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from stock_screener.paper_runtime import (
    DEPLOYMENT_UNIVERSE_VERSION,
    RUNTIME_BINDING_V3_VERSION,
    Code,
    RuntimeBindingError,
    read_strategy_authorization,
    write_strategy_authorization,
)
from stock_screener.paper_runtime.operator_inputs import (
    OPERATOR_INPUT_FILES,
    REQUIRED_OPERATOR_INPUTS,
    STRATEGY_AUTHORIZATION_FILENAME,
)
from stock_screener.strategies import (
    STRATEGY_AUTHORIZATION_CONTRACT,
    STRATEGY_AUTHORIZATION_CONTRACT_V2,
    STRATEGY_VERSION_V2,
    CompounderMonthlyStrategy,
    StrategyDeploymentAuthorization,
    StrategyDeploymentAuthorizationV2,
    StrategyError,
    require_authorization,
)
from stock_screener.strategies import Code as StrategyCode

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTEFACTS = REPO_ROOT / "docs" / "reference" / "deployment"
NOW = "2026-10-18T00:00:00+00:00"

#: The reviewed targets, as §3 names them.
STRATEGY_FP = "a15887f81fa6dca1c5cd032ffbcbe70c32df0d3755c1d4b7911609ec4bd1d840"
UNIVERSE_FP = "b5e4e946b3716c1d4c1e5cf79029eca81e6de9156de690d3585a5eb7a2b883ec"

#: The historical grant, preserved rather than migrated.
HISTORICAL_FP = "ff008ecad69b7c03a9781cd49eb926a19d38c1166576b09ff9c2c6d10f3d98b6"

OTHER_UNIVERSE_FP = "c" * 64
OTHER_STRATEGY_FP = "d" * 64


def grant(**overrides: Any) -> StrategyDeploymentAuthorizationV2:
    """A grant for the reviewed targets, with one field at a time overridden."""
    settings: dict[str, Any] = {
        "authorization_id": "TEST_GRANT_V2",
        "strategy_id": "ALPHATRADER_COMPOUNDER_MONTHLY",
        "strategy_version": STRATEGY_VERSION_V2,
        "strategy_fingerprint": STRATEGY_FP,
        "deployment_universe_version": DEPLOYMENT_UNIVERSE_VERSION,
        "deployment_universe_fingerprint": UNIVERSE_FP,
        "code_commit": "00f9fce",
        "created_at": NOW,
    }
    settings.update(overrides)
    return StrategyDeploymentAuthorizationV2(**settings)


def ask(authorization: Any, **overrides: Any) -> None:
    """Put the reviewed deployment question to a grant."""
    settings: dict[str, Any] = {
        "strategy_id": "ALPHATRADER_COMPOUNDER_MONTHLY",
        "strategy_version": STRATEGY_VERSION_V2,
        "fingerprint": STRATEGY_FP,
        "environment": "EXTERNAL_PAPER",
        "universe_fingerprint": UNIVERSE_FP,
    }
    settings.update(overrides)
    require_authorization(authorization, **settings)


# --------------------------------------------------------------------------- #
# §2-§3 the contract and the exact target
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_new_contract_is_additive_beside_the_old_one() -> None:
    assert STRATEGY_AUTHORIZATION_CONTRACT == "STRATEGY_DEPLOYMENT_AUTHORIZATION_V1"
    assert STRATEGY_AUTHORIZATION_CONTRACT_V2 == "STRATEGY_DEPLOYMENT_AUTHORIZATION_V2"


@pytest.mark.integration
def test_the_new_contract_binds_every_field_the_authority_needs() -> None:
    """§2. Each named field is in the fingerprinted body, not merely on the class."""
    body = grant().body()

    for field in (
        "contract",
        "strategy_id",
        "strategy_version",
        "strategy_fingerprint",
        "deployment_universe_version",
        "deployment_universe_fingerprint",
        "environment",
        "authorization_state",
    ):
        assert field in body, field
    assert grant().as_json()["authorization_fingerprint"] == grant().fingerprint()


@pytest.mark.integration
def test_the_persisted_grant_authorises_the_exact_reviewed_targets() -> None:
    """§3. The committed artefact names the two reviewed fingerprints."""
    stored = json.loads(
        (ARTEFACTS / "compounder-monthly-v2-paper-authorization-v2.json").read_text(
            encoding="utf-8"
        )
    )

    assert stored["contract"] == STRATEGY_AUTHORIZATION_CONTRACT_V2
    assert stored["strategy_version"] == STRATEGY_VERSION_V2
    assert stored["strategy_fingerprint"] == STRATEGY_FP
    assert stored["deployment_universe_version"] == DEPLOYMENT_UNIVERSE_VERSION
    assert stored["deployment_universe_fingerprint"] == UNIVERSE_FP
    assert stored["environment"] == "EXTERNAL_PAPER"
    assert stored["authorization_state"] == "IN_FORCE"
    assert stored["live_trading"] == "NOT_IMPLEMENTED_NOT_AUTHORIZED"


@pytest.mark.integration
def test_the_persisted_grant_names_what_it_supersedes_without_promoting_it() -> None:
    """§13. Provenance, not migration."""
    stored = json.loads(
        (ARTEFACTS / "compounder-monthly-v2-paper-authorization-v2.json").read_text(
            encoding="utf-8"
        )
    )

    assert stored["supersedes"] == HISTORICAL_FP
    assert stored["authorization_fingerprint"] != HISTORICAL_FP


@pytest.mark.integration
def test_the_persisted_grant_matches_the_universe_artefact_it_names() -> None:
    universe = json.loads(
        (ARTEFACTS / "alphatrader-paper-fractionable-universe.json").read_text(encoding="utf-8")
    )
    stored = json.loads(
        (ARTEFACTS / "compounder-monthly-v2-paper-authorization-v2.json").read_text(
            encoding="utf-8"
        )
    )

    assert stored["deployment_universe_fingerprint"] == universe["universe_fingerprint"]
    assert stored["deployment_universe_version"] == universe["version"]


@pytest.mark.integration
def test_the_persisted_grant_carries_no_credential() -> None:
    from stock_screener.execution.external import check_no_credentials

    check_no_credentials(
        json.loads(
            (ARTEFACTS / "compounder-monthly-v2-paper-authorization-v2.json").read_text(
                encoding="utf-8"
            )
        )
    )


# --------------------------------------------------------------------------- #
# §9-§12 the four decisive cases
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_exact_strategy_universe_and_venue_are_accepted() -> None:
    """§9."""
    ask(grant())


@pytest.mark.integration
def test_a_different_universe_is_refused() -> None:
    """§10. The case the whole contract exists for."""
    with pytest.raises(StrategyError, match="approved deployment universe") as caught:
        ask(grant(), universe_fingerprint=OTHER_UNIVERSE_FP)

    assert caught.value.code is StrategyCode.STRATEGY_NOT_AUTHORIZED
    assert "different one can produce a different portfolio" in str(caught.value)


@pytest.mark.integration
def test_a_different_strategy_fingerprint_is_refused() -> None:
    """§11."""
    with pytest.raises(StrategyError, match="A modified strategy is not") as caught:
        ask(grant(), fingerprint=OTHER_STRATEGY_FP)

    assert caught.value.code is StrategyCode.STRATEGY_AUTHORIZATION_VERSION_MISMATCH


@pytest.mark.integration
@pytest.mark.parametrize(
    "environment", ["LIVE", "EXTERNAL_LIVE", "PRODUCTION_LIVE", "PAPER_THEN_LIVE"]
)
def test_a_live_environment_is_refused(environment: str) -> None:
    """§12. Named rather than merely absent, in both directions."""
    with pytest.raises(StrategyError, match="NOT_IMPLEMENTED") as caught:
        ask(grant(), environment=environment)

    assert caught.value.code is StrategyCode.STRATEGY_LIVE_NOT_AUTHORIZED


@pytest.mark.integration
def test_a_grant_cannot_be_issued_for_a_live_environment() -> None:
    with pytest.raises(StrategyError, match="not an authorisable environment"):
        grant(environment="EXTERNAL_LIVE")


@pytest.mark.integration
def test_a_different_strategy_version_is_refused() -> None:
    with pytest.raises(StrategyError, match="this grant covers"):
        ask(grant(), strategy_version="ALPHATRADER_COMPOUNDER_MONTHLY_V1")


@pytest.mark.integration
def test_a_different_environment_is_refused() -> None:
    with pytest.raises(StrategyError, match="this grant covers"):
        ask(grant(), environment="SIMULATED")


@pytest.mark.integration
def test_a_withdrawn_grant_authorises_nothing() -> None:
    """A grant kept as evidence is not a permission."""
    with pytest.raises(StrategyError, match="authorises nothing"):
        ask(grant(authorization_state="WITHDRAWN"))


# --------------------------------------------------------------------------- #
# §4-§5 no wildcard
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_grant_cannot_be_issued_without_a_universe() -> None:
    """§4. An empty universe would authorise every universe."""
    with pytest.raises(StrategyError, match="An empty universe is a wildcard") as caught:
        grant(deployment_universe_fingerprint="")

    assert caught.value.code is StrategyCode.STRATEGY_NOT_AUTHORIZED


@pytest.mark.integration
def test_a_grant_cannot_be_issued_without_a_universe_contract() -> None:
    with pytest.raises(StrategyError, match="deployment-universe contract"):
        grant(deployment_universe_version="")


@pytest.mark.integration
def test_a_grant_cannot_be_issued_without_a_strategy_fingerprint() -> None:
    with pytest.raises(StrategyError, match="exact strategy fingerprint"):
        grant(strategy_fingerprint="  ")


@pytest.mark.integration
def test_checking_a_universe_binding_grant_without_a_universe_is_refused() -> None:
    """§4. The check is refused, never narrowed to the weaker question.

    A caller that omitted the universe would silently get V1's authority out of
    a V2 grant, and nothing would record that the stronger check never ran.
    """
    with pytest.raises(StrategyError, match="none was supplied to check against") as caught:
        require_authorization(
            grant(),
            strategy_id="ALPHATRADER_COMPOUNDER_MONTHLY",
            strategy_version=STRATEGY_VERSION_V2,
            fingerprint=STRATEGY_FP,
            environment="EXTERNAL_PAPER",
        )

    assert caught.value.code is StrategyCode.STRATEGY_NOT_AUTHORIZED


@pytest.mark.integration
def test_the_universe_is_never_resolved_at_check_time() -> None:
    """§4. No "latest", no provider lookup, no default inside the contract.

    Parsed, because the module discusses the wildcards it refuses in prose.
    """
    from stock_screener.strategies import authorization as module

    tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
    called = {
        getattr(node.func, "attr", "") or getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }

    assert not called & {
        "fractionable_universe",
        "mapping_from_evidence",
        "read_deployment_universe",
        "from_settings",
        "request",
    }


@pytest.mark.integration
def test_the_universe_parameter_has_no_default_on_the_contract() -> None:
    """Read off the signature: a required argument cannot be forgotten quietly."""
    signature = inspect.signature(StrategyDeploymentAuthorizationV2.require)

    assert signature.parameters["universe_fingerprint"].default is inspect.Parameter.empty


@pytest.mark.integration
def test_a_changed_universe_fingerprint_invalidates_the_grant() -> None:
    """§5. Intentional: a new universe needs a new reviewed authorization."""
    issued = grant()
    reissued = grant(deployment_universe_fingerprint=OTHER_UNIVERSE_FP)

    ask(issued)
    with pytest.raises(StrategyError):
        ask(issued, universe_fingerprint=OTHER_UNIVERSE_FP)
    ask(reissued, universe_fingerprint=OTHER_UNIVERSE_FP)
    assert reissued.fingerprint() != issued.fingerprint()


# --------------------------------------------------------------------------- #
# §1, §13 the historical grant
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_historical_grant_is_byte_identical() -> None:
    """§13. Still readable, still digesting to what it always did."""
    stored = json.loads(
        (ARTEFACTS / "compounder-monthly-v2-paper-authorization.json").read_text(encoding="utf-8")
    )

    assert stored["contract"] == STRATEGY_AUTHORIZATION_CONTRACT
    assert stored["authorization_fingerprint"] == HISTORICAL_FP
    assert "deployment_universe_fingerprint" not in stored


@pytest.mark.integration
def test_the_v1_contract_schema_is_not_reinterpreted() -> None:
    """§1. V1 still binds four things and still fingerprints as it did."""
    historical = StrategyDeploymentAuthorization(
        authorization_id="X",
        strategy_id="ALPHATRADER_COMPOUNDER_MONTHLY",
        strategy_version=STRATEGY_VERSION_V2,
        strategy_fingerprint=STRATEGY_FP,
        code_commit="c",
        created_at=NOW,
    )

    assert "deployment_universe_fingerprint" not in historical.body()
    assert set(inspect.signature(historical.require).parameters) == {
        "strategy_id",
        "strategy_version",
        "fingerprint",
        "environment",
    }


@pytest.mark.integration
def test_a_v1_grant_is_not_promoted_into_the_new_contract() -> None:
    """A V1 grant answers V1's question and is never asked V2's.

    It must not be silently accepted *as though* it had bound a universe: the
    checker routes it to its own four-field `require`, so a reader can tell from
    the code which authority actually ran.
    """
    historical = StrategyDeploymentAuthorization(
        authorization_id="X",
        strategy_id="ALPHATRADER_COMPOUNDER_MONTHLY",
        strategy_version=STRATEGY_VERSION_V2,
        strategy_fingerprint=STRATEGY_FP,
        code_commit="c",
        created_at=NOW,
    )

    # Accepted, because V1 never claimed to bind a universe — and the universe
    # argument is not quietly treated as satisfied by it. The two contracts are
    # unrelated types, so there is no inheritance by which a V1 grant could
    # answer V2's question; what it answers is recorded on its own body.
    ask(historical)

    assert historical.body()["contract"] == STRATEGY_AUTHORIZATION_CONTRACT
    assert "deployment_universe_fingerprint" not in historical.body()


@pytest.mark.integration
def test_a_body_of_the_old_contract_is_refused_by_the_new_reader() -> None:
    stored = json.loads(
        (ARTEFACTS / "compounder-monthly-v2-paper-authorization.json").read_text(encoding="utf-8")
    )

    with pytest.raises(StrategyError, match="declares contract"):
        StrategyDeploymentAuthorizationV2.from_body(stored)


# --------------------------------------------------------------------------- #
# §14 round trip
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_grant_round_trips_through_its_own_serialisation() -> None:
    """§14. Every field survives, and the fingerprint is identical."""
    issued = grant()

    reloaded = StrategyDeploymentAuthorizationV2.from_body(issued.as_json())

    assert reloaded == issued
    assert reloaded.body() == issued.body()
    assert reloaded.fingerprint() == issued.fingerprint()


@pytest.mark.integration
def test_the_grant_round_trips_through_the_operator_artefact(paper_runtime: Any) -> None:
    issued = grant()
    path = write_strategy_authorization(paper_runtime.root, issued)

    reloaded = read_strategy_authorization(
        paper_runtime.root, expected_fingerprint=issued.fingerprint()
    )

    assert path.name == STRATEGY_AUTHORIZATION_FILENAME
    assert reloaded.fingerprint() == issued.fingerprint()
    assert reloaded.deployment_universe_fingerprint == UNIVERSE_FP


@pytest.mark.integration
def test_every_grant_field_is_read_back_by_name(paper_runtime: Any) -> None:
    """No field may exist only on the writer.

    Each declared field is perturbed one at a time; if the reader ignored it the
    reloaded grant would still equal the original and this would fail.
    """
    issued = grant()
    perturbations: dict[str, Any] = {
        "authorization_id": "OTHER",
        "strategy_id": "OTHER_STRATEGY",
        "strategy_version": "OTHER_VERSION",
        "strategy_fingerprint": OTHER_STRATEGY_FP,
        "deployment_universe_version": "OTHER_UNIVERSE_CONTRACT",
        "deployment_universe_fingerprint": OTHER_UNIVERSE_FP,
        "code_commit": "OTHER_COMMIT",
        "created_at": "2030-01-01T00:00:00+00:00",
        "authorization_state": "WITHDRAWN",
        "approved_by": "OTHER_OWNER",
        "notes": "OTHER_NOTES",
        "supersedes": "e" * 64,
    }

    for field, value in perturbations.items():
        changed = grant(**{field: value})
        reloaded = StrategyDeploymentAuthorizationV2.from_body(changed.as_json())

        assert getattr(reloaded, field) == value, field
        assert reloaded.fingerprint() != issued.fingerprint(), field


@pytest.mark.integration
def test_a_grant_body_missing_a_required_field_is_refused() -> None:
    body = grant().as_json()
    del body["deployment_universe_fingerprint"]

    with pytest.raises(StrategyError, match="omits"):
        StrategyDeploymentAuthorizationV2.from_body(body)


@pytest.mark.integration
def test_a_tampered_grant_artefact_fails_closed(paper_runtime: Any) -> None:
    """A grant edited after it was bound authorises whatever the editor chose."""
    issued = grant()
    path = write_strategy_authorization(paper_runtime.root, issued)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["deployment_universe_fingerprint"] = OTHER_UNIVERSE_FP
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(RuntimeBindingError, match="digests to") as caught:
        read_strategy_authorization(paper_runtime.root, expected_fingerprint=issued.fingerprint())

    assert caught.value.code is Code.RUNTIME_OPERATOR_INPUT_FINGERPRINT_MISMATCH


@pytest.mark.integration
def test_a_missing_grant_artefact_fails_closed(paper_runtime: Any) -> None:
    with pytest.raises(RuntimeBindingError, match="has not been authored") as caught:
        read_strategy_authorization(paper_runtime.root)

    assert caught.value.code is Code.RUNTIME_STRATEGY_AUTHORIZATION_NOT_AUTHORED


# --------------------------------------------------------------------------- #
# §6-§8, §15 production consumes it
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_grant_is_a_required_operator_artefact_for_v3() -> None:
    """§6. Production cannot check a grant it does not load."""
    assert "STRATEGY_AUTHORIZATION" in REQUIRED_OPERATOR_INPUTS[RUNTIME_BINDING_V3_VERSION]
    filename, pinned = OPERATOR_INPUT_FILES["STRATEGY_AUTHORIZATION"]

    assert filename == STRATEGY_AUTHORIZATION_FILENAME
    assert pinned == "strategy_authorization_fingerprint"


@pytest.mark.integration
def test_the_grant_is_not_required_of_a_historical_v1_runtime() -> None:
    """A V1 root goes on needing exactly what it always needed."""
    assert (
        "STRATEGY_AUTHORIZATION"
        not in REQUIRED_OPERATOR_INPUTS["PAPER_OPERATIONS_RUNTIME_BINDING_V1"]
    )


@pytest.mark.integration
def test_production_composition_checks_the_grant_not_only_the_binding() -> None:
    """§6/§15. The composition root loads the grant and asks it the question.

    Parsed rather than argued: the composition must both read the artefact and
    put the universe to it. Checking only the binding would make the grant
    decorative, since the binding is what the transition wrote in the first
    place.
    """
    from stock_screener.paper_runtime import composition as module

    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {
        getattr(node.func, "attr", "") or getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    checks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", "") == "require_authorization")
        and any(kw.arg == "universe_fingerprint" for kw in node.keywords)
    ]

    assert "read_strategy_authorization" in calls
    assert len(checks) == 1


@pytest.mark.integration
def test_the_revision_checks_the_grant_against_the_universe_it_binds() -> None:
    """§7. The transition asks the same question before recording the grant."""
    from stock_screener.paper_runtime import rebind as module

    tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
    checks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "require_authorization"
        and any(kw.arg == "universe_fingerprint" for kw in node.keywords)
    ]

    assert len(checks) == 1


@pytest.mark.integration
def test_the_deployment_universe_fingerprint_has_a_writer_reader_validator_consumer() -> None:
    """§15. Four things, each checked, none taken on trust."""
    from stock_screener.paper_runtime import operator_inputs

    reader = operator_inputs.read_strategy_authorization
    composition = Path(
        str(__import__("stock_screener.paper_runtime.composition", fromlist=["x"]).__file__)
    ).read_text(encoding="utf-8")

    # writer
    assert callable(operator_inputs.write_strategy_authorization)
    # reader
    assert callable(reader)
    # validator — the reader is pinned to the binding's fingerprint, and the
    # contract refuses a grant whose universe does not match
    assert "expected_fingerprint" in inspect.signature(reader).parameters
    assert (
        "universe_fingerprint"
        in inspect.signature(StrategyDeploymentAuthorizationV2.require).parameters
    )
    # production consumer
    assert "require_authorization(" in composition


@pytest.mark.integration
def test_three_authorities_must_agree_on_the_universe() -> None:
    """§8. Grant, runtime binding and decision each carry the fingerprint.

    Defence in depth is only real if all three independently record it, so this
    reads the field off each contract rather than trusting that they agree.
    """
    from stock_screener.paper_runtime import V3_DEPLOYMENT_FIELDS
    from stock_screener.strategies.models import StrategyDecision

    assert "deployment_universe_fingerprint" in grant().body()
    assert "deployment_universe_fingerprint" in V3_DEPLOYMENT_FIELDS
    assert "deployment_universe_fingerprint" in {
        field.name for field in StrategyDecision.__dataclass_fields__.values()
    }


# --------------------------------------------------------------------------- #
# §16-§18 nothing else moved
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_real_runtime_root_was_not_written_to() -> None:
    """§16."""
    real = REPO_ROOT / ".operations"

    assert (real / "runtime" / "binding.json").exists()
    assert (
        json.loads((real / "runtime" / "binding.json").read_text(encoding="utf-8"))["version"]
        == "PAPER_OPERATIONS_RUNTIME_BINDING_V1"
    )
    assert not (real / "runtime" / "bindings").exists()
    for name in (
        "risk_policy.json",
        "symbol_mapping.json",
        "deployment_universe.json",
        "deployment_capability.json",
        STRATEGY_AUTHORIZATION_FILENAME,
    ):
        assert not (real / "runtime" / name).exists(), name


@pytest.mark.integration
def test_no_v3_soak_exists() -> None:
    assert not (REPO_ROOT / ".operations" / "soak" / "v3").exists()


@pytest.mark.integration
def test_the_preserved_contracts_are_unchanged() -> None:
    """§18."""
    from stock_screener.paper_runtime import paper_risk_policy_v2
    from stock_screener.paper_runtime.observability import observability_fingerprint
    from stock_screener.soak_verification.evidence_contract import evidence_contract

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
    assert (
        CompounderMonthlyStrategy().fingerprint()
        == "5aa71b40b7692eaa906649330eb4901bd4a87e6d394674c1245713400d5a3ce8"
    )


@pytest.mark.integration
def test_the_strategy_and_universe_artefacts_are_unchanged() -> None:
    universe = json.loads(
        (ARTEFACTS / "alphatrader-paper-fractionable-universe.json").read_text(encoding="utf-8")
    )
    mapping = json.loads(
        (ARTEFACTS / "alpaca-paper-instrument-mapping.json").read_text(encoding="utf-8")
    )

    assert universe["universe_fingerprint"] == UNIVERSE_FP
    assert (
        mapping["mapping_fingerprint"]
        == "4ae99e6120446bb1e58990c6b9c4b34cdf7956a224f48b8070513c8440ecbec3"
    )
