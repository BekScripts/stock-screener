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

import csv
import json
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import structlog
import typer

from api_clients import ProviderInvalidRequestError, SecDocuments
from data_access import build_session_factory, create_engine_from_url, session_scope
from domain import DelistingReason, TerminalStatus
from research import build_system_prompt, render_brief
from stock_screener.backtest import (
    Cadence,
    CohortEntry,
    DateCoverage,
    DateResult,
    HistoricalIngestResult,
    OutcomeRow,
    PriceBackfillResult,
    backfill_historical_prices,
    backfill_returns,
    backfill_signals,
    date_coverage,
    ingest_cohort,
    market_sessions,
    members_as_of,
    outcome_coverage,
    outcome_rows,
    reconstruct_membership_record,
    research_window,
    sample,
    survivorship_audit,
)
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
from stock_screener.evidence import EvidenceState, inspect_sec_evidence_dir
from stock_screener.identity import (
    ContaminationReport,
    FtdFileResult,
    SymbolResolution,
    attach_identities,
    build_identity_audit,
    detect_contaminated_companies,
    ingest_ftd_history,
    resolve_delisted_symbols,
)
from stock_screener.listing import (
    attribute_observations,
    build_listing_audit,
    ingest_files,
)
from stock_screener.logging import configure_logging
from stock_screener.pit import (
    capture_day,
    discover_delistings,
    ingest_corporate_actions,
    ingest_filing_acceptance,
    ingest_filing_observations,
    measure_forward_returns,
    measure_historical_forward_returns,
    record_capture_failure,
)
from stock_screener.providers import (
    ConfigurationError,
    build_deep_research_provider,
    build_external_research_provider,
    build_ftd_source,
    build_fundamentals_provider,
    build_historical_price_source,
    build_market_data_provider,
    build_observation_source,
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
    update_eligibility_volume,
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
from stock_screener.sec_evidence import (
    ARTIFACT_PREFIXES,
    CONTROL_SUBDIRECTORIES,
    DEFAULT_MIN_FREE_BYTES,
    Budget,
    Checkpoint,
    ChunkStage,
    ControlPlaneUnavailableError,
    ControlState,
    EvidenceControlRoot,
    EvidenceStorage,
    HarvestReport,
    LocalCapacity,
    LocalEvidenceStorage,
    Manifest,
    Selection,
    StorageBackend,
    SweepError,
    checkpoint_path,
    derived_root,
    describe_storage,
    filing_targets,
    guard_selection,
    harvest_artifacts,
    inspect_control_dir,
    load_checkpoint,
    new_urls_since,
    open_evidence_layout,
    preflight_corpus,
    prior_sample,
    reconcile,
    resolve_ciks,
    run_live_canary,
    same_volume,
    save_checkpoint,
    select_universe,
    selection_digest,
    storage_backend,
    submissions_targets,
    verify_digests_of,
)
from stock_screener.sec_evidence.targets import (
    TargetManifestError,
    held_from_manifest,
    load_manifest,
    resolve_documents,
)
from stock_screener.signals import derive_market_signals

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
def update_market_command(
    tickers: TickerOption = None,
    limit: CompanyLimitOption = None,
    since: Annotated[
        datetime | None,
        typer.Option(
            "--since",
            formats=["%Y-%m-%d"],
            help="Backfill from this date instead of resuming from each "
            "company's last stored session. Use it to deepen history; the "
            "nightly top-up should omit it.",
        ),
    ] = None,
) -> None:
    """Refresh daily price history for stored companies."""
    settings = _bootstrap()
    provider = build_market_data_provider(settings)
    with _database(settings) as factory, session_scope(factory) as session:
        report = update_market_data(
            session,
            provider,
            settings,
            tickers=tickers,
            limit=limit,
            since=since.date() if since else None,
        )

    typer.echo(f"market data: {report.summary()}")


@app.command("update-eligibility-volume")
def update_eligibility_volume_command(
    tickers: TickerOption = None, limit: CompanyLimitOption = None
) -> None:
    """Refresh consolidated average volume for the liquidity screen.

    Reads the consolidated tape rather than the feed price history comes from,
    because the dollar-volume threshold is calibrated for the whole market and a
    single exchange carries a few percent of it. Writes no price history.

    Cheap and unmetered, so run it before `enrich`: a company that fails the
    liquidity gate on this figure never costs a paid profile request.
    """
    settings = _bootstrap()
    provider = build_market_data_provider(settings)
    with _database(settings) as factory, session_scope(factory) as session:
        report = update_eligibility_volume(
            session, provider, settings, tickers=tickers, limit=limit
        )

    typer.echo(f"eligibility volume: {report.summary()}")


@app.command("update-benchmark")
def update_benchmark_command(
    since: Annotated[
        datetime | None,
        typer.Option(
            "--since",
            formats=["%Y-%m-%d"],
            help="Backfill from this date instead of resuming. The benchmark "
            "bounds every market-relative figure, so deepen it alongside prices.",
        ),
    ] = None,
) -> None:
    """Refresh the broad-market benchmark's price history.

    Relative strength is a company's return less this series', so scoring
    without it leaves every company short of the data it needs for market
    confirmation. Run it alongside `update-market`.
    """
    settings = _bootstrap()
    provider = build_market_data_provider(settings)
    with _database(settings) as factory, session_scope(factory) as session:
        report = update_benchmark(
            session, provider, settings, since=since.date() if since else None
        )

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


@app.command("capture")
def capture_command(
    snapshot_date: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            formats=["%Y-%m-%d"],
            help="The day to describe. Defaults to today in UTC, matching how "
            "`score` sets its score date.",
        ),
    ] = None,
) -> None:
    """Record what the system knows today, in tables that never change.

    Copies the current scores and computes the observed market state, then
    writes both as immutable rows. Nothing is fetched and no formula runs — the
    scores captured are the ones `score` already produced.

    Safe to re-run: a day that already completed is left alone rather than
    rewritten, because replacing a point-in-time row would substitute hindsight
    for the record. A failure rolls the whole day back and records why, so a day
    is never half-captured and marked good.
    """
    settings = _bootstrap()
    target = snapshot_date.date() if snapshot_date else None
    with _database(settings) as factory:
        try:
            with session_scope(factory) as session:
                result = capture_day(session, settings, snapshot_date=target)
        except Exception as error:
            with session_scope(factory) as session:
                record_capture_failure(
                    session,
                    target or datetime.now(UTC).date(),
                    f"{type(error).__name__}: {error}",
                )
            raise

    typer.echo(f"capture: {result.summary()}")
    if not result.already_captured:
        typer.echo(
            f"  score version {result.score_version}   "
            f"feature version {result.feature_version}   "
            f"scores copied from {result.source_score_date}"
        )


@app.command("update-acceptance")
def update_acceptance_command(
    tickers: TickerOption = None,
    limit: CompanyLimitOption = None,
    refetch: Annotated[
        bool,
        typer.Option(
            "--refetch", help="Re-read every company, not only those with unresolved filings."
        ),
    ] = False,
) -> None:
    """Record when each filing was accepted, so availability can be judged.

    A filing date cannot say whether a document was public at the moment a
    decision was taken: measured across XBRL-bearing filings, 45.7% of those
    accepted on their stated filing date arrived after the 16:00 New York close.
    This stores the instant the SEC accepted each filing, which can.

    Cheap to re-run: a company whose filings are all resolved is skipped without
    a request unless `--refetch` is given.
    """
    settings = _bootstrap()
    provider = build_fundamentals_provider(settings)
    with _database(settings) as factory, session_scope(factory) as session:
        result = ingest_filing_acceptance(
            session, provider, tickers=tickers, limit=limit, only_missing=not refetch
        )

    typer.echo(f"acceptance: {result.summary()}")
    if result.failures:
        typer.echo(result.retry_report())


@app.command("update-actions")
def update_actions_command(
    start: Annotated[
        str, typer.Option("--start", help="Earliest ex-date to fetch, as YYYY-MM-DD.")
    ] = "2015-01-01",
    end: Annotated[
        str | None, typer.Option("--end", help="Latest ex-date to fetch. Defaults to today.")
    ] = None,
    tickers: TickerOption = None,
    limit: CompanyLimitOption = None,
) -> None:
    """Record splits and distributions, so a past price can be read back.

    Stored closes are adjusted retroactively, which makes them right for a
    return and wrong for anything denominated in dollars per share. AAPL's
    stored close for 2020-08-28 is 120.98; the security traded near 499 that
    afternoon, and only the 4-for-1 factor recovers it.

    Appends. Re-running fetches the same actions and stores none of them again.
    """
    settings = _bootstrap()
    provider = build_market_data_provider(settings)
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else datetime.now(UTC).date()

    with _database(settings) as factory, session_scope(factory) as session:
        result = ingest_corporate_actions(
            session, provider, start=start_date, end=end_date, tickers=tickers, limit=limit
        )

    typer.echo(f"actions: {result.summary()}")
    for label, reason in result.failures[:10]:
        typer.echo(f"  {label}: {reason}")


@app.command("measure-returns")
def measure_returns_command(
    observation_date: Annotated[
        str, typer.Argument(help="The observation date to measure from, as YYYY-MM-DD.")
    ],
    tickers: TickerOption = None,
    limit: CompanyLimitOption = None,
) -> None:
    """Record what each security did after an observation date.

    The entry is the open of the *next* session, because a snapshot computable
    at a close cannot be filled at that close. Horizons are counted on the
    market's calendar, so a security that stops trading does not quietly reach
    its horizon on schedule.

    Safe to re-run: a horizon already measured is skipped, and one that was
    truncated because the future had not arrived is measured again once it has.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        result = measure_forward_returns(
            session,
            date.fromisoformat(observation_date),
            benchmark=settings.benchmark_symbol,
            tickers=tickers,
            limit=limit,
        )

    typer.echo(f"returns: {result.summary()}")


@app.command("discover-delistings")
def discover_delistings_command(
    start: Annotated[
        str, typer.Option("--start", help="Earliest effective date, as YYYY-MM-DD.")
    ] = "2020-01-01",
    end: Annotated[
        str | None, typer.Option("--end", help="Latest effective date. Defaults to today.")
    ] = None,
) -> None:
    """Record the securities that left the market, which no other table holds.

    Across 4,599 companies and the 120 sessions after 2024-01-02, not one
    security in the database stopped trading. A universe that size loses about a
    hundred names to acquisitions and failures over six months — the zero is a
    property of a table that holds only what is investable today.

    Discovery is by date range rather than by symbol, because asking about a
    symbol can only confirm the fate of one already known.
    """
    settings = _bootstrap()
    provider = build_market_data_provider(settings)
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else datetime.now(UTC).date()

    with _database(settings) as factory, session_scope(factory) as session:
        result = discover_delistings(session, provider, start=start_date, end=end_date)

    typer.echo(f"delistings: {result.summary()}")


@app.command("derive-signals")
def derive_signals_command(
    snapshot_date: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            formats=["%Y-%m-%d"],
            help="The captured day to derive. Defaults to the newest day with stored features.",
        ),
    ] = None,
) -> None:
    """Describe what the market was doing, from the captured features.

    Reads the immutable feature snapshots and writes market signals beside them:
    peer percentiles, a multi-label setup, and a tradability verdict of
    TRADABLE, UNTRADABLE or UNVERIFIED.

    This produces no ranking and no score. The dimensions it reports are
    anti-correlated, so averaging them would rank highest whatever is mediocre
    at everything — deciding what to do with these facts belongs to a strategy
    layer that does not exist yet.

    Re-running a day already derived under the same version writes nothing. A
    new version appends beside the old one rather than restating it.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        result = derive_market_signals(
            session, snapshot_date=snapshot_date.date() if snapshot_date else None
        )

    typer.echo(f"signals: {result.summary()}")
    if result.setups:
        ordered = sorted(result.setups.items(), key=lambda item: -item[1])
        typer.echo("  setups: " + "   ".join(f"{name} {count}" for name, count in ordered))


@app.command("backfill-signals")
def backfill_signals_command(
    start: Annotated[
        str, typer.Option("--start", help="Earliest session to describe, as YYYY-MM-DD.")
    ],
    end: Annotated[
        str | None, typer.Option("--end", help="Latest session to describe. Defaults to today.")
    ] = None,
    cadence: Annotated[
        Cadence,
        typer.Option(
            "--cadence",
            help="How densely to sample the calendar: every session, or the last of each week "
            "or month.",
        ),
    ] = Cadence.MONTHLY,
) -> None:
    """Capture features and derive market signals for a range of past sessions.

    Runs the ordinary `capture` and `derive-signals` passes over sessions that
    have already happened, from the price history already stored. No provider is
    called and no formula or threshold changes — this only makes the record
    exist for days the system was not running on.

    Safe to re-run and safe to interrupt: each session is its own transaction, a
    day already captured is left alone, and stopping the run loses at most the
    day in flight. Re-running resumes from the record itself.
    """
    settings = _bootstrap()
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else datetime.now(UTC).date()

    with _database(settings) as factory:
        with session_scope(factory) as session:
            calendar = market_sessions(
                session, settings.benchmark_symbol, start=start_date, end=end_date
            )
        wanted = sample(calendar, cadence)
        if not wanted:
            typer.echo("backfill-signals: no stored benchmark sessions in that range")
            return
        typer.echo(
            f"backfill-signals: {len(wanted)} sessions "
            f"({wanted[0]}..{wanted[-1]}, {cadence.value}) of {len(calendar)} in range"
        )
        report = backfill_signals(factory, settings, wanted, on_progress=_echo_progress)

    typer.echo(report.summary())
    _report_gaps(report.missing)
    for failure in report.failed[:10]:
        typer.echo(f"  {failure.session_date}: {failure.detail}")


@app.command("backfill-returns")
def backfill_returns_command(
    start: Annotated[
        str, typer.Option("--start", help="Earliest decision date to measure, as YYYY-MM-DD.")
    ],
    end: Annotated[
        str | None,
        typer.Option("--end", help="Latest decision date to measure. Defaults to today."),
    ] = None,
    cadence: Annotated[
        Cadence,
        typer.Option("--cadence", help="How densely to sample the calendar."),
    ] = Cadence.MONTHLY,
) -> None:
    """Measure forward returns from a range of past decision dates.

    Runs the ordinary `measure-returns` pass over many dates. The measurement is
    unchanged: the entry is the open of the session strictly after the decision
    date, horizons are counted on the market's calendar, and a security that
    stopped trading is recorded as interrupted rather than filled in.

    A horizon that has not elapsed yet is stored as `TRUNCATED` and measured
    again on a later run, so re-running late in the window is how the tail of it
    realises.
    """
    settings = _bootstrap()
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else datetime.now(UTC).date()

    with _database(settings) as factory:
        with session_scope(factory) as session:
            calendar = market_sessions(
                session, settings.benchmark_symbol, start=start_date, end=end_date
            )
        wanted = sample(calendar, cadence)
        if not wanted:
            typer.echo("backfill-returns: no stored benchmark sessions in that range")
            return
        typer.echo(
            f"backfill-returns: {len(wanted)} decision dates "
            f"({wanted[0]}..{wanted[-1]}, {cadence.value}) of {len(calendar)} in range"
        )
        report = backfill_returns(factory, settings, wanted, on_progress=_echo_progress)

    typer.echo(report.summary())
    _report_gaps(report.missing)
    for failure in report.failed[:10]:
        typer.echo(f"  {failure.session_date}: {failure.detail}")


@app.command("research-coverage")
def research_coverage_command(
    start: Annotated[str, typer.Option("--start", help="Earliest date to measure, as YYYY-MM-DD.")],
    end: Annotated[
        str | None, typer.Option("--end", help="Latest date to measure. Defaults to today.")
    ] = None,
    cadence: Annotated[
        Cadence,
        typer.Option("--cadence", help="How densely to measure the calendar."),
    ] = Cadence.MONTHLY,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write the per-date funnel to this CSV as well."),
    ] = None,
) -> None:
    """Report how much of the record is usable, date by date.

    Walks the roster on each measured date and counts how many securities
    survive each stage between existing in the database and being studiable
    without inventing anything: listed, priced deeply enough, filed by the date,
    valued, scored, signalled, and measured forward.

    Reads only. Nothing is written to the database and no formula runs that the
    live path does not run. Universe membership is reported as
    `reconstruct_universe` returns it — where the record cannot reach back to a
    date, the count is zero and says so, rather than being replaced by a proxy.
    """
    settings = _bootstrap()
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else datetime.now(UTC).date()

    with _database(settings) as factory, session_scope(factory) as session:
        calendar = market_sessions(
            session, settings.benchmark_symbol, start=start_date, end=end_date
        )
        wanted = sample(calendar, cadence)
        if not wanted:
            typer.echo("research-coverage: no stored benchmark sessions in that range")
            return
        typer.echo(f"research-coverage: measuring {len(wanted)} dates ({wanted[0]}..{wanted[-1]})")
        measured = []
        for index, day in enumerate(wanted, start=1):
            coverage = date_coverage(session, settings, day)
            measured.append(coverage)
            typer.echo(
                f"  [{index}/{len(wanted)}] {day}  members {coverage.universe_members:5d}  "
                f"listed {coverage.listed_on_date:5d}  scored {coverage.score_reconstructable:5d}  "
                f"signals {coverage.signals_available:5d}  "
                f"returns {coverage.forward_returns_available:5d}  "
                f"usable {coverage.fully_usable:5d}"
            )

    window = research_window(measured)
    typer.echo(
        f"window: {window.first_usable} .. {window.last_usable}   "
        f"median usable {window.median_usable:.0f}   minimum {window.minimum_usable}   "
        f"survivorship {window.survivorship}"
    )
    if window.longest_run is not None:
        typer.echo(f"  longest continuous run: {window.longest_run[0]} .. {window.longest_run[1]}")
    if output is not None:
        _write_coverage_csv(measured, output)
        typer.echo(f"  wrote {output}")


@app.command("ingest-historical")
def ingest_historical_command(
    cohort: Annotated[
        Path,
        typer.Argument(
            help="JSON Lines file, one delisted security per line, each carrying at "
            "least cik, ticker, name, start and end."
        ),
    ],
    limit: CompanyLimitOption = None,
) -> None:
    """Ingest delisted securities into the historical record, by CIK.

    Reads a cohort whose identities a caller has already established, because a
    delisting carries a ticker and a ticker is not an identity: EDGAR's current
    map resolves fewer than four per cent of delisted symbols, and the ones it
    does resolve are mostly symbols since reissued to a different filer.

    Nothing is guessed. A filer EDGAR serves no facts for keeps empty
    fundamentals and the gap `NO_EDGAR_FUNDAMENTALS`; a security outside the
    free price window keeps `NO_FREE_PRICE_HISTORY`; a terminal value no source
    states stays NULL, which is UNKNOWN and never zero.

    Appends. Re-running the same cohort updates one row per CIK and stores no
    observation, bar or action twice.
    """
    settings = _bootstrap()
    entries = _read_cohort(cohort)
    if limit is not None:
        entries = entries[:limit]
    if not entries:
        typer.echo(f"ingest-historical: {cohort} holds no securities")
        return

    observations = build_observation_source(settings)
    market = build_market_data_provider(settings)
    typer.echo(f"ingest-historical: {len(entries)} securities from {cohort}")

    def progress(result: HistoricalIngestResult, index: int) -> None:
        typer.echo(f"  [{index}/{len(entries)}] {result.summary()}")

    with _database(settings) as factory:
        report = ingest_cohort(
            factory,
            observations_source=observations,
            market=market,
            cohort=entries,
            on_progress=progress,
        )

    typer.echo(report.summary())
    gaps = report.gap_counts()
    if gaps:
        typer.echo(
            "  gaps: " + "   ".join(f"{name} {count}" for name, count in sorted(gaps.items()))
        )
    for ticker, reason in report.failures[:10]:
        typer.echo(f"  {ticker}: {reason}")


@app.command("update-observations")
def update_observations_command(
    tickers: TickerOption = None,
    limit: CompanyLimitOption = None,
) -> None:
    """Append what each filing reported, keeping every earlier version.

    The ordinary fundamentals pass overwrites a restated period. This one keeps
    both figures with the dates they were filed, which is what makes a past
    decision reconstructible without reading the future.

    Costs no extra request when run after `update-fundamentals` for the same
    symbols: the filing document just read is reused.
    """
    settings = _bootstrap()
    provider = build_fundamentals_provider(settings)
    with _database(settings) as factory, session_scope(factory) as session:
        result = ingest_filing_observations(session, provider, tickers=tickers, limit=limit)

    typer.echo(f"observations: {result.summary()}")
    if result.failures:
        typer.echo(result.retry_report())


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


identity_app = typer.Typer(
    name="identity",
    help=(
        "Security identity: read the SEC fails-to-deliver record, resolve delisted "
        "symbols to stable security ids, and report what cannot be resolved."
    ),
    no_args_is_help=True,
)
app.add_typer(identity_app)


@identity_app.command("ingest-ftd")
def identity_ingest_ftd_command(
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="Read only the first N archives."),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Re-read archives an earlier run already recorded."),
    ] = False,
) -> None:
    """Read the SEC fails-to-deliver history into the identity-evidence tables.

    433 archives from 2004 onward, about 600 MB, cached under `FTD_CACHE_DIR` so
    a second run reads from disk rather than from a free public server. Resumable
    and idempotent: archives already recorded are skipped, and re-reading one
    widens no window and changes no total.

    Nothing is resolved here. This writes down what the files say; `resolve`
    decides what it means.
    """
    settings = _bootstrap()
    source = build_ftd_source(settings)
    archives = source.list_archives()
    if limit is not None:
        archives = archives[:limit]
    typer.echo(f"identity ingest-ftd: {len(archives)} archives")

    def progress(result: FtdFileResult, index: int, total: int) -> None:
        if result.already_read and index % 50:
            return
        typer.echo(f"  [{index}/{total}] {result.summary()}")

    with _database(settings) as factory:
        report = ingest_ftd_history(
            factory, source, archives=archives, refresh=refresh, on_progress=progress
        )
    source.close()

    typer.echo(report.summary())
    for failure in report.failed[:10]:
        typer.echo(f"  {failure.summary()}")


@identity_app.command("resolve")
def identity_resolve_command(
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="Resolve only the first N delisted symbols."),
    ] = None,
) -> None:
    """Resolve delisted symbols to stable security ids from the FTD evidence.

    One delisting at a time — a symbol *with* its date, because a bare symbol is
    unanswerable. Where the evidence fits two issuers, or none, that is recorded
    and nothing is created: a guessed identity is worse than an absent one,
    because a study cannot see it.

    Writes historical securities and their dated identifier intervals. It never
    writes to `companies`.
    """
    settings = _bootstrap()

    def progress(resolution: SymbolResolution, index: int, total: int) -> None:
        if index % 250 and index != total:
            return
        typer.echo(f"  [{index}/{total}] {resolution.ticker} {resolution.outcome}")

    with _database(settings) as factory:
        report = resolve_delisted_symbols(factory, limit=limit, on_progress=progress)

    typer.echo(report.summary())
    for resolution in report.ambiguous[:10]:
        typer.echo(f"  ambiguous {resolution.ticker}: {resolution.detail}")


@identity_app.command("contamination")
def identity_contamination_command(
    output: OutputOption = None,
) -> None:
    """Report live company rows whose ticker two issuers have held.

    Reads only. Nothing is merged, split or repaired — deciding which of two
    issuers a price bar belongs to is a data operation, and this is a detection
    one. The report is what lets a study exclude the rows it cannot trust.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        report = detect_contaminated_companies(session)

    typer.echo(report.summary())
    for row in report.contaminated[:25]:
        typer.echo(f"  {row.ticker} (company {row.company_id}, {row.status}): {row.detail}")
    if output is not None:
        _write_contamination_csv(report, output)
        typer.echo(f"  wrote {output}")


@identity_app.command("attach")
def identity_attach_command(
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="Attach at most N securities."),
    ] = None,
) -> None:
    """Give stored historical securities their dated identifier intervals.

    The companion to `resolve`, for securities that already exist but carry no
    identity. `resolve` walks the delisting registry, so a security whose exit no
    corporate-action feed files was never reached — which is every receivership.
    This anchors on the security's own last trading date instead.
    """
    settings = _bootstrap()

    def progress(resolution: SymbolResolution, index: int, total: int) -> None:
        if index % 100 and index != total:
            return
        typer.echo(f"  [{index}/{total}] {resolution.ticker} {resolution.outcome}")

    with _database(settings) as factory:
        report = attach_identities(factory, limit=limit, on_progress=progress)

    typer.echo(report.summary())


@app.command("backfill-historical-prices")
def backfill_historical_prices_command(
    limit: CompanyLimitOption = None,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Re-fetch securities that already hold bars."),
    ] = False,
) -> None:
    """Fetch prices and splits for delisted securities that have none.

    No CIK is required. A security whose fundamentals are unrecoverable still has
    prices, corporate actions and therefore outcomes, and dropping it for want of
    a filer number would put the survivorship bias straight back.

    The fetch window is bounded to each security's own trading era, which is what
    makes a ticker-keyed request safe for a symbol that was later reissued.
    """
    settings = _bootstrap()
    market = build_historical_price_source(settings)

    def progress(result: PriceBackfillResult, index: int, total: int) -> None:
        typer.echo(f"  [{index}/{total}] {result.summary()}")

    with _database(settings) as factory:
        report = backfill_historical_prices(
            factory, market, only_unpriced=not refresh, limit=limit, on_progress=progress
        )

    typer.echo(report.summary())
    for failure in report.failed[:10]:
        typer.echo(f"  {failure.summary()}")


@app.command("measure-historical-returns")
def measure_historical_returns_command(
    observation_date: Annotated[
        datetime,
        typer.Option("--date", "-d", formats=["%Y-%m-%d"], help="The decision date."),
    ],
    limit: CompanyLimitOption = None,
) -> None:
    """Measure forward returns for securities that left the market.

    The same `domain.forward_returns` the company path uses, over
    `historical_prices`, written to the same `forward_returns` table. A delisted
    security's outcome is an ordinary row there, so an ordinary query over a
    decision date returns it without being asked to.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        result = measure_historical_forward_returns(
            session,
            observation_date.date(),
            benchmark=settings.benchmark_symbol,
            limit=limit,
        )
    typer.echo(result.summary())


@app.command("reconstruct-membership")
def reconstruct_membership_command() -> None:
    """Rebuild every security's universe-membership intervals from dated evidence.

    Membership is a listing fact, recorded as effective-dated intervals against
    both identity kinds. It is not eligibility — the thresholds are still applied
    per date by the frozen screen — and it is not price presence, which proves a
    security traded and nothing about whether it belonged to the universe.

    Every `MEMBER` verdict this can currently produce is `INFERRED`, because
    exchange, security type and sector are undated everywhere in this system. The
    rows say so, per row.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        result = reconstruct_membership_record(session)
    typer.echo(result.summary())


@app.command("universe-as-of")
def universe_as_of_command(
    observation_date: Annotated[
        datetime,
        typer.Option("--date", "-d", formats=["%Y-%m-%d"], help="The decision date."),
    ],
) -> None:
    """Show the securities a backtest could have chosen from on a date.

    One read of `universe_membership`, both identity kinds, no flag. Securities
    the record cannot place are reported as unknown rather than omitted — a
    universe that hides the size of its own uncertainty is the most useful lie a
    survivorship-biased dataset tells.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        universe = members_as_of(session, observation_date.date())
    typer.echo(universe.summary())


@app.command("survivorship-audit")
def survivorship_audit_command(
    observation_date: Annotated[
        datetime,
        typer.Option("--date", "-d", formats=["%Y-%m-%d"], help="The decision date."),
    ],
    horizon: Annotated[
        int,
        typer.Option("--horizon", "-h", min=1, help="Sessions measured."),
    ] = 20,
) -> None:
    """Report how survivorship-aware the outcome record actually is.

    The distinction it exists to protect: an architecture that *can* carry a
    delisted security's outcome is not a dataset that *does*. The first is a
    property of the schema; the second is a percentage, and reporting the first
    while implying the second is how a censored dataset gets called complete.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        audit = survivorship_audit(
            session, observation_date=observation_date.date(), horizon_sessions=horizon
        )
    for line in audit.lines():
        typer.echo(line)


@app.command("outcomes")
def outcomes_command(
    observation_date: Annotated[
        datetime,
        typer.Option("--date", "-d", formats=["%Y-%m-%d"], help="The decision date."),
    ],
    horizon: Annotated[
        int,
        typer.Option("--horizon", "-h", min=1, help="Sessions measured."),
    ] = 20,
    output: OutputOption = None,
) -> None:
    """Show one decision date's outcomes: survivors and delisted names together.

    One read of `forward_returns`. There is no flag to include delisted
    securities and no union to remember, which is the point — the failure mode of
    every alternative is a query that silently returns survivors only.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        coverage = outcome_coverage(session, observation_date.date(), horizon_sessions=horizon)
        rows = outcome_rows(session, observation_date.date(), horizon_sessions=horizon)

    typer.echo(coverage.summary())
    for row in rows[:25]:
        terminal = ""
        if row.terminal is not None and row.terminal.status is not TerminalStatus.ONGOING:
            value = (
                "UNKNOWN"
                if row.terminal.value_per_share is None
                else (f"{row.terminal.value_per_share:.4g}")
            )
            terminal = f"  terminal {row.terminal.status}={value}"
        typer.echo(
            f"  {row.identity_kind[:4]} {row.ticker:<8} {row.status:<11} "
            f"{_percent(row.total_return)}{terminal}"
        )
    if output is not None:
        _write_outcomes_csv(rows, output)
        typer.echo(f"  wrote {output}")


@identity_app.command("audit")
def identity_audit_command() -> None:
    """Report the state of the identity layer, every figure read from the tables.

    Includes the number that has to be zero: forward returns attached to a
    historical security. Phase 9A gives the schema the shape to hold them and
    migrates none, because migrating one needs its identity proven first.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        audit = build_identity_audit(session)
    for line in audit.lines():
        typer.echo(line)


listing_app = typer.Typer(
    name="listing",
    help=(
        "Historical listing evidence: import Nasdaq's public monthly market-share "
        "files, attribute them to securities, and report what remains unknown."
    ),
    no_args_is_help=True,
)
app.add_typer(listing_app)


@listing_app.command("ingest")
def listing_ingest_command(
    directory: Annotated[
        Path,
        typer.Argument(help="Directory of downloaded .xlsx market-share files."),
    ],
) -> None:
    """Import monthly market-share files, validating each one's tape identity.

    A file whose contents do not carry the tape its month predicts is
    quarantined rather than imported — the check that stops five years of NYSE
    Arca ETFs being recorded as NYSE listings. Re-running is a no-op.
    """
    settings = _bootstrap()
    payloads = {path.name: path.read_bytes() for path in sorted(directory.glob("*.xlsx"))}
    if not payloads:
        typer.echo(f"no .xlsx files in {directory}")
        raise typer.Exit(code=1)
    with _database(settings) as factory, session_scope(factory) as session:
        report = ingest_files(session, payloads)
    typer.echo(report.summary())
    for result in report.quarantined[:10]:
        typer.echo(f"  quarantined {result.file_name}: {result.reason}")


@listing_app.command("attribute")
def listing_attribute_command() -> None:
    """Attach observations to securities, using the Phase 9J identity rules.

    A symbol two issuers held is recorded as reused rather than attributed to
    whichever occupant sorts first.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        report = attribute_observations(session)
    typer.echo(report.summary())


@listing_app.command("audit")
def listing_audit_command(
    scope_from: Annotated[
        datetime,
        typer.Option(formats=["%Y-%m-%d"], help="First month in scope for the delisted cohort."),
    ] = datetime(2022, 1, 1, tzinfo=UTC),
) -> None:
    """Report what the listing evidence covers, and what it cannot see.

    The censored count is the point: NYSE American appears in no importable
    file, so its members are neither confirmed nor excluded.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        audit = build_listing_audit(session, scope_from=scope_from.date())
    for line in audit.lines():
        typer.echo(line)


evidence_app = typer.Typer(
    name="evidence",
    help=(
        "The SEC evidence corpus: inspect the configured root, plan a controlled "
        "batch, and harvest documents onto external storage within a budget."
    ),
    no_args_is_help=True,
)
app.add_typer(evidence_app)


@evidence_app.command("status")
def evidence_status_command() -> None:
    """Report where SEC evidence would be written, and whether it is reachable.

    Prints the state rather than raising, so an unmounted drive is diagnosable
    without reading a traceback.
    """
    settings = _bootstrap()
    backend = storage_backend(settings)
    description = describe_storage(settings)
    for label, text in description:
        typer.echo(f"{label:<17}: {text}")
    failed = any(label == "b2 state" and text.startswith("INVALID") for label, text in description)

    # The local root is inspected only when it is the artifact backend. Under
    # b2 it is unused, and reporting it would suggest a fallback that does
    # not exist.
    state: EvidenceState | None = None
    path: Path | None = None
    if backend is StorageBackend.LOCAL:
        state, path = inspect_sec_evidence_dir(settings)
        typer.echo(f"artifact root    : {path if path is not None else '(unset)'}")
        typer.echo(f"artifact state   : {state}")
        if state is not EvidenceState.UNCONFIGURED and path is not None:
            for name in ARTIFACT_PREFIXES:
                child = path / name
                typer.echo(f"  {name:<14} {'present' if child.is_dir() else 'absent'}")

    control_state, control_path = inspect_control_dir(settings)
    if control_state is ControlState.UNCONFIGURED and backend is StorageBackend.LOCAL:
        typer.echo("control root     : (unset) — LEGACY: colocated beneath the artifact root")
        typer.echo(
            "control state    : shared failure domain with the artifacts; set SEC_CONTROL_DIR"
        )
        control_path = path
    elif control_state is ControlState.UNCONFIGURED:
        typer.echo("control root     : (unset)")
        typer.echo(
            "control state    : UNCONFIGURED — SEC_STORAGE_BACKEND=b2 requires SEC_CONTROL_DIR"
        )
        failed = True
    else:
        typer.echo(f"control root     : {control_path}")
        typer.echo(f"control state    : {control_state}")
    if control_path is not None and control_path.is_dir():
        for name in CONTROL_SUBDIRECTORIES:
            child = control_path / name
            typer.echo(f"  {name:<14} {'present' if child.is_dir() else 'absent'}")
        typer.echo(f"manifest         : {control_path / 'manifests' / 'evidence.jsonl'}")
        typer.echo(f"checkpoints      : {control_path / 'checkpoints'}")
        if (
            control_state is not ControlState.UNCONFIGURED
            and path is not None
            and path.is_dir()
            and same_volume(control_path, path)
        ):
            typer.echo("WARNING          : control root shares a volume with the artifact root")

    if (
        failed
        or state
        in {
            EvidenceState.UNCONFIGURED,
            EvidenceState.UNAVAILABLE,
            EvidenceState.NOT_A_DIRECTORY,
        }
        or control_state in {ControlState.UNAVAILABLE, ControlState.NOT_A_DIRECTORY}
    ):
        raise typer.Exit(code=1)


@evidence_app.command("b2-canary")
def evidence_b2_canary_command(
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help=(
                "Required. This command writes to the configured bucket — a few "
                "synthetic objects under _canary/<run-id>/ — and deletes them afterwards."
            ),
        ),
    ] = False,
    keep: Annotated[
        bool,
        typer.Option("--keep", help="Leave the canary objects in place instead of deleting them."),
    ] = False,
) -> None:
    """Prove the configured Backblaze bucket works, in an isolated namespace.

    The first request that leaves the machine for B2 should be this one. It
    exercises every storage operation, one real harvest into a temporary
    control root, a replay and a reconciliation — all under
    `_canary/<run-id>/`, never under the artifact prefixes — and then removes
    what it wrote. It fetches nothing from the SEC and touches no production
    manifest. About two megabytes are written.
    """
    settings = _bootstrap()
    if storage_backend(settings) is not StorageBackend.B2:
        typer.echo("STOPPED: the canary requires SEC_STORAGE_BACKEND=b2")
        raise typer.Exit(code=1)
    if not confirm:
        typer.echo(
            "refusing without --confirm: this writes synthetic objects under _canary/<run-id>/ "
            "in the configured bucket and deletes them afterwards"
        )
        raise typer.Exit(code=1)

    control_dir = Path(tempfile.mkdtemp(prefix="b2-canary-control-"))
    try:
        report = run_live_canary(settings, control_dir=control_dir, cleanup=not keep)
    except ControlPlaneUnavailableError as exc:
        typer.echo(f"STOPPED: {exc}")
        raise typer.Exit(code=1) from exc
    for line in report.summary():
        typer.echo(line)
    typer.echo(f"  canary control root: {report.control_root} (temporary; not the production one)")
    typer.echo("CANARY PASSED" if report.passed else "CANARY FAILED")
    raise typer.Exit(code=0 if report.passed else 1)


@evidence_app.command("sample")
def evidence_sample_command(
    limit: Annotated[int, typer.Option(help="How many securities to print.")] = 100,
    offset: Annotated[
        int,
        typer.Option(min=0, help="Securities to skip. With --limit this is one chunk."),
    ] = 0,
    production: Annotated[
        bool,
        typer.Option(
            "--production/--sample",
            help=(
                "Paginate the whole security record, or the frozen 100-security "
                "validation batch. --limit means what it says only on --production."
            ),
        ),
    ] = False,
) -> None:
    """Print the deterministic controlled batch.

    Reads the database and nothing else — no network, no writes. The same
    database yields the same batch every time, which is what makes a measured
    harvest reproducible.
    """
    settings = _bootstrap()
    with _database(settings) as factory, session_scope(factory) as session:
        selections = select_universe(session, production=production, offset=offset, limit=limit)
    for selection in selections:
        cik = selection.cik if selection.cik is not None else "-"
        typer.echo(f"{selection.bucket:<22} {selection.symbol:<8} cik={cik:<12} {selection.name}")
    typer.echo(f"{len(selections)} securities")


@evidence_app.command("harvest")
def evidence_harvest_command(
    limit: Annotated[int, typer.Option(help="Most securities to read.")] = 100,
    max_requests: Annotated[
        int, typer.Option(help="Network requests allowed. 0 is unlimited.")
    ] = 500,
    max_bytes: Annotated[int, typer.Option(help="Bytes allowed to store. 0 is unlimited.")] = 10
    * 1024**3,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Plan only: no requests, no writes.")
    ] = False,
    resume: Annotated[
        bool, typer.Option("--resume", help="Reuse artifacts the manifest verifies.")
    ] = True,
    filings_per_security: Annotated[
        int,
        typer.Option(
            help=(
                "Expand each stored submissions index into at most this many filing "
                "documents. 0 fetches catalogues only, which is the default because "
                "expansion multiplies a run's size."
            )
        ),
    ] = 0,
    resolve: Annotated[
        bool,
        typer.Option(
            "--resolve/--no-resolve",
            help="Look up missing CIKs in the SEC ticker file. Costs one request.",
        ),
    ] = False,
    offset: Annotated[
        int,
        typer.Option(min=0, help="Securities to skip. With --limit this is one chunk."),
    ] = 0,
    production: Annotated[
        bool,
        typer.Option(
            "--production/--sample",
            help=(
                "Paginate the whole security record, or the frozen 100-security "
                "validation batch. --limit means what it says only on --production."
            ),
        ),
    ] = False,
) -> None:
    """Fetch SEC evidence for the controlled batch, within a hard budget.

    Resumable by construction: an artifact whose stored bytes still hash to the
    digest the manifest recorded is not fetched again, so a run stopped by its
    budget continues where it left off rather than starting over.

    A dry run issues no request and writes nothing at all, the manifest
    included, so it is safe against an evidence root you are unsure about.
    """
    settings = _bootstrap()
    storage, control = _open_layout(settings)

    with _database(settings) as factory, session_scope(factory) as session:
        selections = select_universe(session, production=production, offset=offset, limit=limit)

    typer.echo(
        f"population    : {'production universe' if production else 'controlled sample'}"
        f" | offset {offset} | limit {limit}"
    )

    # One budget for the whole run, created before anything reaches the network.
    # Resolution reads the SEC ticker file, which is an HTTP request the harvest
    # causes, so it meters into the same ceiling as every document. It is opt-in
    # and never happens during a dry run.
    budget = Budget(max_requests=max_requests, max_bytes=max_bytes)
    resolver = build_observation_source(settings, meter=budget) if resolve and not dry_run else None
    try:
        resolutions = resolve_ciks(selections, resolver)
    finally:
        if resolver is not None:
            resolver.close()
    stored = sum(1 for r in resolutions if r.source == "STORED")
    looked_up = sum(1 for r in resolutions if r.source == "EDGAR")
    unresolved = sum(1 for r in resolutions if not r.resolved)
    typer.echo(
        f"securities    : {len(selections)} — {stored} stored CIK, "
        f"{looked_up} resolved via EDGAR, {unresolved} unresolved"
    )

    targets = submissions_targets(resolutions)
    expanded = filing_targets(storage, resolutions, filings_per_security=filings_per_security)
    targets += expanded
    typer.echo(
        f"targets       : {len(targets)} ({len(targets) - len(expanded)} submissions, "
        f"{len(expanded)} filing documents, cap {filings_per_security}/security)"
    )

    if not resume:
        typer.echo("resume        : disabled — every target will be re-fetched")

    documents = None if dry_run else _sec_documents(settings, budget)
    try:
        report = harvest_artifacts(
            targets,
            storage,
            Manifest(control.manifest_path),
            documents,
            budget=budget,
            dry_run=dry_run,
        )
    finally:
        if documents is not None:
            documents.close()

    typer.echo(report.summary())
    typer.echo(
        f"remaining     : {budget.requests_remaining} requests, {budget.bytes_remaining} bytes"
    )
    for url, reason in report.failures[:10]:
        typer.echo(f"  failed {url}: {reason}")


@evidence_app.command("harvest-manifest")
def evidence_harvest_manifest_command(
    target_manifest: Annotated[Path, typer.Option(help="Frozen JSONL target manifest to execute.")],
    expect_sha256: Annotated[
        str | None, typer.Option(help="Digest the manifest must have.")
    ] = None,
    expect_rows: Annotated[int | None, typer.Option(help="Rows the manifest must hold.")] = None,
    max_requests: Annotated[int, typer.Option(help="HTTP attempts allowed.")] = 3000,
    max_bytes: Annotated[int, typer.Option(help="Bytes allowed.")] = 2 * 1024**3,
    min_free_bytes: Annotated[
        int, typer.Option(help="Free space the run refuses to eat into.")
    ] = DEFAULT_MIN_FREE_BYTES,
    replay: Annotated[
        bool,
        typer.Option(
            "--replay",
            help="Permit already-held targets, to prove a manifest replay costs nothing.",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Verify and plan only. No requests, no writes.")
    ] = False,
) -> None:
    """Fetch exactly the filings a frozen target manifest names.

    The production harvest discovers filings: resolve a CIK, fetch a submissions
    catalogue, take the six most recent. This does none of that. Phase 9L.2
    already decided which filings to fetch, so re-deriving them would spend
    requests to risk selecting something else.

    **Nothing is resolved over the network.** Document names come from the
    submissions catalogues already on disk, so replaying a manifest whose targets
    are all held issues zero HTTP requests.
    """
    settings = _bootstrap()
    storage, control = _open_layout(settings)
    manifest = Manifest(control.manifest_path)
    index = derived_root(control) / "sec_evidence_index.db"
    if not index.is_file():
        typer.echo(f"STOPPED: no derived index at {index}; run the offline extraction first")
        raise typer.Exit(code=1)

    try:
        targets, audit = load_manifest(
            target_manifest, expect_sha256=expect_sha256, expect_rows=expect_rows
        )
    except TargetManifestError as exc:
        typer.echo(f"STOPPED: {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo(f"manifest      : {target_manifest}")
    typer.echo(f"manifest sha  : {audit.digest}")
    typer.echo(f"targets       : {audit.rows} ({audit.unique_keys} unique, {audit.filers} filers)")
    typer.echo(f"forms         : {audit.forms}")
    typer.echo(f"years         : {audit.years}")

    with closing(sqlite3.connect(index)) as catalogue:
        # The live manifest, not the derived index: the index can predate the
        # most recent harvest, and planning against it would re-request filings
        # already on disk.
        held = held_from_manifest(manifest)
        overlap = sorted({t.key for t in targets} & held)
        resolved, unresolved = resolve_documents(targets, catalogue)

    if overlap and not replay:
        # Never silently drop and replace: a validation population that changed
        # is no longer validating the plan that was frozen. `--replay` is the
        # deliberate exception — proving a completed manifest costs nothing to
        # re-run requires passing it targets that are already held.
        typer.echo(f"STOPPED: {len(overlap)} target(s) are already trusted, e.g. {overlap[:3]}")
        typer.echo("         pass --replay to re-run a completed manifest deliberately")
        raise typer.Exit(code=1)
    if overlap:
        typer.echo(f"already held  : {len(overlap)} — replay requested, expecting zero requests")
    else:
        typer.echo("already held  : 0 — target set is disjoint from the corpus")
    typer.echo(f"resolved      : {len(resolved)} documents, {len(unresolved)} unresolved")
    if unresolved:
        typer.echo(f"  unresolved e.g. {[t.key for t in unresolved][:3]}")

    try:
        preflight_corpus(storage, manifest, min_free_bytes=min_free_bytes, verify_digests=False)
    except SweepError as exc:
        typer.echo(f"STOPPED: {exc}")
        raise typer.Exit(code=1) from exc
    _echo_free(storage)

    if dry_run:
        typer.echo("dry run — manifest verified, no request issued and nothing written")
        return

    budget = Budget(max_requests=max_requests, max_bytes=max_bytes)
    documents = _sec_documents(settings, budget)
    try:
        report = harvest_artifacts(resolved, storage, manifest, documents, budget=budget)
    finally:
        documents.close()

    typer.echo(report.summary())
    typer.echo(
        f"remaining     : {budget.requests_remaining} requests, {budget.bytes_remaining} bytes"
    )
    for url, reason in report.failures[:10]:
        typer.echo(f"  failed {url}: {reason}")


@evidence_app.command("sweep")
def evidence_sweep_command(
    offset: Annotated[int, typer.Option(min=0, help="Where the chunk starts.")] = 0,
    limit: Annotated[int, typer.Option(min=1, help="Identities in the chunk.")] = 250,
    filings_per_security: Annotated[int, typer.Option(help="Filing cap per security.")] = 6,
    max_requests: Annotated[int, typer.Option(help="HTTP attempts allowed per pass.")] = 5300,
    max_bytes: Annotated[int, typer.Option(help="Bytes allowed per pass.")] = 8 * 1024**3,
    min_free_bytes: Annotated[
        int, typer.Option(help="Free space the chunk refuses to eat into.")
    ] = DEFAULT_MIN_FREE_BYTES,
    full_verify: Annotated[
        bool,
        typer.Option(
            "--full-verify",
            help="Rehash the entire corpus, not only what is new plus a sample.",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preflight and plan only. No requests, no writes.")
    ] = False,
) -> None:
    """Run one chunk of the production universe through every safety gate.

    Both passes, then a convergence check, then reconciliation — and a chunk is
    recorded complete only when all of them hold. A command exiting zero is not
    the same as a finished chunk: the first pass stores catalogues and the second
    turns them into documents, so a single run always leaves work behind.

    **One chunk per invocation, on purpose.** This never advances to the next
    offset. Whether the remaining chunks run unattended is a separate decision
    from whether one chunk is safe, and this command does not make it.
    """
    settings = _bootstrap()
    storage, control = _open_layout(settings)
    manifest = Manifest(control.manifest_path)
    marker = checkpoint_path(control, offset, limit)

    with _database(settings) as factory, session_scope(factory) as session:
        selections = select_universe(session, production=True, offset=offset, limit=limit)
    digest = selection_digest(selections)
    existing = load_checkpoint(marker)

    typer.echo(f"chunk         : offset {offset}, limit {limit}")
    typer.echo(f"selected      : {len(selections)} identities")
    typer.echo(f"selection sha : {digest}")
    if existing is not None:
        typer.echo(f"checkpoint    : {existing.stage} (started {existing.started_at})")

    try:
        guard_selection(existing, digest, offset)
        # Structural only. Recomputing every digest costs ~50 s at 2,300
        # artifacts and would reach a quarter-hour at full corpus size — three
        # times per chunk, more than the harvest itself. This still catches an
        # orphan, a missing file, a size mismatch, a stray temporary and a path
        # that contradicts its URL; digests are verified in full once the chunk
        # is otherwise done, below.
        preflight_corpus(storage, manifest, min_free_bytes=min_free_bytes, verify_digests=False)
    except SweepError as exc:
        typer.echo(f"STOPPED: {exc}")
        raise typer.Exit(code=1) from exc

    before = _echo_free(storage)

    if dry_run:
        typer.echo("dry run — preflight passed, no requests issued and nothing written")
        return

    checkpoint = existing or Checkpoint(
        offset=offset, limit=limit, selection_digest=digest, selected=len(selections)
    )
    checkpoint.free_before = before
    if not checkpoint.records_before:
        # Where the log stood before this chunk touched it. Recorded once and
        # never recomputed, so a resumed chunk still verifies everything its
        # first attempt wrote rather than only what the resumption added.
        checkpoint.records_before = sum(1 for _ in manifest.entries())
    save_checkpoint(marker, checkpoint)

    for name, stage in (("pass1", ChunkStage.PASS1_DONE), ("pass2", ChunkStage.PASS2_DONE)):
        typer.echo(f"--- {name} ---")
        report = _sweep_pass(
            settings, storage, manifest, selections, filings_per_security, max_requests, max_bytes
        )
        typer.echo(f"  {report.summary()}")
        checkpoint.requests[name] = report.requests_made
        checkpoint.bytes_stored[name] = report.bytes_written
        checkpoint.stage = stage
        save_checkpoint(marker, checkpoint)
        state = reconcile(storage, manifest, verify_digests=False)
        if not state.clean:
            checkpoint.stage = ChunkStage.FAILED
            checkpoint.notes.append(f"{name} reconciliation failed: {state.problems}")
            save_checkpoint(marker, checkpoint)
            typer.echo(f"STOPPED: reconciliation failed after {name}: {state.problems}")
            raise typer.Exit(code=1)

    typer.echo("--- convergence ---")
    final = _sweep_pass(
        settings, storage, manifest, selections, filings_per_security, max_requests, max_bytes
    )
    converged = final.bytes_written == 0
    typer.echo(f"  {final.summary()}")
    checkpoint.stage = ChunkStage.CONVERGED if converged else ChunkStage.FAILED
    if not converged:
        checkpoint.notes.append("convergence pass still produced new evidence")
        save_checkpoint(marker, checkpoint)
        typer.echo("STOPPED: the chunk did not converge — more than two passes are needed")
        raise typer.Exit(code=1)

    typer.echo("--- verification ---")
    verified = reconcile(storage, manifest, verify_digests=full_verify)
    typer.echo(f"  {verified.summary()}")
    if not verified.clean:
        checkpoint.stage = ChunkStage.FAILED
        checkpoint.notes.append(f"verification failed: {verified.problems}")
        save_checkpoint(marker, checkpoint)
        typer.echo(f"STOPPED: verification failed: {verified.problems}")
        raise typer.Exit(code=1)
    checkpoint.accounting = {
        "trusted": verified.trusted,
        "submissions": verified.submissions,
        "filings": verified.filings,
        "manifest_records": verified.manifest_records,
    }

    # Structural reconciliation above covers the whole corpus every chunk. What
    # it does not do is re-read the bytes, and rehashing 9,000 artifacts already
    # costs 25 minutes and grows with every chunk — 28 more of those is longer
    # than the fetching. So the digests are checked in two tiers instead:
    # everything this chunk wrote, and a reproducible slice of everything older.
    if full_verify:
        checkpoint.verification = {"full_corpus": verified.trusted}
    else:
        fresh_urls = new_urls_since(manifest, checkpoint.records_before)
        latest = manifest.latest_by_url()
        trusted = [entry for entry in latest.values() if entry.is_stored]
        fresh = [entry for entry in trusted if entry.url in fresh_urls]
        sample = prior_sample(trusted, exclude=fresh_urls)
        results = {
            "new": verify_digests_of(storage, fresh),
            "sample": verify_digests_of(storage, sample),
        }
        for tier, result in results.items():
            typer.echo(f"  {tier}: " + ", ".join(f"{k}={v}" for k, v in sorted(result.items())))
        failures = {
            f"{tier}.{k}": v
            for tier, result in results.items()
            for k, v in result.items()
            if k != "checked" and v
        }
        if failures:
            checkpoint.stage = ChunkStage.FAILED
            checkpoint.notes.append(f"digest verification failed: {failures}")
            save_checkpoint(marker, checkpoint)
            typer.echo(f"STOPPED: digest verification failed: {failures}")
            raise typer.Exit(code=1)
        checkpoint.verification = {
            "new_hashed": results["new"]["checked"],
            "prior_sampled": results["sample"]["checked"],
            "prior_population": len(trusted) - len(fresh_urls),
        }
        typer.echo(
            "  sample rule   : sorted by (sha256, url), even stride, "
            f"max(500, 5%) = {len(sample)} of {len(trusted) - len(fresh_urls)}"
        )

    checkpoint.free_after = _local_free(storage)
    checkpoint.completed_at = datetime.now(UTC).isoformat()
    checkpoint.stage = ChunkStage.COMPLETE
    save_checkpoint(marker, checkpoint)
    typer.echo(f"CHUNK COMPLETE — checkpoint {marker}")


def _open_layout(settings: Settings) -> tuple[EvidenceStorage, EvidenceControlRoot]:
    """Choose the artifact backend once and open both planes.

    `open_evidence_layout` is the single place a backend is constructed from
    configuration; everything after this line takes an `EvidenceStorage` and
    cannot tell a directory from a bucket. The layout is printed every time,
    because a control root that shares one failure domain with the artifacts
    is a fact the operator should keep seeing.
    """
    try:
        layout = open_evidence_layout(settings)
    except ControlPlaneUnavailableError as exc:
        typer.echo(f"STOPPED: {exc}")
        raise typer.Exit(code=1) from exc
    storage, control = layout.storage, layout.control
    defaulted = " (defaulted — set SEC_STORAGE_BACKEND)" if layout.backend_defaulted else ""
    typer.echo(f"artifact backend : {layout.backend}{defaulted}")
    if isinstance(storage, LocalEvidenceStorage):
        typer.echo(f"artifact root    : {storage.root}")
    if control.colocated:
        typer.echo(f"control root     : {control.path} — LEGACY: colocated with the artifacts")
    else:
        typer.echo(f"control root     : {control.path}")
        if isinstance(storage, LocalEvidenceStorage) and same_volume(control.path, storage.root):
            typer.echo("WARNING          : control root shares a volume with the artifact root")
    typer.echo(f"manifest         : {control.manifest_path}")
    typer.echo(f"checkpoints      : {control.checkpoints}")
    return storage, control


def _local_free(storage: object) -> int:
    """Free bytes on a backend that has a local volume; zero for one that does not."""
    return storage.free_bytes() if isinstance(storage, LocalCapacity) else 0


def _echo_free(storage: object) -> int:
    """Print and return the local free space, when the backend has any to report."""
    free = _local_free(storage)
    if isinstance(storage, LocalCapacity):
        typer.echo(f"artifact free    : {free / 1024**3:.1f} GiB")
    return free


def _sweep_pass(
    settings: Settings,
    storage: EvidenceStorage,
    manifest: Manifest,
    selections: Sequence[Selection],
    filings_per_security: int,
    max_requests: int,
    max_bytes: int,
) -> HarvestReport:
    """One harvest pass over an already-selected chunk."""
    budget = Budget(max_requests=max_requests, max_bytes=max_bytes)
    resolver = build_observation_source(settings, meter=budget)
    try:
        resolutions = resolve_ciks(selections, resolver)
    finally:
        resolver.close()
    targets = submissions_targets(resolutions)
    targets += filing_targets(storage, resolutions, filings_per_security=filings_per_security)
    documents = _sec_documents(settings, budget)
    try:
        # A sweep pass always fetches; the command's own --dry-run returns before
        # reaching here. `resume` governs cache trust inside `harvest`, which is
        # its default behaviour, so there is nothing to pass through.
        return harvest_artifacts(targets, storage, manifest, documents, budget=budget)
    finally:
        documents.close()


def _sec_documents(settings: Settings, budget: Budget) -> SecDocuments:
    """Build the SEC document client, metered by the run's budget."""
    if not settings.sec_user_agent.strip():
        typer.echo("SEC_USER_AGENT is required: name the application and a real contact address.")
        raise typer.Exit(code=1)
    return SecDocuments(settings.sec_user_agent, meter=budget)


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


def _echo_progress(result: DateResult, index: int, total: int) -> None:
    """Print one backfilled session as it finishes.

    A backfill runs for hours. Printing each day as it lands is what makes it
    watchable rather than a process that either finishes or does not.
    """
    typer.echo(
        f"  [{index}/{total}] {result.session_date} {result.status.value} "
        f"{result.rows} rows in {result.seconds:.1f}s"
    )


def _report_gaps(missing: Sequence[date]) -> None:
    """Print the requested sessions that still hold no rows."""
    if not missing:
        typer.echo("  no gaps: every requested session holds rows")
        return
    shown = ", ".join(str(day) for day in missing[:10])
    more = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
    typer.echo(f"  gaps: {len(missing)} sessions hold no rows — {shown}{more}")


def _read_cohort(path: Path) -> list[CohortEntry]:
    """Read a cohort of delisted securities from a JSON Lines file.

    Args:
        path: The file to read. One JSON object per line.

    Returns:
        The entries, in file order.

    Raises:
        ProviderInvalidRequestError: If a line is not an object carrying the
            required identity fields. Refused at the door rather than halfway
            through a run that has already spent requests.
    """
    entries: list[CohortEntry] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        missing = [key for key in ("cik", "ticker", "name", "start", "end") if key not in payload]
        if missing:
            message = f"{path}:{number} is missing {', '.join(missing)}"
            raise ProviderInvalidRequestError(message)
        status = payload.get("asserted_status")
        entries.append(
            CohortEntry(
                cik=int(payload["cik"]),
                ticker=str(payload["ticker"]),
                name=str(payload["name"]),
                start=date.fromisoformat(payload["start"]),
                end=date.fromisoformat(payload["end"]),
                exchange=payload.get("exchange"),
                asserted_status=DelistingReason(status) if status else None,
                asserted_terminal_value=payload.get("asserted_terminal_value"),
                asserted_terminal_source=payload.get("asserted_terminal_source"),
            )
        )
    return entries


def _write_outcomes_csv(rows: Sequence[OutcomeRow], output: Path) -> None:
    """Write one decision date's mixed outcomes to a CSV a spreadsheet can read."""
    columns = [
        "identity_kind",
        "identity_id",
        "ticker",
        "ticker_is_dated",
        "horizon_sessions",
        "status",
        "entry_date",
        "entry_price",
        "exit_date",
        "exit_price",
        "total_return",
        "last_seen_date",
        "last_seen_price",
        "terminal_status",
        "terminal_value_per_share",
        "terminal_source",
    ]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            terminal = row.terminal
            writer.writerow(
                [
                    row.identity_kind,
                    row.identity_id,
                    row.ticker,
                    row.ticker_is_dated,
                    row.horizon_sessions,
                    row.status,
                    row.entry_date,
                    row.entry_price,
                    row.exit_date,
                    row.exit_price,
                    row.total_return,
                    row.last_seen_date,
                    row.last_seen_price,
                    None if terminal is None else terminal.status,
                    None if terminal is None else terminal.value_per_share,
                    None if terminal is None else terminal.source,
                ]
            )


def _write_contamination_csv(report: ContaminationReport, output: Path) -> None:
    """Write the contaminated live rows to a CSV a spreadsheet can read.

    One row per contaminating handover, so a study can exclude exactly these
    `company_id`s — or exclude only the dates on one side of a boundary —
    without re-deriving the scan.
    """
    columns = [
        "company_id",
        "ticker",
        "name",
        "status",
        "issuers",
        "from_issuer",
        "to_issuer",
        "gap_start",
        "gap_end",
        "gap_days",
        "bars_before",
        "bars_after",
    ]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in report.contaminated:
            for handover in row.handovers:
                writer.writerow(
                    [
                        row.company_id,
                        row.ticker,
                        row.name,
                        row.status,
                        " | ".join(row.issuers),
                        handover.from_issuer,
                        handover.to_issuer,
                        handover.gap_start,
                        handover.gap_end,
                        handover.gap_days,
                        handover.bars_before,
                        handover.bars_after,
                    ]
                )


def _write_coverage_csv(coverages: Sequence[DateCoverage], output: Path) -> None:
    """Write the per-date funnel to a CSV a spreadsheet can read."""
    columns = [
        "as_of",
        "in_record",
        "universe_members",
        "universe_observed",
        "universe_baseline",
        "universe_undetermined",
        "listed_on_date",
        "sufficient_price_history",
        "fundamentals_available",
        "market_cap_available",
        "score_reconstructable",
        "signals_available",
        "forward_returns_available",
        "recorded_exits",
        "terminal_outcomes_available",
        "interrupted",
        "fully_usable",
        "SCORED",
        "INSUFFICIENT_DATA",
        "NOT_ELIGIBLE",
        "UNSUPPORTED_SECTOR",
        "ERROR",
    ]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for coverage in coverages:
            writer.writerow(
                [
                    coverage.as_of,
                    coverage.in_record,
                    coverage.universe_members,
                    coverage.universe_observed,
                    coverage.universe_baseline,
                    coverage.universe_undetermined,
                    coverage.listed_on_date,
                    coverage.sufficient_price_history,
                    coverage.fundamentals_available,
                    coverage.market_cap_available,
                    coverage.score_reconstructable,
                    coverage.signals_available,
                    coverage.forward_returns_available,
                    coverage.recorded_exits,
                    coverage.terminal_outcomes_available,
                    coverage.interrupted,
                    coverage.fully_usable,
                    *(coverage.by_status.get(name, 0) for name in columns[-5:]),
                ]
            )


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
