"""The outbound boundary for AI research: a brief goes out, a draft comes back.

This is the only place in the codebase that knows a language-model vendor exists.
Everything above it works in `research` contract types — `DraftReport` in,
validated `ResearchReport` out — so swapping vendors is a change to one adapter
and one setting, exactly as it is for prices and fundamentals.

Two boundaries are worth stating plainly.

**Nothing vendor-shaped escapes.** The adapter returns a `ResearchCompletion`
holding a parsed `DraftReport` and a token count. No message object, no content
block, no vendor error class crosses this line — so the validator and the
persistence layer cannot come to depend on a provider's response shape.

**A failure here is data, not an emergency.** A rate limit, a timeout or an
outage raises a `ProviderError`, which the application turns into a `FAILED`
research report. Scanning, scoring and ranking never depend on a model being
reachable, and a bad afternoon at a vendor costs a company its report and
nothing else.

The prompt itself lives in `research.prompt`, beside the validator that enforces
it. This module knows how to send two strings and read one answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import anthropic
import structlog
from anthropic import transform_schema
from anthropic.types import JSONOutputFormatParam, OutputConfigParam
from pydantic import TypeAdapter, ValidationError

from api_clients.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderDataError,
    ProviderInvalidRequestError,
    ProviderPlanError,
    ProviderRateLimitError,
    ProviderRequestError,
)
from research import DraftReport

if TYPE_CHECKING:
    from collections.abc import Sequence

log = structlog.get_logger(__name__)

DEFAULT_RESEARCH_MODEL = "claude-sonnet-5"
"""The model this adapter is written against.

Recorded on every report. A report written by one model is not evidence about
what another would have said, so the identifier travels with the output rather
than living only in configuration.
"""

DEFAULT_MAX_OUTPUT_TOKENS = 8000
"""Generated tokens allowed per report.

The cap covers reasoning as well as the thirteen sections, because the model
thinks by default and both are billed against the same ceiling.
"""

DEFAULT_TIMEOUT_SECONDS = 120.0
"""Per-request timeout. A thinking model writing thirteen sections is not fast."""

_SERVER_ERROR_FLOOR = 500

_MAX_TOKENS_STOP = "max_tokens"


def _draft_format() -> JSONOutputFormatParam:
    """Build the structured-output format for a draft report.

    This is what `messages.parse` builds internally from an `output_format=`
    argument: the contract's JSON schema, put through the SDK's own
    `transform_schema`. Constructing it here rather than passing `output_format`
    is what leaves the raw response in hand when parsing fails — `parse` raises
    from inside its own post-parser, taking the `stop_reason` and the token usage
    with it, and those two facts are the difference between "the model
    degenerated" and "the answer was cut off at the token ceiling".

    The bytes on the wire are the same either way. A test pins that.
    """
    return JSONOutputFormatParam(
        type="json_schema",
        schema=transform_schema(TypeAdapter(DraftReport).json_schema()),
    )


_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
"""List price per million input and output tokens, by model.

List price on purpose. Introductory and promotional rates are lower and expire,
and a spend guard that assumed the discount would let a run past its ceiling on
the day the discount ended.
"""

_UNKNOWN_PRICE = max(_PRICES_USD_PER_MTOK.values())
"""What an unrecognised model is assumed to cost: the dearest one known.

A model this table has never heard of is more likely to be newer and pricier
than cheaper, and a guard that guesses low on an unknown model is not a guard.
"""

_PER_MTOK = 1_000_000


def estimate_cost_usd(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> float:
    """Estimate what one request cost, deliberately erring high.

    An estimate, not an invoice. It reads list prices, bills cached prompt
    tokens at the full input rate rather than the discounted one, and charges an
    unknown model at the dearest rate known. Every one of those rounds against
    the caller, which is the only direction a spend guard may round.

    Args:
        model_id: The model that served the request.
        input_tokens: Prompt tokens billed.
        output_tokens: Generated tokens billed, reasoning included.
        cache_read_tokens: Prompt tokens served from the vendor's cache.

    Returns:
        Estimated cost in US dollars. Never negative.
    """
    input_price, output_price = _PRICES_USD_PER_MTOK.get(model_id, _UNKNOWN_PRICE)
    prompt = max(input_tokens, 0) + max(cache_read_tokens, 0)
    return (prompt * input_price + max(output_tokens, 0) * output_price) / _PER_MTOK


@dataclass(frozen=True, slots=True)
class ResearchCompletion:
    """One model response, normalised.

    Attributes:
        draft: The unvalidated report, parsed into the contract's own type. It
            has been through the model's structured-output schema and pydantic;
            it has **not** been through `validate_report`, and must not be
            persisted or shown until it has.
        model_id: Which model produced it.
        input_tokens: Prompt tokens billed.
        output_tokens: Generated tokens billed, reasoning included.
        cache_read_tokens: Prompt tokens served from the vendor's cache, billed
            at a fraction of the normal rate. Reported so a run's cost can be
            estimated honestly rather than from the list price alone.
    """

    draft: DraftReport
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


@runtime_checkable
class ResearchProvider(Protocol):
    """A source of draft research reports."""

    def preflight(self) -> None:
        """Raise unless this provider is configured well enough to be asked.

        Called once before a run starts, never per company, and never over the
        network. It exists so a missing credential costs nothing: no request, no
        `FAILED` rows, and an error that names the setting rather than looking
        like a vendor outage.

        Raises:
            ProviderConfigurationError: If the provider cannot serve requests as
                configured. Not a `ProviderError` — see that class for why.
        """
        ...

    def generate_report(self, *, system: str, brief: str) -> ResearchCompletion:
        """Return one draft report for one rendered brief.

        Args:
            system: The instructions — what the model may and may not do.
            brief: The evidence, rendered as text.

        Returns:
            The draft and what it cost. Always unvalidated.

        Raises:
            ProviderError: If the request failed, was refused, or produced
                something that could not be read as a draft. Every one of these
                becomes a `FAILED` research report rather than ending a run.
        """
        ...


class AnthropicResearch:
    """Writes draft reports with an Anthropic model.

    Structured output does the parsing: the request carries the `DraftReport`
    schema, so the response arrives as that shape or not at all. Free-text
    parsing of a thirteen-section report would be its own failure surface.

    **There is no temperature.** Current Claude models reject `temperature`,
    `top_p` and `top_k` outright, so the determinism this task wants comes from
    `effort` and from a prompt that leaves little room for invention. A caller
    asking for "low temperature" gets low effort; the knob is differently shaped,
    not missing.

    Retries are the SDK's, which retry exactly the transient failures — 408, 409,
    429, 5xx and connection errors — and never a rejected request. Adding a
    second retry layer here would multiply the wait on an outage without
    improving the outcome.

    Args:
        api_key: The credential.
        model: Model identifier.
        max_output_tokens: Ceiling on generated tokens, reasoning included.
        effort: How hard the model works per report.
        timeout_seconds: Per-request timeout.
        max_attempts: Total tries per request, including the first.
        client: An injected client, for tests. One is built when absent.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_RESEARCH_MODEL,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        effort: Literal["low", "medium", "high"] = "medium",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = 3,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._api_key = api_key
        self._injected = client is not None
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._effort = effort
        self._client = client or anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max(max_attempts - 1, 0),
        )

    def preflight(self) -> None:
        """Raise unless a credential and a model are configured.

        Checked locally. A key that exists but is wrong is a runtime failure, not
        a configuration one — the vendor is the only thing that can tell us that,
        and finding out costs a request.

        Raises:
            ProviderConfigurationError: If no credential or no model is set.
        """
        if not self._injected and not self._api_key.strip():
            raise ProviderConfigurationError("no research credential is configured")
        if not self._model.strip():
            raise ProviderConfigurationError("no research model is configured")

    def generate_report(self, *, system: str, brief: str) -> ResearchCompletion:
        """Return one draft report, or raise a provider error.

        Args:
            system: The instructions.
            brief: The rendered evidence.

        Returns:
            The parsed draft and its token usage.

        Raises:
            ProviderAuthError: The credential was rejected.
            ProviderRateLimitError: The quota is spent, after the SDK's retries.
            ProviderPlanError: The account may not use this model.
            ProviderDataError: The response could not be read as a draft — it
                declined to answer, returned nothing usable, or returned
                something that failed the contract's own schema after arriving.
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
            raise ProviderAuthError("the research provider rejected the credential") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderPlanError(f"this account may not use {self._model}") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimitError("the research provider is rate limiting") from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderRequestError("the research request timed out") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderRequestError("could not reach the research provider") from exc
        except anthropic.APIStatusError as exc:
            raise _status_error(exc) from exc

        return self._read(response)

    def _read(self, response: Any) -> ResearchCompletion:
        """Turn a vendor response into a completion, or explain why it cannot.

        Every refusal here arrives with HTTP 200 and a bill already incurred, so
        the explanation carries what the response said about itself: why it
        stopped, and how many tokens it spent doing so.
        """
        if getattr(response, "stop_reason", None) == "refusal":
            raise ProviderDataError(f"the research provider declined to answer; {_shape(response)}")

        text = _text(response)
        if not text.strip():
            raise ProviderDataError(
                f"the research provider returned no usable draft; {_shape(response)}"
            )

        try:
            draft = DraftReport.model_validate_json(text)
        except ValidationError as exc:
            # HTTP 200, then the response failed its own schema client-side.
            # The tokens are already spent; the only thing left to decide is
            # whether this ends the run. It does not — the next company's
            # response may well parse.
            raise ProviderDataError(
                f"the research provider returned a draft that failed validation: "
                f"{_fields(exc)}; {_shape(response)}"
            ) from exc

        usage = getattr(response, "usage", None)
        return ResearchCompletion(
            draft=draft,
            model_id=str(getattr(response, "model", self._model)),
            input_tokens=_count(usage, "input_tokens"),
            output_tokens=_count(usage, "output_tokens"),
            cache_read_tokens=_count(usage, "cache_read_input_tokens"),
        )


class MockResearch:
    """A `ResearchProvider` that returns pre-loaded drafts, or fails on cue.

    Every failure this adapter can be asked for is one the real one raises, which
    is what makes it useful: the pipeline's rate-limit, timeout and malformed
    behaviour is testable without a vendor, a key or a wait.

    Args:
        drafts: Drafts to return, one per call, in order. The last is repeated
            once the list runs out. An empty list raises `ProviderDataError`,
            which is how a provider that returned nothing behaves.
        error: Raise this instead of answering. Overrides `drafts`.
        input_tokens: Prompt tokens to report.
        output_tokens: Generated tokens to report.
        model_id: Model identifier to report.
    """

    def __init__(
        self,
        drafts: Sequence[DraftReport] = (),
        *,
        error: Exception | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model_id: str = "mock-research",
    ) -> None:
        self._drafts = list(drafts)
        self._error = error
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._model_id = model_id
        self.calls: list[tuple[str, str]] = []

    def preflight(self) -> None:
        """Raise unless this mock has something to return.

        A mock with neither drafts nor an error is the accidental case: someone
        ran the real command with the default provider still selected. Failing
        here is what stops that run writing a table full of `FAILED` rows that
        say nothing about any company.

        Raises:
            ProviderConfigurationError: If nothing was configured.
        """
        if not self._drafts and self._error is None:
            raise ProviderConfigurationError(
                "the mock research provider has no drafts loaded; configure a real "
                "provider with RESEARCH_PROVIDER and RESEARCH_API_KEY"
            )

    def generate_report(self, *, system: str, brief: str) -> ResearchCompletion:
        """Return the next configured draft, or raise the configured error.

        Raises:
            ProviderError: Whatever the mock was configured to raise.
            ProviderDataError: If no drafts were configured at all.
        """
        self.calls.append((system, brief))
        if self._error is not None:
            raise self._error
        if not self._drafts:
            raise ProviderDataError("the research provider returned no usable draft")

        draft = self._drafts.pop(0) if len(self._drafts) > 1 else self._drafts[0]
        return ResearchCompletion(
            draft=draft,
            model_id=self._model_id,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )


def parse_draft(payload: str) -> DraftReport:
    """Return a draft parsed from raw JSON, or explain why it cannot be.

    Exposed because a malformed response is a normal outcome worth testing, and
    because a provider that does not support structured output would need this
    path. Parsing failure is a `ProviderDataError` — never a crash, and never a
    half-populated draft.

    Args:
        payload: The model's raw JSON output.

    Returns:
        The parsed draft.

    Raises:
        ProviderDataError: If the payload is not a well-formed draft.
    """
    try:
        return DraftReport.model_validate_json(payload)
    except ValidationError as exc:
        raise ProviderDataError("the research provider returned a malformed draft") from exc


def _status_error(exc: anthropic.APIStatusError) -> Exception:
    """Map a vendor status error to this codebase's vocabulary.

    The split that matters is transient versus permanent, not the status number.
    A 5xx is the server having a bad moment and the next company may well
    succeed; a 4xx means the request we built is unacceptable, and building the
    same request for the next company would waste another call to learn the same
    thing.
    """
    if exc.status_code >= _SERVER_ERROR_FLOOR:
        return ProviderRequestError(f"the research provider failed ({exc.status_code})")

    detail = _detail(exc)
    request_id = f" [request {exc.request_id}]" if exc.request_id else ""
    return ProviderInvalidRequestError(
        f"the research provider rejected the request ({exc.status_code}): {detail}{request_id}"
    )


def _text(response: Any) -> str:
    """Return the response's text blocks, joined. Never the blocks themselves."""
    return "".join(
        str(getattr(block, "text", ""))
        for block in getattr(response, "content", ())
        if getattr(block, "type", None) == "text"
    )


def _shape(response: Any) -> str:
    """Describe a response without quoting a word of what it said.

    Structured output can arrive incomplete for a reason that is not the model's
    fault: hitting the token ceiling truncates the JSON mid-document, which fails
    to parse exactly as a degenerate answer would. `stop_reason` is what tells
    the two apart, and raising a failure without it means guessing later.
    """
    usage = getattr(response, "usage", None)
    stop_reason = getattr(response, "stop_reason", None)
    return (
        f"stop_reason={stop_reason}, "
        f"input_tokens={_count(usage, 'input_tokens')}, "
        f"output_tokens={_count(usage, 'output_tokens')}, "
        f"truncated={stop_reason == _MAX_TOKENS_STOP}"
    )


def _fields(exc: ValidationError) -> str:
    """Name the fields a response failed on, without echoing their contents.

    A response body can contain anything the model wrote; the field path and the
    rule it broke are enough to debug from and carry nothing worth redacting.
    """
    parts = [
        f"{'.'.join(str(item) for item in error['loc'])}: {error['type']}"
        for error in exc.errors()[:5]
    ]
    return "; ".join(parts) or "no field reported"


def _detail(exc: anthropic.APIStatusError) -> str:
    """Return the vendor's own explanation, or a placeholder.

    Read from the parsed body rather than `str(exc)`, which embeds the whole
    response. Credentials never appear in a response body, and the request id is
    an opaque correlation handle — neither leaks anything.
    """
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
    return "no explanation supplied"


def _count(usage: object, field: str) -> int:
    """Read one token count off a usage object, defaulting to zero."""
    value = getattr(usage, field, 0)
    return value if isinstance(value, int) else 0
