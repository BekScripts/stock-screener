"""The deep research provider contract, against a fake SDK client."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx
import pytest

from api_clients import AnthropicDeepResearch, MockDeepResearch
from api_clients.deep_research import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DeepResearchCompletion,
)
from api_clients.errors import ProviderAuthError, ProviderConfigurationError, ProviderDataError
from deep_research import DeepBasis, DeepDraftClaim, DeepResearchDraft, DeepSection


class _Response:
    """A vendor response shaped like the SDK's."""

    def __init__(self, text: str, *, stop_reason: str = "end_turn") -> None:
        self.content = [type("Block", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason
        self.model = "claude-sonnet-5"
        self.usage = type(
            "Usage", (), {"input_tokens": 1234, "output_tokens": 567, "cache_read_input_tokens": 0}
        )()


class _Messages:
    """Records what the adapter asked for, and answers with a fixed response."""

    def __init__(self, response: Any = None, *, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.kwargs: dict[str, Any] = {}
        self.counted: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._response

    def count_tokens(self, **kwargs: Any) -> Any:
        self.counted = kwargs
        return type("Count", (), {"input_tokens": 4321})()


class _Client:
    def __init__(self, messages: _Messages) -> None:
        self.messages = messages


def _valid_draft_json() -> str:
    draft = DeepResearchDraft(
        ticker="ACME",
        claims=(
            DeepDraftClaim(
                section=DeepSection.BULL_CASE,
                text="Growth may persist.",
                basis=DeepBasis.INTERPRETATION,
                evidence=("S.growth",),
            ),
        ),
    )
    return draft.model_dump_json()


@pytest.mark.unit
def test_a_valid_response_becomes_a_draft_and_its_usage() -> None:
    messages = _Messages(_Response(_valid_draft_json()))
    provider = AnthropicDeepResearch("key", client=_Client(messages))

    completion = provider.generate_report(system="s", brief="b")

    assert isinstance(completion, DeepResearchCompletion)
    assert completion.draft.ticker == "ACME"
    assert completion.input_tokens == 1234
    assert completion.output_tokens == 567


@pytest.mark.unit
def test_the_output_ceiling_defaults_to_eight_thousand() -> None:
    messages = _Messages(_Response(_valid_draft_json()))
    AnthropicDeepResearch("key", client=_Client(messages)).generate_report(system="s", brief="b")

    assert messages.kwargs["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS == 8_000


@pytest.mark.unit
def test_effort_defaults_to_medium() -> None:
    messages = _Messages(_Response(_valid_draft_json()))
    AnthropicDeepResearch("key", client=_Client(messages)).generate_report(system="s", brief="b")

    assert messages.kwargs["output_config"]["effort"] == "medium"


@pytest.mark.unit
def test_the_schema_requires_claims() -> None:
    """A response omitting claims must be structurally invalid, not merely thin."""
    messages = _Messages(_Response(_valid_draft_json()))
    AnthropicDeepResearch("key", client=_Client(messages)).generate_report(system="s", brief="b")

    schema = messages.kwargs["output_config"]["format"]["schema"]
    assert "claims" in schema["required"]


@pytest.mark.unit
def test_tokens_are_counted_through_the_vendors_own_endpoint() -> None:
    """The ceiling decides whether money is spent, so it is measured, not guessed."""
    messages = _Messages()
    provider = AnthropicDeepResearch("key", client=_Client(messages))

    assert provider.count_input_tokens(system="s", brief="b") == 4321
    assert messages.counted["system"] == "s"


@pytest.mark.unit
def test_a_refusal_raises_rather_than_returning_an_empty_report() -> None:
    messages = _Messages(_Response("", stop_reason="refusal"))
    provider = AnthropicDeepResearch("key", client=_Client(messages))

    with pytest.raises(ProviderDataError, match="declined to answer"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_an_unparseable_response_raises() -> None:
    messages = _Messages(_Response('{"nonsense": true}'))
    provider = AnthropicDeepResearch("key", client=_Client(messages))

    with pytest.raises(ProviderDataError, match="failed validation"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_a_truncated_answer_is_visible_as_truncated() -> None:
    messages = _Messages(_Response(_valid_draft_json(), stop_reason="max_tokens"))
    provider = AnthropicDeepResearch("key", client=_Client(messages))

    assert provider.generate_report(system="s", brief="b").truncated


@pytest.mark.unit
def test_a_rejected_credential_is_reported_as_such() -> None:
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com"))
    error = anthropic.AuthenticationError("bad key", response=response, body=None)
    provider = AnthropicDeepResearch("key", client=_Client(_Messages(error=error)))

    with pytest.raises(ProviderAuthError, match="rejected the credential"):
        provider.generate_report(system="s", brief="b")


@pytest.mark.unit
def test_an_empty_credential_fails_before_any_request() -> None:
    with pytest.raises(ProviderConfigurationError, match="requires an API key"):
        AnthropicDeepResearch("")


@pytest.mark.unit
def test_no_automatic_retries_are_configured() -> None:
    """A retry doubles the bill for a request that may have failed permanently."""
    provider = AnthropicDeepResearch("key")

    assert provider._client.max_retries == 0


@pytest.mark.unit
def test_the_mock_provider_counts_its_calls() -> None:
    provider = MockDeepResearch()

    provider.count_input_tokens(system="s", brief="b")
    provider.generate_report(system="s", brief="b")

    assert (provider.counts, provider.calls) == (1, 1)


@pytest.mark.unit
def test_the_mock_provider_can_be_made_to_fail() -> None:
    provider = MockDeepResearch(failing="model down")

    with pytest.raises(ProviderDataError, match="model down"):
        provider.generate_report(system="s", brief="b")
