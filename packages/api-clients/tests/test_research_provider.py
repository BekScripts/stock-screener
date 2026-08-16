"""The research provider boundary: what comes back, and what every failure becomes.

The mapping from vendor errors to this codebase's vocabulary is the whole point
of these tests. Every one of them must end as a `ProviderError` subclass, because
the layer above turns exactly those into a `FAILED` research report — anything
that escapes as a vendor exception would instead end the run.
"""

from __future__ import annotations

import json
import re
from typing import Any

import anthropic
import httpx
import pytest
from anthropic import transform_schema
from pydantic import TypeAdapter

from api_clients import (
    AnthropicResearch,
    MockResearch,
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderDataError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderPlanError,
    ProviderRateLimitError,
    ProviderRequestError,
    ResearchProvider,
    estimate_cost_usd,
    parse_draft,
)
from research import (
    MAX_CLAIM_CHARS,
    MAX_DRAFT_CLAIM_CHARS,
    Basis,
    DraftClaim,
    DraftReport,
    Section,
)


def _draft(ticker: str = "XYZ") -> DraftReport:
    return DraftReport(
        ticker=ticker,
        claims=(
            DraftClaim(
                section=Section.BULL_CASE,
                text="Growth may persist.",
                basis=Basis.INTERPRETATION,
                evidence=("M.x",),
            ),
        ),
    )


class _Usage:
    input_tokens = 1200
    output_tokens = 800
    cache_read_input_tokens = 300


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Response:
    """The shape `messages.create` returns, reduced to what the adapter reads.

    The draft arrives as JSON text, exactly as it does on the wire — which is
    what makes a truncated or degenerate answer expressible in a test at all.
    """

    def __init__(self, draft: DraftReport | str | None, *, stop_reason: str = "end_turn") -> None:
        if draft is None:
            body = ""
        elif isinstance(draft, str):
            body = draft
        else:
            body = draft.model_dump_json()
        self.content = [_Block(body)]
        self.stop_reason = stop_reason
        self.model = "claude-sonnet-5"
        self.usage = _Usage()


class _Messages:
    def __init__(self, result: object | Exception) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Client:
    def __init__(self, result: object | Exception) -> None:
        self.messages = _Messages(result)


def _provider(result: object | Exception) -> tuple[AnthropicResearch, _Client]:
    client = _Client(result)
    provider = AnthropicResearch("test-key", client=client)  # type: ignore[arg-type]
    return provider, client


def _status_error(status: int, message: str = "x") -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    response = httpx.Response(
        status, request=request, json=body, headers={"request-id": "req_test123"}
    )
    return anthropic.APIStatusError("boom", response=response, body=body)


# --- the happy path --------------------------------------------------------


@pytest.mark.unit
def test_returns_the_parsed_draft_and_its_usage() -> None:
    provider, _ = _provider(_Response(_draft()))

    completion = provider.generate_report(system="rules", brief="evidence")

    assert completion.draft.ticker == "XYZ"
    assert completion.model_id == "claude-sonnet-5"
    assert completion.input_tokens == 1200
    assert completion.output_tokens == 800
    assert completion.cache_read_tokens == 300


@pytest.mark.unit
def test_sends_the_system_prompt_and_the_brief_separately() -> None:
    provider, client = _provider(_Response(_draft()))

    provider.generate_report(system="rules", brief="evidence")

    call = client.messages.calls[0]
    assert call["system"] == "rules"
    assert call["messages"] == [{"role": "user", "content": "evidence"}]


@pytest.mark.unit
def test_asks_for_the_draft_schema_and_bounds_the_output() -> None:
    provider, client = _provider(_Response(_draft()))

    provider.generate_report(system="rules", brief="evidence")

    call = client.messages.calls[0]
    assert call["max_tokens"] > 0
    assert call["output_config"]["effort"] == "medium"
    assert call["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.unit
def test_never_sends_a_sampling_temperature() -> None:
    # Current Claude models reject `temperature`, `top_p` and `top_k` with a 400.
    # Determinism comes from effort and the prompt; sending one would fail every
    # request.
    provider, client = _provider(_Response(_draft()))

    provider.generate_report(system="rules", brief="evidence")

    call = client.messages.calls[0]
    assert "temperature" not in call
    assert "top_p" not in call
    assert "top_k" not in call


# --- every failure becomes a provider error --------------------------------


@pytest.mark.unit
def test_a_rejected_credential_is_an_auth_error() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(401, request=request, json={"error": {"message": "bad key"}})
    provider, _ = _provider(anthropic.AuthenticationError("no", response=response, body=None))

    with pytest.raises(ProviderAuthError, match="rejected the credential"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_a_rate_limit_is_a_rate_limit_error() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request, json={"error": {"message": "slow down"}})
    provider, _ = _provider(anthropic.RateLimitError("429", response=response, body=None))

    with pytest.raises(ProviderRateLimitError, match="rate limiting"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_a_forbidden_model_is_a_plan_error() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(403, request=request, json={"error": {"message": "no access"}})
    provider, _ = _provider(anthropic.PermissionDeniedError("403", response=response, body=None))

    with pytest.raises(ProviderPlanError, match="may not use"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_a_timeout_is_a_request_error() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    provider, _ = _provider(anthropic.APITimeoutError(request=request))

    with pytest.raises(ProviderRequestError, match="timed out"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_a_connection_failure_is_a_request_error() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    provider, _ = _provider(anthropic.APIConnectionError(request=request))

    with pytest.raises(ProviderRequestError, match="could not reach"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_a_server_error_is_a_request_error() -> None:
    provider, _ = _provider(_status_error(503))

    with pytest.raises(ProviderRequestError, match="failed \\(503\\)"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_a_rejected_request_is_permanent_not_bad_model_output() -> None:
    # A 400 means the request we built is unacceptable. Treating it as bad model
    # output would record one FAILED report per company for a defect that is
    # identical every time.
    provider, _ = _provider(_status_error(400, "Schema is too complex."))

    with pytest.raises(ProviderInvalidRequestError, match="Schema is too complex"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_a_rejected_request_keeps_the_vendor_explanation_and_request_id() -> None:
    provider, _ = _provider(_status_error(400, "Your credit balance is too low."))

    with pytest.raises(ProviderInvalidRequestError) as caught:
        provider.generate_report(system="s", brief="b")

    assert "credit balance" in str(caught.value)
    assert "request req_test123" in str(caught.value)


@pytest.mark.unit
def test_a_transient_failure_is_not_permanent() -> None:
    # The distinction the runner branches on: 5xx keeps the run going, 4xx stops it.
    provider, _ = _provider(_status_error(503))

    with pytest.raises(ProviderRequestError) as caught:
        provider.generate_report(system="s", brief="b")

    assert not isinstance(caught.value, ProviderInvalidRequestError)


@pytest.mark.unit
def test_a_draft_that_fails_its_own_schema_is_a_data_error() -> None:
    # HTTP 200, tokens already billed, and the body does not satisfy the
    # contract. This must land on the failure boundary the runner already
    # handles, not escape as a traceback that ends the whole command.
    over_long = json.dumps(
        {
            "ticker": "XYZ",
            "claims": [
                {
                    "section": Section.BULL_CASE.value,
                    "text": "x" * (MAX_DRAFT_CLAIM_CHARS + 1),
                    "basis": Basis.INTERPRETATION.value,
                    "evidence": ["M.x"],
                }
            ],
        }
    )
    provider, _ = _provider(_Response(over_long))

    with pytest.raises(ProviderDataError, match=re.escape("claims.0.text")):
        provider.generate_report(system="rules", brief="evidence")


@pytest.mark.unit
def test_a_parse_failure_does_not_abort_the_run() -> None:
    # ProviderInvalidRequestError aborts every remaining candidate; a bad draft
    # from one company must not.
    provider, _ = _provider(_Response('{"claims": []}'))

    with pytest.raises(ProviderDataError) as caught:
        provider.generate_report(system="rules", brief="evidence")

    assert not isinstance(caught.value, ProviderInvalidRequestError)


@pytest.mark.unit
def test_a_parse_failure_names_the_field_that_failed() -> None:
    provider, _ = _provider(_Response('{"claims": []}'))

    with pytest.raises(ProviderDataError) as caught:
        provider.generate_report(system="rules", brief="evidence")

    assert "ticker: missing" in str(caught.value)


@pytest.mark.unit
def test_a_refusal_is_a_data_error() -> None:
    provider, _ = _provider(_Response(_draft(), stop_reason="refusal"))

    with pytest.raises(ProviderDataError, match="declined to answer"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_a_missing_draft_is_a_data_error() -> None:
    provider, _ = _provider(_Response(None))

    with pytest.raises(ProviderDataError, match="no usable draft"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_a_truncated_answer_says_it_was_truncated() -> None:
    # An answer cut off at the token ceiling fails to parse exactly as a
    # degenerate one does. Only the stop reason tells them apart, so a failure
    # that omits it forces the question to be guessed at later.
    provider, _ = _provider(_Response('{"ticker": "XYZ", "clai', stop_reason="max_tokens"))

    with pytest.raises(ProviderDataError, match="truncated=True") as caught:
        provider.generate_report(system="s", brief="b")

    assert "stop_reason=max_tokens" in str(caught.value)


@pytest.mark.unit
def test_a_degenerate_answer_is_reported_as_not_truncated() -> None:
    provider, _ = _provider(_Response("basbasbasis nan", stop_reason="end_turn"))

    with pytest.raises(ProviderDataError, match="truncated=False"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_a_failed_response_reports_what_it_spent() -> None:
    provider, _ = _provider(_Response("not json", stop_reason="end_turn"))

    with pytest.raises(ProviderDataError) as caught:
        provider.generate_report(system="s", brief="b")

    assert "input_tokens=1200" in str(caught.value)
    assert "output_tokens=800" in str(caught.value)


@pytest.mark.unit
def test_a_failed_response_never_quotes_what_the_model_wrote() -> None:
    provider, _ = _provider(_Response("SECRET-SOUP basbasis", stop_reason="end_turn"))

    with pytest.raises(ProviderDataError) as caught:
        provider.generate_report(system="s", brief="b")

    assert "SECRET-SOUP" not in str(caught.value)


# --- parsing raw output ----------------------------------------------------


@pytest.mark.unit
def test_parses_a_well_formed_draft() -> None:
    draft = parse_draft(_draft("ACME").model_dump_json())

    assert draft.ticker == "ACME"


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "{}",
        '{"ticker": "XYZ", "sections": "not an object"}',
        '{"ticker": "XYZ", "sections": {}, "final_score": 88}',
    ],
)
def test_malformed_output_is_a_data_error(payload: str) -> None:
    with pytest.raises(ProviderDataError, match="malformed draft"):
        parse_draft(payload)


# --- the mock --------------------------------------------------------------


@pytest.mark.unit
def test_the_mock_satisfies_the_protocol() -> None:
    assert isinstance(MockResearch(), ResearchProvider)


@pytest.mark.unit
def test_the_mock_returns_configured_drafts_in_order() -> None:
    provider = MockResearch([_draft("AAA"), _draft("BBB")])

    assert provider.generate_report(system="s", brief="b").draft.ticker == "AAA"
    assert provider.generate_report(system="s", brief="b").draft.ticker == "BBB"


@pytest.mark.unit
def test_the_mock_records_what_it_was_sent() -> None:
    provider = MockResearch([_draft()])

    provider.generate_report(system="rules", brief="evidence")

    assert provider.calls == [("rules", "evidence")]


@pytest.mark.unit
def test_a_mock_with_no_drafts_fails_like_an_empty_response() -> None:
    with pytest.raises(ProviderDataError, match="no usable draft"):
        MockResearch().generate_report(system="s", brief="b")


@pytest.mark.unit
def test_the_mock_raises_whatever_it_was_given() -> None:
    provider = MockResearch(error=ProviderRateLimitError("429"))

    with pytest.raises(ProviderRateLimitError):
        provider.generate_report(system="s", brief="b")


# --- the schema the provider asks for --------------------------------------


@pytest.mark.unit
def test_the_draft_schema_forbids_extra_fields() -> None:
    # Structured output requires closed objects. A schema that allowed extras
    # would let a model return a score field the contract has no place for.
    schema = DraftReport.model_json_schema()

    assert schema["additionalProperties"] is False
    assert all(
        definition.get("additionalProperties") is False
        for definition in schema["$defs"].values()
        if definition.get("type") == "object"
    )


@pytest.mark.unit
def test_the_draft_schema_has_all_thirteen_sections() -> None:
    sections = DraftReport.model_json_schema()["$defs"]["Section"]["enum"]

    assert set(sections) == {section.value for section in Section}


# --- preflight: configuration failure is not provider failure ---------------


@pytest.mark.unit
def test_a_configuration_error_is_not_a_provider_error() -> None:
    # The runner catches `ProviderError` and turns it into a FAILED report. A
    # misconfiguration must never take that path — there is nothing to record.
    assert not issubclass(ProviderConfigurationError, ProviderError)


@pytest.mark.unit
def test_preflight_passes_for_a_configured_provider() -> None:
    AnthropicResearch("sk-test").preflight()


@pytest.mark.unit
@pytest.mark.parametrize("key", ["", "   "])
def test_preflight_rejects_a_blank_credential(key: str) -> None:
    with pytest.raises(ProviderConfigurationError, match="no research credential"):
        AnthropicResearch(key).preflight()


@pytest.mark.unit
def test_preflight_rejects_a_blank_model() -> None:
    with pytest.raises(ProviderConfigurationError, match="no research model"):
        AnthropicResearch("sk-test", model="  ").preflight()


@pytest.mark.unit
def test_preflight_makes_no_request() -> None:
    provider, client = _provider(_Response(_draft()))

    provider.preflight()

    assert client.messages.calls == []


@pytest.mark.unit
def test_an_empty_mock_fails_preflight() -> None:
    with pytest.raises(ProviderConfigurationError, match="no drafts loaded"):
        MockResearch().preflight()


@pytest.mark.unit
def test_a_loaded_mock_passes_preflight() -> None:
    MockResearch([_draft()]).preflight()


@pytest.mark.unit
def test_a_mock_configured_to_fail_passes_preflight() -> None:
    # Configured to raise is configured. Only the empty default is the accident.
    MockResearch(error=ProviderRateLimitError("429")).preflight()


# --- the exact schema the SDK sends ----------------------------------------


def _sent_schema() -> dict[str, Any]:
    """The schema the adapter actually transmits, read off the recorded call."""
    provider, client = _provider(_Response(_draft()))
    provider.generate_report(system="rules", brief="evidence")
    schema = client.messages.calls[0]["output_config"]["format"]["schema"]
    assert isinstance(schema, dict)
    return schema


@pytest.mark.unit
def test_the_request_is_the_one_messages_parse_would_have_sent() -> None:
    # The adapter builds the structured-output format itself rather than passing
    # `output_format=`, so that a parse failure leaves the response — and its
    # stop_reason — in hand. That is a diagnostic change, not a generation
    # change, and this is what says so: the schema on the wire is byte-identical
    # to the one the SDK's own helper would have built.
    assert _sent_schema() == transform_schema(TypeAdapter(DraftReport).json_schema())


@pytest.mark.unit
def test_the_sent_schema_carries_all_thirteen_sections() -> None:
    # Flattening moved the sections from thirteen object properties to thirteen
    # enum values. Every one must still be nameable, or the model could not
    # answer a section even if it wanted to.
    sections = _sent_schema()["$defs"]["Section"]["enum"]

    assert set(sections) == {section.value for section in Section}


@pytest.mark.unit
def test_the_sent_schema_has_one_claims_array_not_thirteen() -> None:
    # The whole point of the flattening: the live API rejected thirteen
    # parallel arrays of objects as "Schema is too complex."
    root = _sent_schema()

    assert set(root["properties"]) == {
        "ticker",
        "claims",
        "confidence",
        "confidence_rationale",
    }
    assert root["properties"]["claims"]["type"] == "array"
    assert "ReportSections" not in root["$defs"]


@pytest.mark.unit
def test_the_sent_schema_asks_for_the_claims_it_cannot_do_without() -> None:
    # Optional, the model may answer with a minimal object carrying no claims at
    # all and still satisfy the schema — which is what it did, four times out of
    # four, when this was left to its discretion.
    root = _sent_schema()

    assert "claims" in root["required"]


@pytest.mark.unit
def test_every_object_in_the_sent_schema_is_closed() -> None:
    schema = _sent_schema()
    objects = [schema, *(d for d in schema["$defs"].values() if d.get("type") == "object")]

    assert all(obj["additionalProperties"] is False for obj in objects)


@pytest.mark.unit
def test_the_sent_schema_requires_the_fields_the_contract_requires() -> None:
    schema = _sent_schema()

    assert set(schema["required"]) == {"ticker", "claims"}
    assert set(schema["$defs"]["DraftClaim"]["required"]) == {"section", "text", "basis"}


@pytest.mark.unit
def test_the_sent_schema_uses_no_unsupported_construct() -> None:
    # Structured output rejects numeric and string constraints, and the
    # combinators below. The SDK demotes them into `description`; this asserts
    # none survives as an actual schema keyword.
    banned = {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "pattern",
        "patternProperties",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "dependentSchemas",
    }
    found: list[str] = []

    def walk(node: object, path: str = "$") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in banned:
                    found.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(_sent_schema())

    assert found == []


@pytest.mark.unit
def test_the_wire_schema_does_not_impose_the_accepted_claim_length() -> None:
    # The API strips length constraints from the schema and demotes them to a
    # description hint, so a strict wire bound is unenforceable and merely
    # destroys whole responses. The real 240-character rule lives in
    # `validate_report`, where overrunning costs one claim.
    text = _sent_schema()["$defs"]["DraftClaim"]["properties"]["text"]

    assert "maxLength" not in text
    assert f"maxLength: {MAX_DRAFT_CLAIM_CHARS}" in text["description"]
    assert MAX_DRAFT_CLAIM_CHARS > MAX_CLAIM_CHARS


@pytest.mark.unit
def test_a_long_claim_parses_off_the_wire() -> None:
    # The DELL incident: HTTP 200, one claim past 240 characters, and pydantic
    # rejected the entire paid draft. It must parse now.
    long_text = "Revenue growth is accelerating. " * 12
    assert len(long_text) > MAX_CLAIM_CHARS

    draft = DraftReport(
        ticker="XYZ",
        claims=(
            DraftClaim(
                section=Section.GROWTH_DRIVERS,
                text=long_text,
                basis=Basis.INTERPRETATION,
                evidence=("M.x",),
            ),
        ),
    )

    assert len(draft.claims[0].text) > MAX_CLAIM_CHARS


# --- cost estimation -------------------------------------------------------


@pytest.mark.unit
def test_prices_a_known_model_from_its_published_rate() -> None:
    # Sonnet 5 lists at $3 per million in, $15 per million out.
    cost = estimate_cost_usd("claude-sonnet-5", 1_000_000, 1_000_000)

    assert cost == pytest.approx(18.0)


@pytest.mark.unit
def test_an_unknown_model_is_priced_at_the_dearest_rate_known() -> None:
    # A guard that guesses low on an unfamiliar model is not a guard.
    unknown = estimate_cost_usd("claude-something-7", 1_000_000, 1_000_000)
    dearest = max(
        estimate_cost_usd(model, 1_000_000, 1_000_000)
        for model in ("claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")
    )

    assert unknown == pytest.approx(dearest)


@pytest.mark.unit
def test_cached_prompt_tokens_are_billed_at_the_full_input_rate() -> None:
    # They are actually billed at a fraction of it. Estimating them at full
    # price is the error direction a spend guard is allowed to make.
    cached = estimate_cost_usd("claude-sonnet-5", 0, 0, cache_read_tokens=1_000_000)

    assert cached == pytest.approx(3.0)


@pytest.mark.unit
def test_a_request_that_billed_nothing_costs_nothing() -> None:
    assert estimate_cost_usd("claude-sonnet-5", 0, 0) == 0.0


@pytest.mark.unit
@pytest.mark.parametrize("tokens", [-1, -10_000])
def test_a_negative_token_count_never_produces_a_credit(tokens: int) -> None:
    assert estimate_cost_usd("claude-sonnet-5", tokens, tokens) == 0.0


@pytest.mark.unit
def test_output_tokens_cost_more_than_input_tokens() -> None:
    assert estimate_cost_usd("claude-sonnet-5", 0, 1000) > estimate_cost_usd(
        "claude-sonnet-5", 1000, 0
    )
