"""P2.3.1E — the deployment authority a paper runtime needs, and who may set it.

Wiring `run-due` to the submission authority in P2.3.1D exposed two missing
inputs. They are not the same kind of thing, and these tests are organised
around that difference.

The **instrument mapping is evidence**: every field comes from the authoritative
internal identity record or from the venue's own asset metadata, so it can be
built, verified and fingerprinted without anyone's approval. The tests for it are
about refusing to guess — ambiguous identities, unknown fractionability, a
symbol the venue never confirmed.

The **risk policy is judgment**: concentration, cash floor and drawdown ladder
state how much of a loss is acceptable. No reading of the repository produces
them. The tests for it are about refusing to supply them — that the candidate
names its unresolved fields, that it cannot be handed to the engine, and that
nothing silently falls back to the example policy whose own docstring says it is
not authorised.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.paper_runtime import (
    INSTRUMENT_MAPPING_VERSION,
    OWNER_APPROVAL_REQUIRED,
    REBIND_PLAN_VERSION,
    RISK_POLICY_CANDIDATE_VERSION,
    Code,
    Routability,
    RuntimeBindingError,
    mapping_from_evidence,
    policy_field_inventory,
    rebind_plan,
    risk_policy_candidate,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def asset(
    symbol: str,
    *,
    tradable: bool = True,
    fractionable: bool | None = True,
    status: str = "active",
    asset_id: str = "",
    answered: str = "",
) -> dict[str, Any]:
    """One provider asset payload, as Alpaca's `/v2/assets` returns it."""
    body: dict[str, Any] = {
        "id": asset_id or f"asset-{symbol.lower()}",
        "symbol": answered or symbol,
        "class": "us_equity",
        "exchange": "NASDAQ",
        "status": status,
        "tradable": tradable,
    }
    if fractionable is not None:
        body["fractionable"] = fractionable
    return body


def mapping_of(
    identities: dict[int, str], assets: dict[str, dict[str, Any]], **overrides: Any
) -> Any:
    """Build a mapping from the two evidence sides."""
    settings: dict[str, Any] = {
        "identities": identities,
        "provider_assets": assets,
        "universe": "TEST_UNIVERSE",
        "universe_as_of": "2026-08-26",
        "evidence_at": "2026-10-12T00:00:00+00:00",
        "provider_evidence_fingerprint": "e" * 64,
    }
    settings.update(overrides)
    return mapping_from_evidence(**settings)


# --------------------------------------------------------------------------
# §3, §4, §8: the risk policy inventory and its approval surface
# --------------------------------------------------------------------------


def test_every_risk_policy_field_is_inventoried() -> None:
    # §3. Every field the engine accepts is accounted for, so a field cannot be
    # left out of the approval surface by being forgotten.
    import dataclasses

    from stock_screener.portfolio_risk import RiskPolicy

    engine_fields = {row.name for row in dataclasses.fields(RiskPolicy)}
    inventoried = {row.name for row in policy_field_inventory()}

    assert inventoried == engine_fields
    assert len(inventoried) == 23


def test_every_inventoried_field_states_its_authority() -> None:
    for row in policy_field_inventory():
        assert row.unit
        assert row.meaning
        assert row.engine_default
        assert row.constraint
        assert row.authority
        assert row.rationale, row.name


def test_no_numeric_limit_is_invented() -> None:
    # §4. No numeric limit without an authoritative source carries a value. A
    # *structural* field may carry a proposed value with its rationale — §8
    # asks for conservatively justifiable candidates — but a concentration cap,
    # a cash floor or a drawdown ladder may not, because a plausible number
    # beside "nobody has decided" is the number that gets used.
    numeric = {
        "max_position_weight",
        "max_positions",
        "min_cash_weight",
        "max_trade_notional_weight",
        "min_position_weight",
        "max_portfolio_volatility",
        "max_position_adv_fraction",
        "max_trade_adv_fraction",
        "drawdown_bands",
        "max_risk_metric_age_seconds",
        "correlation_threshold",
        "max_correlated_pair_exposure",
    }
    for row in policy_field_inventory():
        if row.name in numeric:
            assert row.needs_owner, row.name
            assert row.candidate == OWNER_APPROVAL_REQUIRED, row.name


def test_a_proposed_value_for_an_owner_field_is_never_used_unsupplied() -> None:
    # The invariant that makes a proposed value safe. `construction_mode` is
    # structural and carries a proposal, but it is still in the approval
    # surface, and building a policy without the owner supplying it is refused
    # rather than filled from the proposal.
    candidate = risk_policy_candidate()
    proposed = {row.name: row for row in candidate.fields if row.needs_owner}

    assert "construction_mode" in proposed
    assert proposed["construction_mode"].candidate != OWNER_APPROVAL_REQUIRED
    assert proposed["construction_mode"].rationale
    assert "construction_mode" in candidate.owner_approval_required
    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_RISK_POLICY_NOT_AUTHORED):
        candidate.authorised_policy(
            **{
                name: None
                for name in candidate.owner_approval_required
                if name != "construction_mode"
            }
        )


def test_the_approval_surface_is_finite_and_named() -> None:
    # §8. Not "approve the policy" — these exact fields.
    candidate = risk_policy_candidate()

    assert candidate.owner_approval_required == (
        "construction_mode",
        "max_position_weight",
        "max_positions",
        "min_cash_weight",
        "max_trade_notional_weight",
        "min_position_weight",
        "max_portfolio_volatility",
        "max_position_adv_fraction",
        "max_trade_adv_fraction",
        "drawdown_bands",
        "max_risk_metric_age_seconds",
        "correlation_threshold",
        "max_correlated_pair_exposure",
    )
    assert len(candidate.derivable) == 10


def test_the_derivable_fields_name_a_real_source() -> None:
    # A field is only derivable if something in the repository constrains it.
    # "derived" with no stated source would be an invented value with a label.
    for row in policy_field_inventory():
        if not row.needs_owner:
            assert row.authority != OWNER_APPROVAL_REQUIRED
            assert len(row.authority) > 12, row.name


def test_the_candidate_is_paper_only_and_unauthorised() -> None:
    # §6, §11.
    candidate = risk_policy_candidate()
    body = candidate.as_json()

    assert candidate.environment == "EXTERNAL_PAPER"
    assert candidate.authorized is False
    assert body["authorized"] is False
    assert body["version"] == RISK_POLICY_CANDIDATE_VERSION
    assert any("NOT_AUTHORIZED" in row for row in body["limitations"])
    assert any("EXTERNAL_PAPER only" in row for row in body["limitations"])


def test_the_candidate_does_not_promote_the_example_policy() -> None:
    # §2. The example's limits must not become authority by being reused.
    from stock_screener.portfolio_risk.policy import EXAMPLE_POLICY_V1

    candidate = risk_policy_candidate()
    by_name = {row.name: row for row in candidate.fields}

    assert EXAMPLE_POLICY_V1.production_authorized is False
    for field in ("max_position_weight", "max_positions", "min_cash_weight"):
        assert by_name[field].candidate == OWNER_APPROVAL_REQUIRED
        assert by_name[field].candidate != getattr(EXAMPLE_POLICY_V1, field)


def test_the_candidate_cannot_be_handed_to_the_engine() -> None:
    # §11, §46. Partial approval is refused rather than completed from the
    # engine's defaults, which come from the unauthorised example.
    candidate = risk_policy_candidate()

    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_RISK_POLICY_NOT_AUTHORED):
        candidate.authorised_policy()
    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_RISK_POLICY_NOT_AUTHORED):
        candidate.authorised_policy(max_positions=20)


def test_a_fully_approved_candidate_builds_a_policy_still_not_live_authorised() -> None:
    # Once an owner supplies every unresolved field the engine can be used —
    # and `production_authorized` stays False, because this function is not the
    # place that declaration is made.
    candidate = risk_policy_candidate()
    approved = {
        "construction_mode": "PRESERVE_ABSOLUTE_WEIGHTS",
        "max_position_weight": 0.2,
        "max_positions": 25,
        "min_cash_weight": 0.0,
        "max_trade_notional_weight": None,
        "min_position_weight": None,
        "max_portfolio_volatility": None,
        "max_position_adv_fraction": None,
        "max_trade_adv_fraction": None,
        "drawdown_bands": (),
        "max_risk_metric_age_seconds": None,
        "correlation_threshold": None,
        "max_correlated_pair_exposure": None,
    }

    policy = candidate.authorised_policy(**approved)

    assert policy.policy_id == "ALPHATRADER_PAPER"
    assert policy.production_authorized is False
    assert policy.long_only is True
    assert policy.max_gross_exposure == 1.0


# --------------------------------------------------------------------------
# §9, §45: the policy fingerprint
# --------------------------------------------------------------------------


def test_the_policy_fingerprint_ignores_field_order() -> None:
    # §45. Same semantics in a different order is the same policy.
    import dataclasses

    candidate = risk_policy_candidate()
    shuffled = dataclasses.replace(candidate, fields=tuple(reversed(candidate.fields)))

    assert shuffled.fingerprint() == candidate.fingerprint()


def test_one_changed_limit_changes_the_policy_fingerprint() -> None:
    # §45. And the change must be visible even when the field was unresolved:
    # resolving a field is a material change.
    import dataclasses

    candidate = risk_policy_candidate()
    fields = list(candidate.fields)
    index = next(i for i, row in enumerate(fields) if row.name == "max_position_weight")
    fields[index] = dataclasses.replace(fields[index], candidate=0.2, authority="owner")
    resolved = dataclasses.replace(candidate, fields=tuple(fields))

    assert resolved.fingerprint() != candidate.fingerprint()


def test_the_policy_fingerprint_binds_the_engine_and_the_authorisation_state() -> None:
    # §9.
    import dataclasses

    candidate = risk_policy_candidate()
    body = candidate.body()

    assert body["engine_version"] == "PORTFOLIO_RISK_ENGINE_V1_2"
    assert body["environment"] == "EXTERNAL_PAPER"
    assert body["authorized"] is False
    promoted = dataclasses.replace(candidate, authorized=True)
    assert promoted.fingerprint() != candidate.fingerprint()


# --------------------------------------------------------------------------
# §13, §47: identity is durable, never the ticker
# --------------------------------------------------------------------------


def test_two_identities_sharing_a_symbol_are_both_refused() -> None:
    # §47. The case a ticker-keyed mapping gets silently wrong: both would
    # route to one instrument and their positions would merge into one.
    mapping = mapping_of(
        {11: "AAA", 22: "AAA", 33: "BBB"},
        {"AAA": asset("AAA"), "BBB": asset("BBB")},
    )
    by_id = {row.identity_id: row for row in mapping.records}

    assert by_id[11].routability == Routability.AMBIGUOUS
    assert by_id[22].routability == Routability.AMBIGUOUS
    assert by_id[11].routable is False
    assert by_id[22].routable is False
    assert by_id[33].routable is True
    assert "claimed by durable identities [11, 22]" in by_id[11].detail


def test_an_ambiguous_identity_has_no_venue_symbol_at_all() -> None:
    # §20. No fallback to the internal symbol.
    mapping = mapping_of({11: "AAA", 22: "AAA"}, {"AAA": asset("AAA")})

    assert mapping.venue_mapping().entries == ()
    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_SYMBOL_MAPPING_NOT_AUTHORED):
        mapping.records[0].as_venue_symbol()


def test_the_record_keeps_identity_and_symbol_apart() -> None:
    # §13. The label is the durable identity; the symbol is routing metadata.
    mapping = mapping_of({42: "AAA"}, {"AAA": asset("AAA", asset_id="prov-42")})
    row = mapping.records[0]

    assert row.label == "COMPANY:42"
    assert row.identity_id == 42
    assert row.internal_symbol == "AAA"
    assert row.provider_symbol == "AAA"
    assert row.provider_asset_id == "prov-42"


# --------------------------------------------------------------------------
# §16, §17, §49, §51: what the venue must have said
# --------------------------------------------------------------------------


def test_fractionability_comes_from_the_provider() -> None:
    # §16, §49.
    mapping = mapping_of(
        {1: "FRAC", 2: "WHOLE"},
        {"FRAC": asset("FRAC", fractionable=True), "WHOLE": asset("WHOLE", fractionable=False)},
    )
    by_id = {row.identity_id: row for row in mapping.records}

    assert by_id[1].fractionable is True
    assert by_id[2].fractionable is False


def test_unknown_fractionability_is_not_silently_true() -> None:
    # §49. An unknown capability is never read as permitted, and it is reported
    # as its own outcome rather than as "not tradable" — the asset *is*
    # tradable and what is missing is the capability.
    mapping = mapping_of({1: "QUIET"}, {"QUIET": asset("QUIET", fractionable=None)})
    row = mapping.records[0]

    assert row.fractionable is None
    assert row.routability == Routability.FRACTIONABILITY_UNKNOWN
    assert row.routable is False
    assert "not read as permitted" in row.detail


def test_an_inactive_or_untradable_asset_is_not_routable() -> None:
    # §51.
    mapping = mapping_of(
        {1: "DEAD", 2: "HALT"},
        {
            "DEAD": asset("DEAD", status="inactive"),
            "HALT": asset("HALT", tradable=False),
        },
    )
    by_id = {row.identity_id: row for row in mapping.records}

    assert by_id[1].routability == Routability.INACTIVE
    assert by_id[2].routability == Routability.NOT_TRADABLE
    assert mapping.routable() == ()


def test_a_symbol_the_venue_answered_differently_is_refused() -> None:
    mapping = mapping_of({1: "AAA"}, {"AAA": asset("AAA", answered="BBB")})

    assert mapping.records[0].routability == Routability.SYMBOL_MISMATCH
    assert "answered for BBB" in mapping.records[0].detail


def test_an_identity_the_venue_never_answered_for_is_unmapped() -> None:
    # §50.
    mapping = mapping_of({1: "GHOST"}, {})
    row = mapping.records[0]

    assert row.routability == Routability.UNMAPPED
    assert row.provider_asset_id == ""
    assert row.fractionable is None
    assert mapping.coverage == 0.0


def test_a_whole_share_asset_is_routable_and_not_reshaped() -> None:
    # §18. The mapping layer does not round. A whole-share-only instrument is
    # routable — its capability is known — and a fractional quantity for it is
    # refused later by the certified adapter, which is the existing semantics.
    mapping = mapping_of({1: "WHOLE"}, {"WHOLE": asset("WHOLE", fractionable=False)})
    row = mapping.records[0]

    assert row.routable is True
    assert row.as_venue_symbol().fractionable is False


# --------------------------------------------------------------------------
# §19, §22, §48: coverage and the mapping fingerprint
# --------------------------------------------------------------------------


def test_coverage_is_reported_and_never_overstated() -> None:
    # §19. Unresolved identities are counted, so coverage cannot read 100%
    # while something is unmapped.
    mapping = mapping_of(
        {1: "AAA", 2: "GHOST", 3: "BBB", 4: "BBB"},
        {"AAA": asset("AAA"), "BBB": asset("BBB")},
    )
    body = mapping.as_json()

    assert body["candidate_identities"] == 4
    assert body["routable"] == 1
    assert body["coverage"] == 0.25
    assert body["counts"] == {"AMBIGUOUS": 2, "ROUTABLE": 1, "UNMAPPED": 1}


def test_the_mapping_fingerprint_ignores_input_order() -> None:
    # §22.
    assets = {"AAA": asset("AAA"), "BBB": asset("BBB")}
    first = mapping_of({1: "AAA", 2: "BBB"}, assets)
    second = mapping_of({2: "BBB", 1: "AAA"}, assets)

    assert first.fingerprint() == second.fingerprint()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("answered", "CCC"),
        ("asset_id", "another-asset"),
        ("tradable", False),
        ("fractionable", False),
        ("status", "inactive"),
    ],
)
def test_a_changed_provider_fact_changes_the_mapping_fingerprint(field: str, value: Any) -> None:
    # §22, §48. Symbol, provider asset id, tradable, fractionable and status
    # all change where or whether an order goes, so all of them are inside the
    # digest.
    before = mapping_of({1: "AAA"}, {"AAA": asset("AAA")})
    after = mapping_of({1: "AAA"}, {"AAA": asset("AAA", **{field: value})})

    assert after.fingerprint() != before.fingerprint()


def test_the_same_identity_remapped_to_a_new_asset_changes_the_fingerprint() -> None:
    # §48, §26. A corporate action that moves an identity to another
    # instrument is a new mapping, not a silent edit of the old one.
    before = mapping_of({1: "OLD"}, {"OLD": asset("OLD", asset_id="asset-old")})
    after = mapping_of({1: "NEW"}, {"NEW": asset("NEW", asset_id="asset-new")})

    assert after.fingerprint() != before.fingerprint()


def test_the_mapping_records_when_the_provider_was_read() -> None:
    # §25. A mapping is a statement about the venue at an instant.
    mapping = mapping_of({1: "AAA"}, {"AAA": asset("AAA")})
    body = mapping.as_json()

    assert body["evidence_at"] == "2026-10-12T00:00:00+00:00"
    assert body["provider_evidence_fingerprint"] == "e" * 64
    assert body["version"] == INSTRUMENT_MAPPING_VERSION
    assert any("not a timeless fact" in row for row in body["limitations"])


def test_the_mapping_carries_no_credential_or_account_number() -> None:
    # §23.
    import json

    mapping = mapping_of({1: "AAA"}, {"AAA": asset("AAA")}, account_binding_fingerprint="a" * 64)
    text = json.dumps(mapping.as_json()).lower()

    for forbidden in ("secret", "authorization", "apca", "key_id", "paper-account"):
        assert forbidden not in text, forbidden
    assert "a" * 64 in text


# --------------------------------------------------------------------------
# §27-§34: what deploying these artefacts actually requires
# --------------------------------------------------------------------------


def test_the_runtime_binding_binds_neither_artefact() -> None:
    # §27. The mechanical fact the whole rebind question turns on: the binding
    # says nothing about a risk policy, a symbol mapping, instrument
    # capabilities or fractionability, so authoring either needs no rebind.
    import dataclasses

    from stock_screener.paper_runtime.binding import PaperOperationsRuntimeBinding

    names = {row.name for row in dataclasses.fields(PaperOperationsRuntimeBinding)}

    for absent in ("risk_policy", "symbol_mapping", "capabilities", "fractional"):
        assert not any(absent in name for name in names), absent
    assert "accounting_generation" in names
    assert "execution_journal_generation" in names
    assert "account_binding_fingerprint" in names


def test_the_generations_are_capability_independent() -> None:
    # §32. Instrument capability must not be conflated with accounting
    # identity, and the contracts do not: the generation is derived from the
    # runtime id alone.
    from pathlib import Path as _Path

    import stock_screener.paper_runtime.bootstrap as module

    source = _Path(module.__file__).read_text(encoding="utf-8")

    assert 'generation = f"alphatrader-paper-{runtime_id}"' in source
    assert "capabilit" not in source.split("generation = ")[1][:400]


def test_the_capability_lives_inside_the_recorded_account_fingerprint() -> None:
    # §32. Where the whole-shares-only default is actually stored: the venue
    # binding digests its capabilities, and the runtime records that digest.
    import dataclasses

    from stock_screener.execution.external import (
        BrokerEnvironment,
        ExternalBrokerAccountBinding,
        prove_paper_environment,
    )

    proof = prove_paper_environment(
        environment=BrokerEnvironment.PAPER,
        base_url="https://paper-api.alpaca.markets",
        key_id="PKTESTKEY01",
    )
    narrow = ExternalBrokerAccountBinding.paper(
        provider="ALPACA", account_id="acct", proof=proof, label="configured paper account"
    )
    wide = dataclasses.replace(
        narrow,
        capabilities=dataclasses.replace(narrow.capabilities, fractional_quantities=True),
    )

    assert narrow.capabilities.fractional_quantities is False
    assert "capabilities" in narrow.body()
    assert narrow.binding_fingerprint() != wide.binding_fingerprint()


def test_a_mapping_with_no_fractional_instrument_needs_no_rebind(
    paper_runtime: Any,
) -> None:
    # §28. Prefer preserving the existing lineages. When the mapping declares
    # nothing fractionable, the recorded capability already describes what is
    # in use and no field of the binding changes.
    plan = rebind_plan(
        binding=paper_runtime.binding,
        policy_fingerprint="p" * 64,
        mapping_fingerprint="m" * 64,
        mapping_needs_fractional=False,
    )

    assert plan.outcome == "REBIND_REUSE_EXISTING_LINEAGES"
    assert plan.resulting_binding_fields == ()
    assert plan.accounting_generation == paper_runtime.binding.accounting_generation
    assert "neither artefact is bound" in plan.reason


def test_a_fractional_mapping_names_the_exact_blocking_invariant(
    paper_runtime: Any,
) -> None:
    # §29. If reuse is impossible, name the field and the invariant — not a
    # vague "a rebind is needed".
    plan = rebind_plan(
        binding=paper_runtime.binding,
        policy_fingerprint="p" * 64,
        mapping_fingerprint="m" * 64,
        mapping_needs_fractional=True,
    )
    fields = dict(plan.resulting_binding_fields)

    assert plan.outcome == "REBIND_REQUIRES_NEW_GENERATIONS"
    assert "account_binding_fingerprint" in fields
    assert "RUNTIME_ALREADY_BOUND" in " ".join(plan.required_operator_actions)
    assert "BOOTSTRAP_ALREADY_DONE" in " ".join(plan.required_operator_actions)
    # And it says plainly that the ledger is not the reason.
    assert "not because of accounting semantics" in plan.reason
    assert "capability-independent" in fields["accounting_generation"]


def test_the_plan_authorises_nothing_and_forbids_a_convenience_reset(
    paper_runtime: Any,
) -> None:
    # §30, §31. A new policy or mapping is not a reason to reset accounting.
    plan = rebind_plan(
        binding=paper_runtime.binding, policy_fingerprint="p" * 64, mapping_fingerprint="m" * 64
    )
    body = plan.as_json()

    assert body["executed"] is False
    assert body["version"] == REBIND_PLAN_VERSION
    assert any("not an authorisation" in row for row in plan.prohibited)
    assert any("No accounting reset for convenience" in row for row in plan.prohibited)
    assert any("No paper re-arm" in row for row in plan.prohibited)
    assert any("No live authority" in row for row in plan.prohibited)


def test_the_plan_binds_every_artefact_it_describes(paper_runtime: Any) -> None:
    # §34.
    plan = rebind_plan(
        binding=paper_runtime.binding,
        policy_fingerprint=risk_policy_candidate().fingerprint(),
        mapping_fingerprint="m" * 64,
        operations_generation="ops-head",
    )
    body = plan.body()

    assert body["current_binding_fingerprint"] == paper_runtime.binding.fingerprint()
    assert body["policy_fingerprint"] == risk_policy_candidate().fingerprint()
    assert body["mapping_fingerprint"] == "m" * 64
    assert body["accounting_generation"] == paper_runtime.binding.accounting_generation
    assert body["execution_generation"] == (paper_runtime.binding.execution_journal_generation)
    assert body["operations_generation"] == "ops-head"
    assert body["bootstrap_epoch"] == paper_runtime.binding.bootstrap_epoch
    assert len(plan.fingerprint()) == 64


# --------------------------------------------------------------------------
# §52: the artefacts are the shape the production composition consumes
# --------------------------------------------------------------------------


def test_the_authored_artefacts_satisfy_the_production_composition(
    paper_runtime: Any, tmp_path: Path
) -> None:
    # §52. A temporary root, the real `compose_operations`, and the artefacts
    # written in the shape the mapping and the approved policy produce. This
    # proves configuration compatibility without touching the real runtime.
    from stock_screener.paper_operations import OperationsMode
    from stock_screener.paper_runtime import (
        compose_operations,
        write_risk_policy,
        write_symbol_mapping,
    )

    mapping = mapping_of(
        {1: "AAA", 2: "BBB", 3: "CCC"},
        {name: asset(name) for name in ("AAA", "BBB", "CCC")},
    )
    candidate = risk_policy_candidate()
    policy = candidate.authorised_policy(
        construction_mode="PRESERVE_ABSOLUTE_WEIGHTS",
        max_position_weight=1.0,
        max_positions=50,
        min_cash_weight=0.0,
        max_trade_notional_weight=None,
        min_position_weight=None,
        max_portfolio_volatility=None,
        max_position_adv_fraction=None,
        max_trade_adv_fraction=None,
        drawdown_bands=(),
        max_risk_metric_age_seconds=None,
        correlation_threshold=None,
        max_correlated_pair_exposure=None,
    )
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

    slot = paper_runtime.schedule.slot_for("2026-11-02T14:35:00+00:00")
    paper_runtime.clock.set("2026-11-02T14:35:00+00:00")
    composition = compose_operations(
        settings=_fake_settings(),
        root=paper_runtime.root,
        binding=paper_runtime.binding,
        slot=slot,
        schedule=paper_runtime.schedule,
        calendar=paper_runtime.calendar,
        mode=OperationsMode.EXTERNAL_PAPER,
        clock=paper_runtime.clock,
        client=_scripted_client(),
        marks={},
    )
    try:
        assert composition.submit is not None
        assert composition.submit.risk_policy.policy_id == "ALPHATRADER_PAPER"
        assert composition.submit.risk_policy.production_authorized is False
        assert len(composition.adapter.mapping.entries) == 3
        assert composition.readiness()["operator_inputs"]["complete"] is True
    finally:
        composition.close()


def _fake_settings() -> Any:
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


def _scripted_client() -> Any:
    """An HTTP client whose network answers nothing but reads."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    return httpx.Client(
        base_url="https://paper-api.alpaca.markets",
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )


def test_an_unauthorised_candidate_cannot_reach_the_composition(
    paper_runtime: Any,
) -> None:
    # §46. The composition must not fall back to EXAMPLE_POLICY_V1 when the
    # authored policy is absent or unusable. It refuses, naming the artefact.
    from stock_screener.paper_operations import OperationsMode
    from stock_screener.paper_runtime import compose_operations, write_symbol_mapping

    mapping = mapping_of({1: "AAA"}, {"AAA": asset("AAA")})
    write_symbol_mapping(paper_runtime.root, mapping.authored_symbols())
    slot = paper_runtime.schedule.slot_for("2026-11-02T14:35:00+00:00")

    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_RISK_POLICY_NOT_AUTHORED):
        compose_operations(
            settings=_fake_settings(),
            root=paper_runtime.root,
            binding=paper_runtime.binding,
            slot=slot,
            schedule=paper_runtime.schedule,
            calendar=paper_runtime.calendar,
            mode=OperationsMode.EXTERNAL_PAPER,
            clock=paper_runtime.clock,
            client=_scripted_client(),
            marks={},
        )


def test_an_unmapped_selected_identity_cannot_be_routed(paper_runtime: Any) -> None:
    # §20, §50. The strategy selects an identity the mapping does not cover:
    # the adapter has no symbol for it and there is no ticker fallback.
    from stock_screener.execution.errors import ExecutionError
    from stock_screener.portfolio_risk import SecurityIdentity

    mapping = mapping_of({1: "AAA"}, {"AAA": asset("AAA")})
    venue = mapping.venue_mapping()

    assert venue.symbol_for(SecurityIdentity(kind="COMPANY", identity_id=1)) == "AAA"
    with pytest.raises(ExecutionError):
        venue.symbol_for(SecurityIdentity(kind="COMPANY", identity_id=999))


def test_the_authored_rows_carry_the_provider_asset_id(paper_runtime: Any) -> None:
    # §21. The stronger provider identity travels with the authored mapping, so
    # a later symbol reuse is distinguishable from the original instrument.
    mapping = mapping_of({7: "AAA"}, {"AAA": asset("AAA", asset_id="prov-aaa")})
    rows = mapping.authored_symbols()

    assert rows == [
        {
            "kind": "COMPANY",
            "identity_id": 7,
            "symbol": "AAA",
            "fractionable": True,
            "provider_asset_id": "prov-aaa",
        }
    ]
