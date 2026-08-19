"""Command-line interface.

The Phase 1 ingestion and scan commands, `run-scan` which chains them, the Phase
2 scoring and ranking commands, and a `research` group for inspecting what a
Phase 3 run would do. Each is thin: build the dependencies, call into `scanning`,
`scoring` or `research`, print the outcome. Any logic worth testing lives in a
module, not in a command body.

Commands write their results to stdout with `typer.echo` rather than a logger.
The table and the summary are the program's *output*, not diagnostics — they
belong on stdout whatever the log level, and they should stay legible when logs
are being emitted as JSON.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import structlog
import typer

from api_clients import ProviderInvalidRequestError
from data_access import build_session_factory, create_engine_from_url, session_scope
from research import build_system_prompt, render_brief
from stock_screener.config import Settings, get_settings
from stock_screener.deep_research import (
    PreparationError,
    assemble_deep_brief,
    collect_external_evidence,
    format_collection,
    format_preparation,
    format_run,
    prepare_company,
    run_deep_research,
)
from stock_screener.logging import configure_logging
from stock_screener.providers import (
    ConfigurationError,
    build_deep_research_provider,
    build_external_research_provider,
    build_fundamentals_provider,
    build_market_data_provider,
    build_profile_provider,
    build_research_provider,
)
from stock_screener.research import (
    MAX_CANDIDATES,
    assemble_brief,
    format_brief,
    format_candidates,
    format_research_run,
    prepare_filing_evidence,
    research_candidates,
    research_company,
    select_candidates,
)
from stock_screener.scanning import (
    format_table,
    scan_market,
    update_benchmark,
    update_filing_text,
    update_filings,
    update_fundamentals,
    update_market_data,
    update_universe,
    write_csv,
)
from stock_screener.scoring import (
    DEFAULT_RANKING_LIMIT,
    enrich_candidates,
    format_explanation,
    format_rankings_table,
    great_company_wrong_price,
    hidden_gems,
    improving_fast,
    latest_score,
    score_market,
    top_opportunities,
    write_rankings_csv,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from sqlalchemy.orm import Session, sessionmaker

    from stock_screener.scoring import RankingRow

log = structlog.get_logger(__name__)

app = typer.Typer(
    name="stock-screener",
    help="Compounder Radar — scan U.S. equities for research candidates.",
    no_args_is_help=True,
    add_completion=False,
)

TickerOption = Annotated[
    list[str] | None,
    typer.Option("--ticker", "-t", help="Restrict to these symbols. Repeatable."),
]

CompanyLimitOption = Annotated[
    int | None,
    typer.Option(
        "--limit",
        "-n",
        min=1,
        help="Process at most this many companies, in ticker order. "
        "Bounds a first run against a metered provider.",
    ),
]

RankingLimitOption = Annotated[
    int,
    typer.Option("--limit", "-n", min=1, help="Show at most this many companies."),
]

OutputOption = Annotated[
    Path | None,
    typer.Option("--output", "-o", help="Write the full result to this CSV file."),
]


def _bootstrap() -> Settings:
    """Load settings and configure logging once, for any command."""
    settings = get_settings()
    configure_logging(settings)
    return settings


@contextmanager
def _database(settings: Settings) -> Iterator[sessionmaker[Session]]:
    """Yield a session factory, disposing the engine when the command ends.

    A command that returns without disposing leaves pooled connections to be
    closed by the garbage collector, which on SQLite surfaces as a
    `ResourceWarning` and on PostgreSQL as a connection the server holds open.

    Args:
        settings: Supplies the database URL.

    Yields:
        A session factory bound to a fresh engine.
    """
    engine = create_engine_from_url(settings.database_url)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


@app.command("update-universe")
def update_universe_command() -> None:
    """Refresh the company list from the market-data provider."""
    settings = _bootstrap()
    provider = build_market_data_provider(settings)
    with _database(settings) as factory, session_scope(factory) as session:
        report = update_universe(session, provider)

    typer.echo(f"universe: {report.summary()}")


@app.command("update-market")
def update_market_command(tickers: TickerOption = None, limit: CompanyLimitOption = None) -> None:
    """Refresh daily price history for stored companies."""
    settings = _bootstrap()
    provider = build_market_data_provider(settings)
    with _database(settings) as factory, session_scope(factory) as session:
        report = update_market_data(session, provider, settings, tickers=tickers, limit=limit)

    typer.echo(f"market data: {report.summary()}")


@app.command("update-benchmark")
def update_benchmark_command() -> None:
    """Refresh the broad-market benchmark's price history.

    Relative strength is a company's return less this series', so scoring
    without it leaves every company short of the data it needs for market
    confirmation. Run it alongside `update-market`.
    """
    settings = _bootstrap()
    provider = build_market_data_provider(settings)
    with _database(settings) as factory, session_scope(factory) as session:
        report = update_benchmark(session, provider, settings)

    typer.echo(f"benchmark {settings.benchmark_symbol}: {report.summary()}")


@app.command("update-fundamentals")
def update_fundamentals_command(
    tickers: TickerOption = None,
    limit: CompanyLimitOption = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Re-store every period even if the provider has nothing newer. "
            "Needed after changing how a provider is parsed.",
        ),
    ] = False,
) -> None:
    """Refresh company profiles and quarterly financial statements.

    A metered provider charges several requests per company, so `--limit` is the
    safe way to make a first pass: ingest fifty, inspect them, then widen.

    The incremental skip compares reporting dates, so it cannot tell that the
    adapter itself changed. Use `--force` after fixing a normalisation bug, or
    the stored values stay as they were.
    """
    settings = _bootstrap()
    provider = build_fundamentals_provider(settings)
    with _database(settings) as factory, session_scope(factory) as session:
        report = update_fundamentals(
            session, provider, settings, tickers=tickers, limit=limit, force=force
        )

    typer.echo(f"fundamentals: {report.summary()}")


@app.command("scan")
def scan_command(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the full result to this CSV file."),
    ] = None,
    include_ineligible: Annotated[
        bool,
        typer.Option("--all", help="Show excluded companies and their reasons too."),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="Show at most this many rows."),
    ] = None,
    tickers: TickerOption = None,
) -> None:
    """Screen stored data and print the eligible companies.

    Calculates no score — `score` does that, over the same stored data. The CSV
    export always contains every company scanned, including exclusions and their
    reasons, because the manual review the project depends on needs to see what
    was thrown away.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        result = scan_market(
            session,
            settings.eligibility_thresholds,
            tickers=tickers,
            bar_volume_basis=settings.bar_volume_basis,
        )

    shown = result.rows if include_ineligible else result.eligible
    if limit is not None:
        shown = shown[:limit]

    typer.echo(format_table(shown))
    typer.echo("")
    typer.echo(
        f"Processed: {result.processed}   "
        f"Eligible: {len(result.eligible)}   "
        f"Excluded: {result.processed - len(result.eligible)}"
    )

    if output is not None:
        written = write_csv(result.rows, output)
        typer.echo(f"Wrote {written} rows to {output}")


@app.command("run-scan")
def run_scan_command(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the full result to this CSV file."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="Show at most this many rows."),
    ] = None,
) -> None:
    """Run the whole pipeline: universe, prices, fundamentals, then scan.

    Safe to run repeatedly. The unique constraints on financial periods and price
    sessions mean a second run updates rows rather than duplicating them.
    """
    settings = _bootstrap()
    market_data = build_market_data_provider(settings)
    fundamentals = build_fundamentals_provider(settings)
    # Four sequential transactions on one engine: each stage commits before the
    # next begins, so a failure in fundamentals does not roll back the prices.
    with _database(settings) as factory:
        with session_scope(factory) as session:
            universe_report = update_universe(session, market_data)
            typer.echo(f"universe: {universe_report.summary()}")

        with session_scope(factory) as session:
            price_report = update_market_data(session, market_data, settings)
            typer.echo(f"market data: {price_report.summary()}")

        with session_scope(factory) as session:
            fundamentals_report = update_fundamentals(session, fundamentals, settings)
            typer.echo(f"fundamentals: {fundamentals_report.summary()}")

        with session_scope(factory) as session:
            result = scan_market(
                session,
                settings.eligibility_thresholds,
                bar_volume_basis=settings.bar_volume_basis,
            )

    shown = result.eligible[:limit] if limit is not None else result.eligible
    typer.echo("")
    typer.echo(format_table(shown))
    typer.echo("")
    typer.echo(
        f"Processed: {result.processed}   "
        f"Eligible: {len(result.eligible)}   "
        f"Failed: {universe_report.failed + price_report.failed + fundamentals_report.failed}"
    )

    if output is not None:
        written = write_csv(result.rows, output)
        typer.echo(f"Wrote {written} rows to {output}")


@app.command("score")
def score_command(
    tickers: TickerOption = None,
    limit: CompanyLimitOption = None,
    preview: Annotated[
        int,
        typer.Option("--preview", min=0, help="Print this many of the top-scoring companies."),
    ] = 10,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Calculate without saving. Use it to inspect a formula change "
            "before it enters the score history.",
        ),
    ] = False,
) -> None:
    """Score the stored market and save today's snapshots.

    Reads only what is already in the database, so it is safe to re-run: the
    day's rows are replaced rather than appended, and no provider is called.

    A company that cannot be scored still gets a row carrying the reason —
    ineligible, unsupported sector, or not enough data — which is what makes
    "why is it not in the ranking?" answerable afterwards.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        run_result = score_market(
            session, settings, tickers=tickers, limit=limit, persist=not dry_run
        )
        top = top_opportunities(session, limit=preview) if preview and not dry_run else []

    typer.echo(f"scoring: {run_result.summary()}")
    typer.echo(
        f"benchmark {run_result.benchmark.symbol}: "
        f"6m {_percent(run_result.benchmark.return_6m)}   "
        f"12m {_percent(run_result.benchmark.return_12m)}"
    )
    if top:
        typer.echo("")
        typer.echo(format_rankings_table(top))


@app.command("enrich")
def enrich_command(
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            "-n",
            min=1,
            help="Candidates to enrich. Defaults to FMP_ENRICHMENT_LIMIT.",
        ),
    ] = None,
) -> None:
    """Verify the top candidates against the metered provider, then re-score them.

    The broad scan ranks the whole market on free data. This spends one request
    per top-ranked company to replace a calculated market cap with the vendor's
    and unverified single-exchange volume with consolidated volume, then runs the
    same formula again over those inputs — which is where a company with real but
    thin liquidity finally drops out.

    A quota that runs out stops the pass. Companies already enriched keep their
    verified state; the rest stay preliminary and say so.
    """
    settings = _bootstrap()
    provider = build_profile_provider(settings)
    if provider is None:
        _report_no_enrichment(settings)
        return

    with _database(settings) as factory, session_scope(factory) as session:
        report = enrich_candidates(session, provider, settings, limit=limit)

    typer.echo(f"enrichment: {report.summary()}")
    if report.discrepancies:
        typer.echo(f"market-cap disagreements worth a look: {', '.join(report.discrepancies[:20])}")


@app.command("rankings")
def rankings_command(
    limit: RankingLimitOption = DEFAULT_RANKING_LIMIT,
    min_score: Annotated[
        float | None,
        typer.Option("--min-score", help="Only companies at or above this final score."),
    ] = None,
    output: OutputOption = None,
) -> None:
    """Show Top Opportunities — the main ranking, best first."""
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        rows = top_opportunities(session, limit=limit, min_score=min_score)

    _emit_ranking(rows, output, label="Top Opportunities")


@app.command("hidden-gems")
def hidden_gems_command(
    limit: RankingLimitOption = DEFAULT_RANKING_LIMIT,
    output: OutputOption = None,
) -> None:
    """Show strong, fast-growing companies still small enough to be overlooked."""
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        rows = hidden_gems(session, limit=limit)

    _emit_ranking(rows, output, label="Hidden Gems")


@app.command("wrong-price")
def wrong_price_command(
    limit: RankingLimitOption = DEFAULT_RANKING_LIMIT,
    output: OutputOption = None,
) -> None:
    """Show Great Company, Wrong Price — strong businesses scoring badly on price."""
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        rows = great_company_wrong_price(session, limit=limit)

    _emit_ranking(rows, output, label="Great Company, Wrong Price")


@app.command("improving")
def improving_command(
    limit: RankingLimitOption = DEFAULT_RANKING_LIMIT,
    window: Annotated[
        int,
        typer.Option("--window", min=1, help="Days to compare back over."),
    ] = 30,
    min_change: Annotated[
        float,
        typer.Option("--min-change", help="Minimum improvement, in score points."),
    ] = 0.0,
    output: OutputOption = None,
) -> None:
    """Show the companies whose score has risen most.

    Needs score history: a company with no earlier snapshot is absent rather
    than shown as unchanged, so this view fills in over the first month of
    nightly runs.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        rows = improving_fast(session, limit=limit, window_days=window, min_change=min_change)

    _emit_ranking(rows, output, label=f"Improving Fast ({window}d)")


@app.command("explain")
def explain_command(
    ticker: Annotated[str, typer.Argument(help="The symbol to explain.")],
) -> None:
    """Show one company's latest score, metric by metric."""
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        detail = latest_score(session, ticker)

    if detail is None:
        typer.echo(f"No score stored for {ticker.upper()}. Run `score` first.")
        raise typer.Exit(code=1)

    typer.echo(format_explanation(detail))


research_app = typer.Typer(
    name="research",
    help="Inspect what an AI research run would do. No model is called.",
    no_args_is_help=True,
)
app.add_typer(research_app)


@research_app.command("candidates")
def research_candidates_command(
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", min=1, help="Cap the selection across every source."),
    ] = MAX_CANDIDATES,
) -> None:
    """List the companies a research run would spend a model call on."""
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        candidates = select_candidates(session, limit=limit)

    typer.echo(format_candidates(candidates))


@research_app.command("brief")
def research_brief_command(
    ticker: Annotated[str, typer.Argument(help="The symbol to assemble a brief for.")],
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the whole brief as JSON, exactly as a model sees it."),
    ] = False,
) -> None:
    """Assemble one company's research brief and show what it contains.

    Reads the database only. No provider is called, and no model is called.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        brief = assemble_brief(session, settings, ticker)

    if brief is None:
        typer.echo(
            f"No brief for {ticker.upper()}. The company must be stored and scored "
            "under the current version — run `score` first."
        )
        raise typer.Exit(code=1)

    typer.echo(brief.model_dump_json(indent=2) if as_json else format_brief(brief))


@research_app.command("update-filings")
def research_update_filings_command(
    tickers: TickerOption = None,
    limit: CompanyLimitOption = None,
    candidates: Annotated[
        bool,
        typer.Option(
            "--candidates",
            help="Restrict the pass to the current research candidates. "
            "A full-universe refresh costs thousands of requests for filings "
            "no brief will cite.",
        ),
    ] = False,
) -> None:
    """Refresh the stored SEC filing index used for brief citations."""
    settings = _bootstrap()
    provider = build_fundamentals_provider(settings)
    with _database(settings) as factory, session_scope(factory) as session:
        selected = list(tickers) if tickers else None
        if candidates:
            selected = [candidate.ticker for candidate in select_candidates(session)]
            typer.echo(f"restricting to {len(selected)} research candidate(s)")
        report = update_filings(session, provider, tickers=selected, limit=limit)

    typer.echo(f"filings: {report.summary()}")


@research_app.command("update-filing-text")
def research_update_filing_text_command(
    tickers: TickerOption = None,
    limit: CompanyLimitOption = None,
    candidates: Annotated[
        bool,
        typer.Option(
            "--candidates",
            help="Restrict the pass to the current research candidates.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-read filings whose text is already stored."),
    ] = False,
) -> None:
    """Read stored filings and keep the sections a brief may quote.

    One request per filing, so this is driven by an explicit list rather than run
    over the market. Filings already extracted are skipped unless `--force`.
    """
    settings = _bootstrap()
    provider = build_fundamentals_provider(settings)
    with _database(settings) as factory, session_scope(factory) as session:
        selected = list(tickers) if tickers else None
        if candidates:
            selected = [candidate.ticker for candidate in select_candidates(session)]
            typer.echo(f"restricting to {len(selected)} research candidate(s)")
        if selected is None and limit is None:
            typer.echo(
                "refusing to read every company's filings: pass --tickers, --limit "
                "or --candidates. One request per filing adds up."
            )
            raise typer.Exit(code=1)
        report = update_filing_text(session, provider, tickers=selected, limit=limit, force=force)

    typer.echo(f"filing text: {report.summary()}")


@research_app.command("run")
def research_run_command(
    ticker: Annotated[
        str | None,
        typer.Argument(help="The symbol to research. Omitted, researches every candidate."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Ignore a stored report and generate a new one."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the exact prompt and brief that would be sent, and stop. "
            "No model is called and nothing is stored.",
        ),
    ] = False,
    prepare: Annotated[
        bool,
        typer.Option(
            "--prepare",
            help="Fetch this company's SEC filing index and text from EDGAR first, "
            "so filing-dependent sections have evidence to cite. One ticker only. "
            "Nothing is generated if the fetch fails.",
        ),
    ] = False,
) -> None:
    """Generate AI research, validate it, and store what validation accepted.

    Reuses a stored report whenever the evidence, the scoring rules and the
    prompt are all unchanged, so a re-run costs nothing.

    `--prepare` runs the two SEC passes for the named company before the brief is
    assembled. Without it a company whose filings were never ingested is still
    researched, and pays full price for a report whose filing-dependent sections
    can only answer `UNKNOWN`.
    """
    settings = _bootstrap()

    if prepare:
        if ticker is None:
            typer.echo(
                "--prepare needs a ticker: preparing every candidate would crawl "
                "the SEC for filings no brief has asked for yet."
            )
            raise typer.Exit(code=1)
        fundamentals = build_fundamentals_provider(settings)
        with _database(settings) as factory, session_scope(factory) as session:
            preparation = prepare_filing_evidence(session, fundamentals, ticker)
        typer.echo(preparation.summary())
        if preparation.failed:
            # Asked for and not delivered. Generating anyway would buy the
            # evidence-poor report this flag exists to prevent.
            typer.echo(
                f"filing preparation failed for {preparation.ticker} — "
                "no research was generated and nothing was spent."
            )
            raise typer.Exit(code=2)

    if dry_run:
        if ticker is None:
            typer.echo("--dry-run needs a ticker: there is one prompt per company.")
            raise typer.Exit(code=1)
        with _database(settings) as factory, session_scope(factory) as session:
            brief = assemble_brief(session, settings, ticker)
        if brief is None:
            typer.echo(f"No brief for {ticker.upper()}. Run `score` first.")
            raise typer.Exit(code=1)
        typer.echo(build_system_prompt())
        typer.echo("\n" + "=" * 78 + "\n")
        typer.echo(render_brief(brief))
        return

    try:
        provider = build_research_provider(settings)
    except ConfigurationError as exc:
        typer.echo(f"research provider not configured: {exc}")
        raise typer.Exit(code=2) from exc

    try:
        with _database(settings) as factory, session_scope(factory) as session:
            if ticker is not None:
                outcomes = [research_company(session, settings, provider, ticker, force=force)]
            else:
                selected = select_candidates(session)
                outcomes = research_candidates(
                    session,
                    settings,
                    provider,
                    [(candidate.ticker, candidate.selection) for candidate in selected],
                    force=force,
                    budget_usd=settings.research_max_run_cost_usd,
                )
    except ProviderInvalidRequestError as exc:
        typer.echo(f"research aborted — the provider rejected the request:\n  {exc}")
        raise typer.Exit(code=2) from exc

    typer.echo(format_research_run(outcomes))


deep_research_app = typer.Typer(
    name="deep-research",
    help="On-demand deep research. Phase 6B prepares one company; no model is called.",
    no_args_is_help=True,
)
app.add_typer(deep_research_app)


@deep_research_app.command("prepare")
def deep_research_prepare_command(
    ticker: Annotated[str, typer.Argument(help="The single symbol to prepare.")],
    external: Annotated[
        bool,
        typer.Option(
            "--external",
            help="Also collect current external evidence (W.*). Calls a search provider.",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the whole brief as JSON instead of a summary."),
    ] = False,
) -> None:
    """Refresh one company end to end and assemble its deep research brief.

    Runs the existing ingestion and scoring passes for this ticker only — prices,
    fundamentals, the benchmark if it has fallen behind, the CompounderScore, the
    SEC filing index and the text behind it — then reads the result back out of
    the database as a `DeepResearchBrief`.

    With `--external`, current public material is searched for and the sources
    worth citing are attached as `W.` evidence. Without it the brief carries none,
    which is a complete brief: deterministic preparation never depends on a search
    vendor being configured or reachable.

    No model is called either way.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        try:
            result = prepare_company(
                session,
                settings,
                build_market_data_provider(settings),
                build_fundamentals_provider(settings),
                ticker,
            )
        except PreparationError as error:
            typer.echo(f"Cannot prepare {ticker.upper()}: {error}")
            raise typer.Exit(code=1) from error

        brief = assemble_deep_brief(session, settings, result.ticker, preparation=result)
        if brief is None:  # pragma: no cover — preparation raises before this can happen
            typer.echo(f"Prepared {result.ticker} but could not assemble a brief.")
            raise typer.Exit(code=1)

        collection = None
        if external:
            provider = build_external_research_provider(settings)
            if provider is None:
                typer.echo(
                    "External collection is disabled. Set EXTERNAL_RESEARCH_PROVIDER to enable it."
                )
                raise typer.Exit(code=1)
            collection = collect_external_evidence(provider, settings, brief)
            brief = assemble_deep_brief(
                session,
                settings,
                result.ticker,
                preparation=result,
                external=collection.evidence,
            )
            if brief is None:  # pragma: no cover — it assembled a moment ago
                typer.echo(f"Prepared {result.ticker} but could not assemble a brief.")
                raise typer.Exit(code=1)

        if as_json:
            rendered = brief.model_dump_json(indent=2)
        else:
            rendered = format_preparation(result, brief)
            if collection is not None:
                rendered = f"{rendered}\n\n{format_collection(collection, brief)}"

    typer.echo(rendered)


@deep_research_app.command("run")
def deep_research_run_command(
    ticker: Annotated[str, typer.Argument(help="The single symbol to research.")],
    no_external: Annotated[
        bool,
        typer.Option(
            "--no-external",
            help="Skip external collection and research from deterministic and SEC evidence only.",
        ),
    ] = False,
    refresh_external: Annotated[
        bool,
        typer.Option(
            "--refresh-external",
            help="Search for current evidence again instead of reusing a recent collection.",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the validated report as JSON."),
    ] = False,
) -> None:
    """Research one company and persist a validated deep research report.

    Refreshes the company, collects current external evidence unless
    `--no-external`, assembles the brief, and — only if no identical report
    already exists — asks a model for a draft and validates it. **This spends
    money**, unless the cache answers first.

    External evidence collected recently is reused rather than searched for
    again, so a repeated request inside the window costs nothing at all.
    `--refresh-external` is the deliberate "get me current news" override.

    Nothing unvalidated is ever stored. A provider failure persists nothing at
    all, and a prompt above the input ceiling is refused before the call rather
    than trimmed to fit.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        external = None if no_external else build_external_research_provider(settings)
        run = run_deep_research(
            session,
            settings,
            synthesis=build_deep_research_provider(settings),
            market_data=build_market_data_provider(settings),
            fundamentals=build_fundamentals_provider(settings),
            external=external,
            ticker=ticker,
            refresh_external=refresh_external,
        )
        rendered = (
            run.report.model_dump_json(indent=2)
            if as_json and run.report is not None
            else format_run(run)
        )

    typer.echo(rendered)
    if run.report is None:
        raise typer.Exit(code=1)


@app.command("run-daily")
def run_daily_command(
    output: OutputOption = None,
    limit: RankingLimitOption = DEFAULT_RANKING_LIMIT,
) -> None:
    """Run the whole nightly job: ingest, scan, score, then print the ranking.

    Safe to run repeatedly. Every write is an upsert, so a second run in the
    same day updates rows rather than duplicating them — including the day's
    scores.
    """
    settings = _bootstrap()
    market_data = build_market_data_provider(settings)
    fundamentals = build_fundamentals_provider(settings)

    # Each stage commits before the next begins, so a failure in fundamentals
    # does not roll back the prices already stored.
    with _database(settings) as factory:
        with session_scope(factory) as session:
            typer.echo(f"universe: {update_universe(session, market_data).summary()}")

        with session_scope(factory) as session:
            typer.echo(
                f"market data: {update_market_data(session, market_data, settings).summary()}"
            )

        with session_scope(factory) as session:
            benchmark_report = update_benchmark(session, market_data, settings)
            typer.echo(f"benchmark {settings.benchmark_symbol}: {benchmark_report.summary()}")

        with session_scope(factory) as session:
            typer.echo(
                f"fundamentals: {update_fundamentals(session, fundamentals, settings).summary()}"
            )

        with session_scope(factory) as session:
            typer.echo(f"scoring: {score_market(session, settings).summary()}")

        # Only now, with a preliminary ranking to choose from, is it worth
        # spending metered requests — on the few hundred companies a reader
        # will actually see.
        profiles = build_profile_provider(settings)
        if profiles is None:
            _report_no_enrichment(settings)
        else:
            with session_scope(factory) as session:
                typer.echo(
                    f"enrichment: {enrich_candidates(session, profiles, settings).summary()}"
                )

        with session_scope(factory) as session:
            rows = top_opportunities(session, limit=limit)

    typer.echo("")
    _emit_ranking(rows, output, label="Top Opportunities")


def _report_no_enrichment(settings: Settings) -> None:
    """Explain that the configured provider has nothing to enrich a candidate with.

    EDGAR publishes no market capitalisation and no consolidated volume, which
    are the only two things enrichment buys. Running it against EDGAR alone
    would spend a request per candidate to learn what the scan already knew.
    """
    typer.echo(
        f"enrichment skipped: FUNDAMENTALS_PROVIDER={settings.fundamentals_provider} "
        "supplies no market cap or consolidated volume. Rankings stay PRELIMINARY."
    )


def _emit_ranking(rows: Sequence[RankingRow], output: Path | None, *, label: str) -> None:
    """Print a ranking and, when asked, write it to a CSV file."""
    typer.echo(format_rankings_table(rows))
    typer.echo("")
    typer.echo(f"{label}: {len(rows)} companies")

    if output is not None:
        written = write_rankings_csv(rows, output)
        typer.echo(f"Wrote {written} rows to {output}")


def _percent(value: float | None) -> str:
    """Format a decimal proportion for a one-line summary."""
    return "-" if value is None else f"{value * 100:.1f}%"


def run() -> int:
    """Run the CLI, converting a configuration failure into an exit code.

    Click stays in standalone mode so `--help` and usage errors keep their normal
    formatting; those exit through `SystemExit`, which is caught and turned back
    into a return code. A `ConfigurationError` gets a one-line message instead of
    a traceback, because "you forgot to set ALPACA_API_KEY" is not a crash.

    Returns:
        0 on success, 2 when the application is misconfigured, or whatever exit
        code Click chose.
    """
    try:
        app()
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        return 2
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 0
    return 0
