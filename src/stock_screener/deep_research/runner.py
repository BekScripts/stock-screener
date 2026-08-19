"""Running deep research for one company, and refusing to when it should not.

The order here is the whole design, and two steps of it exist purely to avoid
spending money badly.

    prepare → collect → assemble → render → measure → cache → guard → generate
    → validate → persist

**The cache is consulted before the provider, never after.** Identical evidence
under an identical prompt and model is a report that already exists, and paying
for it again would buy a second copy of the same reading.

**The prompt is measured before it is sent.** A brief that exceeds the input
ceiling aborts, reports its size, and calls nothing. It is never trimmed to fit:
silently dropping evidence produces a report whose gaps are invisible, which is
worse than no report at all.

Failures are graded rather than pooled. A Tavily outage costs the external half
of the evidence and nothing else — the run continues on deterministic and SEC
evidence, which is exactly the shape of a company nobody has written about
lately. A preparation failure aborts before anything is paid for. A provider
failure persists nothing: a fabricated report is worse than a missing one, and
there is no such thing as a partial charge worth saving.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from api_clients import ProviderError
from data_access import (
    EXTERNAL_DEGRADED,
    EXTERNAL_FRESH,
    EXTERNAL_REUSED,
    DeepResearchReportRepository,
)
from deep_research import (
    CURRENT_DEEP_PROMPT_VERSION,
    ExternalEvidence,
    build_deep_system_prompt,
    render_deep_brief,
    validate_deep_report,
)
from stock_screener.deep_research.brief import assemble_deep_brief
from stock_screener.deep_research.collection import collect_external_evidence
from stock_screener.deep_research.preparation import PreparationError, prepare_company

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from api_clients import (
        DeepResearchProvider,
        ExternalResearchProvider,
        FundamentalsProvider,
        MarketDataProvider,
    )
    from data_access import StoredDeepResearchReport
    from deep_research import DeepResearchBrief, DeepResearchReport
    from stock_screener.config import Settings
    from stock_screener.deep_research.collection import CollectionReport
    from stock_screener.deep_research.preparation import PreparationResult

log = structlog.get_logger(__name__)


class RunStatus(StrEnum):
    """How a deep research run ended.

    Graded rather than pooled, because the six outcomes call for six different
    responses from whoever asked. Three of them cost nothing.
    """

    COMPLETE = "COMPLETE"
    """A clean validated report: every claim survived."""

    VALIDATION_PARTIAL = "VALIDATION_PARTIAL"
    """A useful report, with claims dropped and the reasons recorded."""

    CACHE_HIT = "CACHE_HIT"
    """The same evidence under the same prompt already has a report. No call."""

    PREPARATION_FAILURE = "PREPARATION_FAILURE"
    """The company could not be refreshed or scored. Aborted before any spend."""

    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    """The prompt exceeded the input ceiling. Aborted before any spend, and the
    evidence was left intact rather than trimmed to fit."""

    COST_REFUSED = "COST_REFUSED"
    """The estimated cost exceeded the per-company ceiling. Nothing was sent."""

    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    """The model could not be reached or did not answer usably. Nothing was
    persisted — a fabricated report is worse than a missing one."""


@dataclass(frozen=True, slots=True)
class DeepResearchRun:
    """What one deep research run did, and what it cost.

    Attributes:
        ticker: The company.
        status: How it ended.
        detail: A human-readable account, carrying the reason on any failure.
        report: The validated report, when one exists. None on every failure and
            on nothing else.
        stored_id: Primary key of the persisted row, when one was written.
        preparation: What the deterministic refresh did.
        collection: What external collection did, when it ran. None when the
            evidence was reused rather than searched for.
        external_state: Whether the external evidence was `FRESH`, `REUSED` or
            `DEGRADED`.
        prompt_chars: Rendered prompt size, before tokenisation.
        input_tokens: Measured prompt tokens. Counted before the call, so it is
            populated even when the run refused to make one.
        output_tokens: Generated tokens. Zero unless a generation happened.
        estimated_cost_usd: List-price estimate, erring high. Zero on a cache
            hit and on every refusal.
        provider_calls: Generations performed. Zero on a cache hit, by design.
        degraded: Anything that worked less well than it should have — a failed
            external collection, a stage that could not refresh.
    """

    ticker: str
    status: RunStatus
    detail: str = ""
    report: DeepResearchReport | None = None
    stored_id: int | None = None
    preparation: PreparationResult | None = None
    collection: CollectionReport | None = None
    external_state: str = EXTERNAL_FRESH
    prompt_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    provider_calls: int = 0
    degraded: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        """Whether a report is available, whether freshly written or cached."""
        return self.report is not None

    def summary(self) -> str:
        """Return a one-line human-readable summary."""
        return (
            f"ticker={self.ticker} status={self.status.value} "
            f"calls={self.provider_calls} in={self.input_tokens} out={self.output_tokens} "
            f"cost=${self.estimated_cost_usd:.4f}"
        )


def _cache_key(brief: DeepResearchBrief, settings: Settings) -> dict[str, str]:
    """Return the four values that decide whether a report already exists."""
    return {
        "deterministic_fingerprint": brief.deterministic_fingerprint(),
        "evidence_fingerprint": brief.evidence_fingerprint(),
        "prompt_version": _prompt_version(settings),
    }


def _prompt_version(settings: Settings) -> str:
    """Return the cache-visible prompt identity.

    The model is folded into the prompt version rather than kept as a separate
    column, because a report is a product of both: the same instructions answered
    by a different model is a different reading, and serving one for the other
    from cache would be quietly wrong.
    """
    return f"{CURRENT_DEEP_PROMPT_VERSION}/{settings.deep_research_model}"


def run_deep_research(
    session: Session,
    settings: Settings,
    *,
    synthesis: DeepResearchProvider,
    market_data: MarketDataProvider | None = None,
    fundamentals: FundamentalsProvider | None = None,
    external: ExternalResearchProvider | None = None,
    ticker: str,
    refresh: bool = True,
    refresh_external: bool = False,
) -> DeepResearchRun:
    """Research one company end to end, spending nothing it does not have to.

    Args:
        session: Open database session. The caller commits.
        settings: Supplies every ceiling and provider setting.
        synthesis: The model that writes the draft.
        market_data: Price source. Required only when `refresh` is true.
        fundamentals: Statement, filing and filing-text source. Required only
            when `refresh` is true.
        external: Current-evidence search. None skips collection entirely, which
            is what `--no-external` does and what an unconfigured vendor means.
        ticker: The single company to research.
        refresh: Run deterministic preparation first. False reuses whatever is
            stored, which is what makes a cache test cheap.
        refresh_external: Search again even when a recent collection could be
            reused. The deliberate "get me current news" request; without it a
            repeated run inside the window costs nothing.

    Returns:
        What happened, what it cost, and the report when there is one. Never
        raises for an expected failure — the status carries it.

    Raises:
        ValueError: If `refresh` is true without the providers a refresh needs.
            A programming error, not a runtime condition, so it is not a status.
    """
    symbol = ticker.strip().upper()
    degraded: list[str] = []
    preparation: PreparationResult | None = None

    if refresh:
        if market_data is None or fundamentals is None:
            raise ValueError("refreshing needs a market data and a fundamentals provider")
        try:
            preparation = prepare_company(session, settings, market_data, fundamentals, symbol)
        except PreparationError as error:
            return DeepResearchRun(
                ticker=symbol, status=RunStatus.PREPARATION_FAILURE, detail=str(error)
            )
        degraded.extend(outcome.line() for outcome in preparation.degraded)
        symbol = preparation.ticker

    brief = assemble_deep_brief(session, settings, symbol, preparation=preparation)
    if brief is None:
        return DeepResearchRun(
            ticker=symbol,
            status=RunStatus.PREPARATION_FAILURE,
            detail=(
                f"{symbol} has no brief to research: it must be stored and scored "
                f"under the current version"
            ),
            preparation=preparation,
        )

    company_id = _company_id(session, symbol)
    repository = DeepResearchReportRepository(session)

    collection: CollectionReport | None = None
    evidence: tuple[ExternalEvidence, ...] = ()
    external_state = EXTERNAL_FRESH
    collected_at: datetime | None = None

    if external is not None:
        reused = (
            None
            if refresh_external
            else repository.latest_collection(
                company_id,
                deterministic_fingerprint=brief.deterministic_fingerprint(),
                since=datetime.now(UTC)
                - timedelta(hours=settings.deep_research_external_cache_hours),
            )
        )
        if reused is not None:
            evidence = _stored_evidence(reused)
            external_state = EXTERNAL_REUSED
            collected_at = reused.external_collected_at
            log.info(
                "external evidence reused",
                ticker=symbol,
                items=len(evidence),
                collected_at=str(collected_at),
            )
        else:
            collection = collect_external_evidence(external, settings, brief)
            evidence = collection.evidence
            collected_at = datetime.now(UTC)
            if collection.failures:
                external_state = EXTERNAL_DEGRADED
                degraded.extend(f"external search: {failure}" for failure in collection.failures)

        brief = assemble_deep_brief(
            session, settings, symbol, preparation=preparation, external=evidence
        )
        if brief is None:  # pragma: no cover — it assembled a moment ago
            return DeepResearchRun(
                ticker=symbol,
                status=RunStatus.PREPARATION_FAILURE,
                detail="the brief could not be reassembled with external evidence",
                preparation=preparation,
            )

    system = build_deep_system_prompt()
    rendered = render_deep_brief(brief)
    prompt_chars = len(system) + len(rendered)

    cached = repository.find_cached(company_id, **_cache_key(brief, settings))
    if cached is not None:
        log.info("deep research cache hit", ticker=symbol, report_id=cached.id)
        return DeepResearchRun(
            ticker=symbol,
            status=RunStatus.CACHE_HIT,
            detail=f"an identical report already exists (id {cached.id})",
            report=_rehydrate(cached),
            stored_id=cached.id,
            preparation=preparation,
            collection=collection,
            external_state=external_state,
            prompt_chars=prompt_chars,
            degraded=tuple(degraded),
        )

    try:
        input_tokens = synthesis.count_input_tokens(system=system, brief=rendered)
    except ProviderError as error:
        return DeepResearchRun(
            ticker=symbol,
            status=RunStatus.PROVIDER_FAILURE,
            detail=f"the prompt could not be measured: {error}",
            preparation=preparation,
            collection=collection,
            external_state=external_state,
            prompt_chars=prompt_chars,
            degraded=tuple(degraded),
        )

    if input_tokens > settings.deep_research_max_input_tokens:
        return DeepResearchRun(
            ticker=symbol,
            status=RunStatus.INPUT_TOO_LARGE,
            detail=(
                f"the prompt measured {input_tokens} tokens against a ceiling of "
                f"{settings.deep_research_max_input_tokens}; nothing was sent and no "
                f"evidence was dropped to fit"
            ),
            preparation=preparation,
            collection=collection,
            external_state=external_state,
            prompt_chars=prompt_chars,
            input_tokens=input_tokens,
            degraded=tuple(degraded),
        )

    reserved = _reserve_cost(settings, input_tokens)
    if reserved > settings.deep_research_max_cost_usd:
        return DeepResearchRun(
            ticker=symbol,
            status=RunStatus.COST_REFUSED,
            detail=(
                f"the worst-case cost of this run is about ${reserved:.2f}, above the "
                f"${settings.deep_research_max_cost_usd:.2f} ceiling"
            ),
            preparation=preparation,
            collection=collection,
            external_state=external_state,
            prompt_chars=prompt_chars,
            input_tokens=input_tokens,
            degraded=tuple(degraded),
        )

    try:
        completion = synthesis.generate_report(system=system, brief=rendered)
    except ProviderError as error:
        # Nothing is written. A failed generation leaves no trace beyond this
        # record, because the alternative is a stored report nobody wrote.
        log.warning("deep research generation failed", ticker=symbol, error=str(error)[:200])
        return DeepResearchRun(
            ticker=symbol,
            status=RunStatus.PROVIDER_FAILURE,
            detail=str(error),
            preparation=preparation,
            collection=collection,
            external_state=external_state,
            prompt_chars=prompt_chars,
            input_tokens=input_tokens,
            provider_calls=1,
            degraded=tuple(degraded),
        )

    report = validate_deep_report(
        completion.draft,
        brief,
        prompt_version=_prompt_version(settings),
        model_id=completion.model_id,
        generated_at=datetime.now(UTC),
    )
    stored = repository.save(
        company_id,
        report,
        external_state=external_state,
        external_collected_at=collected_at,
        collected_external=[item.model_dump(mode="json") for item in evidence],
    )
    session.flush()

    if completion.truncated:
        degraded.append("the answer was cut off at the output token ceiling")

    run = DeepResearchRun(
        ticker=symbol,
        status=(RunStatus.COMPLETE if not report.issues else RunStatus.VALIDATION_PARTIAL),
        detail=f"{len(report.issues)} validation issue(s)",
        report=report,
        stored_id=stored.id,
        preparation=preparation,
        collection=collection,
        external_state=external_state,
        prompt_chars=prompt_chars,
        input_tokens=completion.input_tokens or input_tokens,
        output_tokens=completion.output_tokens,
        estimated_cost_usd=completion.cost_usd(),
        provider_calls=1,
        degraded=tuple(degraded),
    )
    log.info("deep research complete", summary=run.summary())
    return run


def _reserve_cost(settings: Settings, input_tokens: int) -> float:
    """Return the worst case this run could cost, before it is allowed to start.

    Assumes the output ceiling is reached, because the guard exists for the run
    that goes long rather than the one that behaves. List prices, so a
    promotional rate expiring cannot let a run past the ceiling.
    """
    from api_clients.research import estimate_cost_usd

    return estimate_cost_usd(
        settings.deep_research_model,
        input_tokens=input_tokens,
        output_tokens=settings.deep_research_max_output_tokens,
    )


def _company_id(session: Session, ticker: str) -> int:
    """Return the stored company's primary key.

    Raises:
        PreparationError: If the company is not stored, which cannot happen after
            a brief was assembled for it.
    """
    from data_access import CompanyRepository

    company = CompanyRepository(session).get_by_ticker(ticker)
    if company is None:  # pragma: no cover — a brief was just built from this row
        raise PreparationError(f"{ticker} is not stored")
    return company.id


def _rehydrate(row: StoredDeepResearchReport) -> DeepResearchReport | None:
    """Read a stored report back, or None when it can no longer be parsed.

    A row written under an older contract may not validate against today's
    models. That is a cache miss in effect, not a crash: the caller sees no
    report and regenerates.
    """
    from pydantic import ValidationError

    from deep_research import DeepResearchReport as _Report

    try:
        return _Report.model_validate(row.validated_report_json)
    except ValidationError:
        log.warning("stored deep report could not be read", report_id=row.id)
        return None


def _stored_evidence(row: StoredDeepResearchReport) -> tuple[ExternalEvidence, ...]:
    """Read a stored collection back into evidence, or nothing when it will not parse.

    A row written under an older contract may no longer validate. That is a cache
    miss in effect: the caller collects again rather than proceeding on a
    half-read set.
    """
    from pydantic import ValidationError

    raw = row.collected_external_json or []
    try:
        return tuple(ExternalEvidence.model_validate(item) for item in raw)
    except ValidationError:
        log.warning("stored external evidence could not be read", report_id=row.id)
        return ()
