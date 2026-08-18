"""One company, end to end: evidence in, validated report stored.

The order is the whole point, and it is not negotiable:

    ResearchBrief → rendered prompt → DraftReport → validate_report → persist

Nothing skips a step. In particular there is no path from a provider's answer to
the database that does not pass through validation — `save_report` takes a
`ResearchReport`, and only `validate_report` and `failed_report` build one, so an
unvalidated draft cannot be stored even by mistake.

Two things this module refuses to do.

**A transient failure does not fail a run.** A quota, a timeout, an outage, an
answer that would not parse — each becomes a `FAILED` report and a returned
outcome. Scanning, scoring and ranking never wait on a model, and a vendor having
a bad afternoon costs one company its report.

**A permanent failure stops it immediately.** A rejected schema or an
unacceptable parameter is a defect in the request we built, identical for every
company. Recording it nineteen times would spend nineteen calls to learn one
fact and fill the table with rows blaming the model.

**It does not pay twice.** A brief whose fingerprint, score version and prompt
version already have a usable report is reused without a provider call. That is
the difference between re-reading the same evidence for free and re-buying it.

**It stops before it overspends.** A batch tracks its own estimated cost and
refuses to start a company whose request could carry the run past the configured
budget. The remaining companies are skipped, not failed — nothing was asked and
nothing went wrong; the run simply ran out of allowance.

**Evidence can be fetched first, but never during.** `prepare_filing_evidence`
runs the two existing SEC passes for one company before a brief is assembled;
assembly itself stays zero-network. Preparation is a separate step rather than
part of `research_company` because a batch must not turn into a crawl.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from api_clients import ProviderError, ProviderInvalidRequestError, estimate_cost_usd
from domain import CURRENT_SCORE_VERSION
from research import (
    CURRENT_PROMPT_VERSION,
    ResearchStatus,
    SelectionReason,
    build_system_prompt,
    failed_report,
    render_brief,
    validate_report,
)
from stock_screener.research.brief import assemble_brief
from stock_screener.research.store import find_cached_report, save_report
from stock_screener.scanning.ingestion import update_filing_text, update_filings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from api_clients import FundamentalsProvider, ResearchProvider
    from research import ResearchBrief, ResearchReport
    from stock_screener.config import Settings
    from stock_screener.scanning.ingestion import IngestionReport

log = structlog.get_logger(__name__)

RESERVED_PROMPT_TOKENS = 24_000
"""Prompt tokens assumed for a company that has not been asked about yet.

The budget check has to price a request before the brief exists, so it prices a
pessimistic one: roughly three times the largest brief observed, which leaves
room for a company with far more filings and history than any seen so far. Too
low and the guard lets a run cross its ceiling; too high only means the last
company of an almost-exhausted run waits for the next one.
"""


@dataclass(frozen=True, slots=True)
class FilingPreparation:
    """What the two SEC passes did for one company.

    Attributes:
        ticker: The symbol prepared, normalised as the ingestion passes saw it.
        index: Counts from the filing-index pass.
        text: Counts from the filing-text pass.
    """

    ticker: str
    index: IngestionReport
    text: IngestionReport

    @property
    def failed(self) -> bool:
        """Whether either pass could not complete.

        A caller that asked for preparation and did not get it must not fall
        through to a paid call: the report it would buy is the evidence-poor one
        preparation existed to prevent.
        """
        return bool(self.index.failed or self.text.failed)

    def summary(self) -> str:
        """Return a one-line human-readable summary of both passes."""
        return f"filings: {self.index.summary()}\nfiling text: {self.text.summary()}"


def prepare_filing_evidence(
    session: Session, provider: FundamentalsProvider, ticker: str
) -> FilingPreparation:
    """Fetch one company's SEC filing index and the text a brief may quote.

    The two existing passes, run for a single symbol and nothing else. Both are
    incremental: the index is upserted because a company files between reporting
    periods, and the text pass skips filings it has already read, so preparing a
    company twice costs one cheap request and no document reads.

    This is the only place a network call happens on the research path. Brief
    assembly stays zero-network by construction — it reads what this stored.

    Args:
        session: Open database session. The caller commits.
        provider: Source of the filing index and documents.
        ticker: The single symbol to prepare.

    Returns:
        Both passes' counts, and whether either failed.
    """
    symbol = ticker.strip().upper()
    index = update_filings(session, provider, tickers=[symbol])
    text = update_filing_text(session, provider, tickers=[symbol])
    log.info(
        "filing evidence prepared",
        ticker=symbol,
        index=index.summary(),
        text=text.summary(),
    )
    return FilingPreparation(ticker=symbol, index=index, text=text)


class ResearchSkip(StrEnum):
    """Why a candidate was passed over without being researched.

    Neither value is a failure. A skipped company has no report, no `FAILED` row
    and no provider call against its name — the run declined to ask, which is a
    different thing from asking and being let down.
    """

    NO_BRIEF = "NO_BRIEF"
    """No brief could be assembled: unknown company, or never scored."""

    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    """The run's remaining allowance could not cover another request."""


@dataclass(frozen=True, slots=True)
class ResearchOutcome:
    """What one company's research run produced.

    Attributes:
        ticker: The company.
        report: The validated report. Present even when the provider failed —
            a `FAILED` report is still a record of the attempt.
        brief: The evidence the report was written from, or None when no brief
            could be assembled and nothing was attempted.
        cached: Whether the report was reused rather than generated. A cache hit
            costs nothing and is the expected outcome of a re-run.
        input_tokens: Prompt tokens billed. Zero on a cache hit.
        output_tokens: Generated tokens billed, reasoning included.
        cost_usd: Estimated cost of this company's request. Zero on a cache hit,
            on a skip, and on a failure that never reached the provider — a
            failure mid-request may have been billed, and that is not knowable
            from here.
        model_id: Which model produced it, or the configured one on failure.
        skipped: Why nothing was attempted, when nothing was.
    """

    ticker: str
    report: ResearchReport | None
    brief: ResearchBrief | None = None
    cached: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model_id: str = ""
    skipped: ResearchSkip | None = None

    @property
    def status(self) -> str:
        """The report's status, or why the company was skipped."""
        if self.report is not None:
            return self.report.status.value
        return self.skipped.value if self.skipped is not None else ResearchSkip.NO_BRIEF.value


def research_company(
    session: Session,
    settings: Settings,
    provider: ResearchProvider,
    ticker: str,
    *,
    selection: SelectionReason = SelectionReason.TOP_RANKED,
    score_version: str = CURRENT_SCORE_VERSION,
    prompt_version: str = CURRENT_PROMPT_VERSION,
    force: bool = False,
    persist: bool = True,
) -> ResearchOutcome:
    """Research one company, reusing a stored report when nothing has changed.

    Args:
        session: Open database session. The caller commits.
        settings: Supplies the model identifier recorded on a failed report.
        provider: The model to ask.
        ticker: The company to research.
        selection: Why the company qualified, recorded on the brief.
        score_version: The formula version whose score is being explained.
        prompt_version: The prompt asking for the report. Part of the cache key.
        force: Ignore a usable stored report and generate a new one.
        persist: Write the result. False generates and validates without
            storing, which is what makes a prompt change inspectable before it
            enters the record.

    Returns:
        The outcome. `report` is None only when no brief could be assembled —
        an unknown or never-scored company — in which case nothing was called
        and nothing was stored.
    """
    brief = assemble_brief(
        session, settings, ticker, selection=selection, score_version=score_version
    )
    if brief is None:
        log.warning("research skipped: no brief", ticker=ticker)
        return ResearchOutcome(ticker=ticker.upper(), report=None, skipped=ResearchSkip.NO_BRIEF)

    if not force:
        cached = find_cached_report(session, brief, prompt_version=prompt_version)
        if cached is not None:
            log.info("research reused", ticker=brief.ticker, status=cached.status.value)
            return ResearchOutcome(
                ticker=brief.ticker,
                report=cached,
                brief=brief,
                cached=True,
                model_id=cached.model_id,
            )

    outcome = _generate(provider, brief, settings, prompt_version)
    if persist and outcome.report is not None:
        save_report(session, outcome.report)
    return outcome


def _generate(
    provider: ResearchProvider,
    brief: ResearchBrief,
    settings: Settings,
    prompt_version: str,
) -> ResearchOutcome:
    """Ask the provider, validate what comes back, and never raise.

    A *transient* provider failure is caught here rather than propagated because
    the caller is usually a loop over candidates: one company's outage must not
    end the others' reports. A *permanent* one is re-raised, because continuing
    would mean spending a request per remaining company to be told the same
    thing.

    Raises:
        ProviderInvalidRequestError: The request itself was rejected. Aborts the
            run rather than being recorded.
    """
    generated_at = datetime.now(UTC)

    try:
        completion = provider.generate_report(
            system=build_system_prompt(), brief=render_brief(brief)
        )
    except ProviderInvalidRequestError:
        # Permanent: the request we built is unacceptable, so the next company's
        # would be too. Let it escape — a run that recorded this as a FAILED
        # report per ticker would blame the model for our own defect and spend a
        # call proving it each time.
        log.error("research aborted: the provider rejected the request", ticker=brief.ticker)
        raise
    except ProviderError as exc:
        log.warning("research failed", ticker=brief.ticker, error=type(exc).__name__)
        return ResearchOutcome(
            ticker=brief.ticker,
            report=failed_report(
                brief,
                prompt_version=prompt_version,
                model_id=settings.research_model,
                generated_at=generated_at,
                detail=f"{type(exc).__name__}: {exc}",
            ),
            brief=brief,
            model_id=settings.research_model,
        )

    report = validate_report(
        completion.draft,
        brief,
        prompt_version=prompt_version,
        model_id=completion.model_id,
        generated_at=generated_at,
    )
    log.info(
        "research generated",
        ticker=brief.ticker,
        status=report.status.value,
        confidence=report.confidence.level.value,
        issues=len(report.issues),
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
    )
    return ResearchOutcome(
        ticker=brief.ticker,
        report=report,
        brief=brief,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        cost_usd=estimate_cost_usd(
            completion.model_id,
            completion.input_tokens,
            completion.output_tokens,
            completion.cache_read_tokens,
        ),
        model_id=completion.model_id,
    )


def research_candidates(
    session: Session,
    settings: Settings,
    provider: ResearchProvider,
    candidates: Sequence[tuple[str, SelectionReason]],
    *,
    score_version: str = CURRENT_SCORE_VERSION,
    prompt_version: str = CURRENT_PROMPT_VERSION,
    force: bool = False,
    budget_usd: float | None = None,
) -> list[ResearchOutcome]:
    """Research several companies, one after another, within a spend limit.

    Sequential on purpose: a research run is a handful of companies against a
    metered provider, and concurrency here buys a few seconds at the cost of
    making rate limits arrive all at once, interleaving the logs, and making
    spend depend on how many requests happened to be in flight when the run was
    stopped.

    The budget is checked *before* each company, against a deliberately
    pessimistic price for the request about to be made. A run therefore stops
    under its ceiling rather than discovering it has crossed it, and the
    companies never asked about come back skipped rather than failed.

    Args:
        session: Open database session. The caller commits.
        settings: Supplies the configured model identifier and output ceiling.
        provider: The model to ask.
        candidates: Ticker and selection reason per company.
        score_version: The formula version being explained.
        prompt_version: The prompt asking for the reports.
        force: Ignore stored reports and regenerate.
        budget_usd: Estimated spend allowed for this run. None removes the
            guard, which is what the mock provider and the tests want.

    Returns:
        One outcome per company, in order. A company whose provider call failed
        transiently is present with a `FAILED` report, not absent; a company the
        budget stopped is present with no report and `BUDGET_EXHAUSTED`.

    Raises:
        ProviderInvalidRequestError: The provider rejected the request itself —
            a schema, a parameter, an account that cannot serve the model. The
            run stops at the company that hit it; the outcomes already collected
            are lost, because none of them would have been written anyway.
    """
    outcomes: list[ResearchOutcome] = []
    spent = 0.0
    reserve = estimate_cost_usd(
        settings.research_model, RESERVED_PROMPT_TOKENS, settings.research_max_output_tokens
    )

    for index, (ticker, selection) in enumerate(candidates):
        if budget_usd is not None and spent + reserve > budget_usd:
            log.warning(
                "research stopped: run budget reached",
                spent_usd=round(spent, 4),
                budget_usd=budget_usd,
                next_request_usd=round(reserve, 4),
                skipped=len(candidates) - index,
            )
            outcomes.extend(
                ResearchOutcome(
                    ticker=remaining.upper(),
                    report=None,
                    skipped=ResearchSkip.BUDGET_EXHAUSTED,
                )
                for remaining, _ in candidates[index:]
            )
            break

        outcome = research_company(
            session,
            settings,
            provider,
            ticker,
            selection=selection,
            score_version=score_version,
            prompt_version=prompt_version,
            force=force,
        )
        spent += outcome.cost_usd
        outcomes.append(outcome)

    log.info(
        "research run complete",
        companies=len(outcomes),
        generated=sum(1 for o in outcomes if o.report is not None and not o.cached),
        reused=sum(1 for o in outcomes if o.cached),
        failed=sum(
            1 for o in outcomes if o.report is not None and o.report.status is ResearchStatus.FAILED
        ),
        skipped=sum(1 for o in outcomes if o.report is None),
        input_tokens=sum(o.input_tokens for o in outcomes),
        output_tokens=sum(o.output_tokens for o in outcomes),
        estimated_cost_usd=round(spent, 4),
    )
    return outcomes
