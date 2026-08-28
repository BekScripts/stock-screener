"""The deep research synthesis provider: a brief in, an unvalidated draft out.

Separate from `api_clients.research` on purpose, and not a subclass of it. The
two speak to the same vendor with the same posture, but they carry different
contracts — thirteen sections against seventeen, four bases against five — and a
shared base class would make a change to either one a change to both. Phase 3 is
frozen; the cheapest way to keep it that way is to leave it alone.

What is shared is the machinery underneath: error translation, response
inspection and list-price cost estimation all come from the Phase 3 module
rather than being written twice.

The posture is Phase 3's, because Phase 3's worked: structured output through an
explicit format, `claims` required by the schema, medium effort, a bounded output
ceiling, no automatic retries, and diagnostics that carry the stop reason and the
token usage — the two facts that distinguish "the model degenerated" from "the
answer was cut off".

One addition Phase 3 has no need for: `count_input_tokens`. A deep brief is much
larger than a Phase 3 one and its size varies with how much news exists, so the
runner measures the prompt before paying for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import anthropic
import structlog
from anthropic import transform_schema
from anthropic.types import JSONOutputFormatParam, OutputConfigParam
from pydantic import TypeAdapter, ValidationError

from api_clients.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderDataError,
    ProviderPlanError,
    ProviderRateLimitError,
    ProviderRequestError,
)

# Intra-package reuse of the Phase 3 request machinery. These are private to the
# package, not to the module: duplicating error translation would mean two
# behaviours to keep in agreement, and the one that drifted would be the one
# nobody was watching.
from api_clients.research import (
    _count,
    _fields,
    _shape,
    _status_error,
    _text,
    estimate_cost_usd,
)
from deep_research import DeepResearchDraft

log = structlog.get_logger(__name__)

DEFAULT_DEEP_MODEL = "claude-sonnet-5"

DEFAULT_MAX_OUTPUT_TOKENS = 8_000
"""Ceiling on one deep report's generated tokens.

Seventeen sections rather than thirteen, and on a thinking model this covers the
reasoning too. Phase 3 settled on the same number for a smaller report; live
measurement decides whether deep research needs more.
"""

DEFAULT_TIMEOUT_SECONDS = 180.0

_MAX_TOKENS_STOP = "max_tokens"


def _draft_format() -> JSONOutputFormatParam:
    """Build the structured-output format for a deep draft.

    Constructed here rather than passed as `output_format=` for the reason Phase
    3 documents: `messages.parse` raises from inside its own post-parser, taking
    the stop reason and the token usage with it, and those are exactly the facts
    a failed generation needs to explain itself.
    """
    return JSONOutputFormatParam(
        type="json_schema",
        schema=transform_schema(TypeAdapter(DeepResearchDraft).json_schema()),
    )


@dataclass(frozen=True, slots=True)
class DeepResearchCompletion:
    """One deep draft and what it cost.

    Attributes:
        draft: The model's output, parsed but entirely unvalidated. Only
            `deep_research.validate_deep_report` may turn this into a report.
        model_id: The model that answered, as it identified itself.
        input_tokens: Prompt tokens billed.
        output_tokens: Generated tokens billed.
        cache_read_tokens: Prompt tokens served from cache, billed at a fraction
            of the normal rate. Reported so a run's cost can be estimated
            honestly rather than from the list price alone.
        stop_reason: Why generation ended. `max_tokens` means the answer was cut
            off, which is a different problem from a bad answer.
    """

    draft: DeepResearchDraft
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    stop_reason: str = ""

    @property
    def truncated(self) -> bool:
        """Whether the answer was cut off at the output ceiling."""
        return self.stop_reason == _MAX_TOKENS_STOP

    def cost_usd(self) -> float:
        """Estimate what this generation cost, at list price and erring high."""
        return estimate_cost_usd(
            self.model_id,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
        )


@runtime_checkable
class DeepResearchProvider(Protocol):
    """A model that writes deep research drafts.

    Two methods, and the second exists because a deep brief's size is not known
    until it is built. Measuring before spending is the whole point of the input
    ceiling.
    """

    def count_input_tokens(self, *, system: str, brief: str) -> int:
        """Return how many prompt tokens this request would bill.

        Args:
            system: The instructions.
            brief: The rendered evidence.

        Returns:
            The token count. An estimate is acceptable where a vendor offers no
            counting endpoint, provided it errs high.

        Raises:
            ProviderError: If the count could not be obtained.
        """
        ...

    def generate_report(self, *, system: str, brief: str) -> DeepResearchCompletion:
        """Return one deep draft, or raise a provider error.

        Args:
            system: The instructions.
            brief: The rendered evidence.

        Returns:
            The draft and what it cost. Always unvalidated.

        Raises:
            ProviderError: If no usable draft could be obtained.
        """
        ...


class AnthropicDeepResearch:
    """Deep research drafts from Anthropic.

    Args:
        api_key: Anthropic credential. The same key Phase 3 uses — one vendor,
            so a second credential would be a second thing to rotate.
        model: Model identifier.
        max_output_tokens: Ceiling on generated tokens.
        effort: How hard the model works. The determinism lever, since current
            models reject `temperature`.
        timeout_seconds: Per-request timeout.
        client: SDK client, injected by tests.

    Raises:
        ProviderConfigurationError: If the credential is empty.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_DEEP_MODEL,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        effort: Literal["low", "medium", "high"] = "medium",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: Any = None,
    ) -> None:
        if client is None and not api_key:
            raise ProviderConfigurationError("deep research requires an API key")
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._effort = effort
        # `max_retries=0`: a deep generation is expensive and slow, and a silent
        # retry doubles the bill for a request that may have failed for a reason
        # retrying cannot fix. The caller decides.
        self._client = client or anthropic.Anthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=0
        )

    def count_input_tokens(self, *, system: str, brief: str) -> int:
        """Return how many prompt tokens this request would bill.

        Uses the vendor's own counting endpoint rather than a character
        heuristic, because the ceiling it feeds decides whether money is spent.
        Counting is free and fast; guessing is neither safe nor necessary.
        """
        try:
            counted = self._client.messages.count_tokens(
                model=self._model,
                system=system,
                messages=[{"role": "user", "content": brief}],
            )
        except anthropic.AuthenticationError as exc:
            raise ProviderAuthError("the deep research provider rejected the credential") from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderRequestError("the token count timed out") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderRequestError("could not reach the deep research provider") from exc
        except anthropic.APIStatusError as exc:
            raise _status_error(exc) from exc

        return int(getattr(counted, "input_tokens", 0))

    def generate_report(self, *, system: str, brief: str) -> DeepResearchCompletion:
        """Return one deep draft, or raise a provider error.

        Raises:
            ProviderAuthError: The credential was rejected.
            ProviderRateLimitError: The quota is spent.
            ProviderPlanError: The account may not use this model.
            ProviderDataError: The response could not be read as a draft.
            ProviderRequestError: The request timed out or the server failed.
        """
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_output_tokens,
                system=system,
                messages=[{"role": "user", "content": brief}],
                output_config=OutputConfigParam(effort=self._effort, format=_draft_format()),
            )
        except anthropic.AuthenticationError as exc:
            raise ProviderAuthError("the deep research provider rejected the credential") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderPlanError(f"this account may not use {self._model}") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimitError("the deep research provider is rate limiting") from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderRequestError("the deep research request timed out") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderRequestError("could not reach the deep research provider") from exc
        except anthropic.APIStatusError as exc:
            raise _status_error(exc) from exc

        return self._read(response)

    def _read(self, response: Any) -> DeepResearchCompletion:
        """Turn a vendor response into a completion, or explain why it cannot.

        Every refusal here arrives with HTTP 200 and a bill already incurred, so
        the explanation carries what the response said about itself.
        """
        if getattr(response, "stop_reason", None) == "refusal":
            raise ProviderDataError(
                f"the deep research provider declined to answer; {_shape(response)}"
            )

        text = _text(response)
        if not text.strip():
            raise ProviderDataError(
                f"the deep research provider returned no usable draft; {_shape(response)}"
            )

        try:
            draft = DeepResearchDraft.model_validate_json(text)
        except ValidationError as exc:
            raise ProviderDataError(
                f"the deep research provider returned a draft that failed validation: "
                f"{_fields(exc)}; {_shape(response)}"
            ) from exc

        usage = getattr(response, "usage", None)
        return DeepResearchCompletion(
            draft=draft,
            model_id=str(getattr(response, "model", self._model)),
            input_tokens=_count(usage, "input_tokens"),
            output_tokens=_count(usage, "output_tokens"),
            cache_read_tokens=_count(usage, "cache_read_input_tokens"),
            stop_reason=str(getattr(response, "stop_reason", "") or ""),
        )


class MockDeepResearch:
    """A `DeepResearchProvider` returning a pre-loaded draft.

    What the default configuration uses and what every automated test uses. It
    calls nothing, so the whole pipeline — brief, prompt, budget preflight,
    cache, validation, persistence — is exercisable without a credential or a
    bill.

    Args:
        draft: What to return. A caller wanting to exercise validation supplies
            a draft with deliberately bad claims.
        input_tokens: Prompt tokens to report.
        output_tokens: Generated tokens to report.
        failing: When set, every call raises `ProviderDataError` with this
            message. Exercises the provider-failure path.
        model_id: What to report as the answering model.
    """

    def __init__(
        self,
        draft: DeepResearchDraft | None = None,
        *,
        input_tokens: int = 1_000,
        output_tokens: int = 500,
        failing: str = "",
        model_id: str = "mock-deep-research",
    ) -> None:
        self._draft = draft or DeepResearchDraft(ticker="MOCK", claims=())
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._failing = failing
        self._model_id = model_id
        self.calls = 0
        self.counts = 0

    def count_input_tokens(self, *, system: str, brief: str) -> int:
        """Return the configured prompt size, without calling anything."""
        self.counts += 1
        return self._input_tokens

    def generate_report(self, *, system: str, brief: str) -> DeepResearchCompletion:
        """Return the configured draft.

        Raises:
            ProviderDataError: If this mock was configured to fail.
        """
        self.calls += 1
        if self._failing:
            raise ProviderDataError(self._failing)
        return DeepResearchCompletion(
            draft=self._draft,
            model_id=self._model_id,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )
