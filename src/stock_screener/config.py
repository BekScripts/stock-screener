"""Typed application settings, loaded from the environment.

Settings are read once and cached. Never read `os.environ` directly elsewhere in
the codebase — add a field here instead so the value is typed, validated at
startup, and documented in `.env.example`.

Two groups of settings deserve a note.

**Eligibility thresholds** live here rather than as constants in the screening
code, because tuning them is the main thing this project does. Changing the
minimum market cap should be an environment variable, not a code change.

**Provider credentials** are `SecretStr`, so a settings object rendered into a
log line or a traceback prints `**********` instead of an API key. Both provider
selectors default to `mock`, which is what lets a fresh clone run the whole
pipeline before any credentials exist.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from domain import EligibilityThresholds, VolumeBasis

Environment = Literal["local", "test", "staging", "production"]
MarketDataProviderName = Literal["alpaca", "mock"]
FundamentalsProviderName = Literal["edgar", "edgar+fmp", "fmp", "mock"]
FxProviderName = Literal["ecb", "ecb+fallback", "none", "mock"]
ResearchProviderName = Literal["anthropic", "mock"]
ExternalResearchProviderName = Literal["tavily", "mock", "none"]
DeepResearchProviderName = Literal["anthropic", "mock"]


class Settings(BaseSettings):
    """Runtime configuration for the application.

    Values come from environment variables, falling back to a local `.env` file.
    Field names map to upper-case env vars, e.g. `log_level` -> `LOG_LEVEL`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    environment: Environment = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = Field(
        default=False,
        description="Emit machine-readable JSON logs. Enable outside local dev.",
    )

    # -- database ---------------------------------------------------------

    database_url: str = Field(
        default="sqlite:///./compounder_radar.db",
        description=(
            "SQLAlchemy database URL. SQLite is the default so a clone runs with "
            "no setup; PostgreSQL is the intended production database."
        ),
    )

    # -- providers --------------------------------------------------------

    market_data_provider: MarketDataProviderName = Field(
        default="mock",
        description="Which market-data adapter to use. `alpaca` requires credentials.",
    )
    fundamentals_provider: FundamentalsProviderName = Field(
        default="mock",
        description=(
            "Which fundamentals adapter to use. `edgar` is free and covers every "
            "U.S. filer but has no market cap or sector, so the screen excludes "
            "everything for missing data; `edgar+fmp` takes statements from EDGAR "
            "and the profile from FMP, which is the combination that works on a "
            "free FMP plan. `fmp` alone requires a key and a plan that covers the "
            "symbols being scanned."
        ),
    )
    sec_user_agent: str = Field(
        default="",
        description=(
            "Contact string sent to the SEC on every request, e.g. "
            "'Compounder Radar you@example.com'. The SEC requires it and blocks "
            "requests without one. Required when the provider involves EDGAR."
        ),
    )
    fixture_path: str = Field(
        default="fixtures/sample_universe.json",
        description="Fixture file the mock providers read when selected.",
    )

    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    alpaca_trading_base_url: str = Field(
        default="https://api.alpaca.markets",
        description=(
            "Alpaca trading host, which serves the asset list. Paper-trading "
            "keys (the ones beginning `PK`) must point at "
            "https://paper-api.alpaca.markets or every request returns 401."
        ),
    )
    alpaca_data_base_url: str = "https://data.alpaca.markets"
    alpaca_feed: Literal["iex", "sip"] = Field(
        default="iex",
        description=(
            "Which tape to read. `sip` is the consolidated tape and needs a paid "
            "data subscription; `iex` is what a free account can read, and it "
            "reports only that exchange's share of volume — roughly 2-4% of the "
            "real figure. See the liquidity note in docs/reference/metrics.md."
        ),
    )

    fundamentals_api_key: SecretStr | None = None
    fundamentals_base_url: str = "https://financialmodelingprep.com"
    fundamentals_api_root: str = Field(
        default="/stable",
        description=(
            "Path prefix before each FMP endpoint. FMP retired `/api/v3` for new "
            "keys, so this is configurable rather than hardcoded."
        ),
    )

    # -- provider behaviour -----------------------------------------------

    provider_max_attempts: int = Field(
        default=3, ge=1, description="Total tries per request, including the first."
    )
    provider_retry_backoff_seconds: float = Field(
        default=1.0, ge=0, description="Base backoff delay, doubled after each failure."
    )
    provider_min_request_interval_seconds: float = Field(
        default=0.2, ge=0, description="Minimum gap between provider requests."
    )
    provider_batch_size: int = Field(
        default=100, ge=1, description="Symbols per batched market-data request."
    )
    provider_timeout_seconds: float = Field(default=30.0, gt=0)

    # -- ingestion --------------------------------------------------------

    price_history_days: int = Field(
        default=400,
        ge=1,
        description=(
            "Calendar days of price history to keep. 400 covers a 52-week high "
            "and a twelve-month return with room for market holidays."
        ),
    )
    fundamentals_quarters: int = Field(
        default=20,
        ge=1,
        description=(
            "Quarters to request per company. Sixteen are needed for a 3-year CAGR "
            "and six for growth acceleration, but metered plans cap this — FMP's "
            "free tier allows five and rejects anything larger outright."
        ),
    )

    # -- scoring ----------------------------------------------------------

    fmp_enrichment_limit: int = Field(
        default=200,
        ge=0,
        description=(
            "How many top-ranked candidates the enrichment pass may spend "
            "metered requests on. The broad scan runs on Alpaca and EDGAR and "
            "needs none of these; enrichment buys a vendor market cap and "
            "consolidated volume for the companies a ranking actually shows. "
            "Set it to what the plan's daily allowance can serve — a free FMP "
            "tier is a few hundred requests a day."
        ),
    )

    # -- ai research -------------------------------------------------------

    research_provider: ResearchProviderName = Field(
        default="mock",
        description=(
            "Which model writes research reports. `mock` returns a canned draft "
            "and calls nothing, which is what lets the whole pipeline — brief, "
            "validation, persistence — be exercised without credentials or cost."
        ),
    )
    research_api_key: SecretStr | None = Field(
        default=None,
        description="Credential for the research provider. Required when it is not `mock`.",
    )
    research_model: str = Field(
        default="claude-sonnet-5",
        description=(
            "Model identifier passed to the research provider. Stored on every "
            "report, because a report written by one model is not evidence about "
            "what another would have said."
        ),
    )
    research_max_output_tokens: int = Field(
        default=8000,
        ge=1,
        description=(
            "Ceiling on one report's generated tokens. Bounds cost per company "
            "and, on a thinking model, must leave room for the reasoning as well "
            "as the thirteen sections — the cap covers both."
        ),
    )
    research_effort: Literal["low", "medium", "high"] = Field(
        default="medium",
        description=(
            "How hard the model works per report. This is the determinism and "
            "cost lever: current Claude models reject `temperature`, so a low "
            "sampling temperature is not expressible and effort plus a "
            "deterministic prompt is what replaces it."
        ),
    )
    research_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description=(
            "Per-request timeout. Generous because a thinking model writing "
            "thirteen sections is slow, and a timeout mid-report costs the "
            "tokens already spent."
        ),
    )
    # -- deep research synthesis (Phase 6D) ---------------------------------

    deep_research_provider: DeepResearchProviderName = Field(
        default="mock",
        description=(
            "Which model writes deep research reports. `mock` returns a canned "
            "draft and calls nothing, which is what lets the whole pipeline — "
            "brief, validation, persistence, cache — be exercised without cost."
        ),
    )
    deep_research_model: str = Field(
        default="claude-sonnet-5",
        description=(
            "Model identifier for deep research. Stored on every report, because "
            "a report written by one model is not evidence about what another "
            "would have said."
        ),
    )
    deep_research_max_input_tokens: int = Field(
        default=30_000,
        ge=1,
        description=(
            "Hard ceiling on the prompt a deep research run may send. A safety "
            "guard, never a truncation target: a brief that exceeds it aborts "
            "before the provider is called and reports its measured size. "
            "Silently dropping evidence to fit would produce a report whose "
            "gaps nobody could see."
        ),
    )
    deep_research_max_output_tokens: int = Field(
        default=8_000,
        ge=1,
        description=(
            "Ceiling on one deep report's generated tokens. On a thinking model "
            "this covers the reasoning as well as the seventeen sections."
        ),
    )
    deep_research_effort: Literal["low", "medium", "high"] = Field(
        default="medium",
        description=(
            "How hard the model works per deep report. The determinism and cost "
            "lever, as in Phase 3: current Claude models reject `temperature`, so "
            "effort plus a deterministic prompt is what replaces it."
        ),
    )
    deep_research_timeout_seconds: float = Field(
        default=180.0,
        gt=0,
        description=(
            "Per-request timeout. Longer than Phase 3's because seventeen "
            "sections over a wider evidence set is slower, and a timeout "
            "mid-report costs the tokens already spent."
        ),
    )
    deep_research_external_cache_hours: float = Field(
        default=6.0,
        gt=0,
        description=(
            "How long a company's collected external evidence may be reused "
            "before it is searched for again. Exists because a search engine "
            "returns a slightly different valid article set every few minutes: "
            "the fingerprint change is real, but paying a model to re-read "
            "substantially the same news is not worth it. Six hours keeps a "
            "morning and an afternoon run distinct while making a repeated "
            "request free."
        ),
    )
    deep_research_max_cost_usd: float = Field(
        default=1.0,
        gt=0,
        description=(
            "Estimated spend allowed for one company's deep research run. "
            "Checked against the measured input size before the request, so a "
            "run that would cross it never starts. Per company rather than per "
            "run, because deep research is asked for one ticker at a time."
        ),
    )

    # -- external research (Phase 6C) ---------------------------------------

    external_research_provider: ExternalResearchProviderName = Field(
        default="none",
        description=(
            "Where current external evidence comes from. `none` disables "
            "collection entirely, which is the default because deterministic "
            "preparation must never depend on it — a company with no external "
            "evidence is a legal brief. `mock` serves fixture results."
        ),
    )
    external_research_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Credential for the external research provider. Required unless the "
            "provider is `none` or `mock`."
        ),
    )
    external_research_base_url: str = Field(
        default="https://api.tavily.com",
        description="API host for the external research provider.",
    )
    external_research_window_days: int = Field(
        default=90,
        ge=1,
        description=(
            "How far back a search reaches, in days. Deep research is about what "
            "is true now, and an unbounded window fills the evidence set with "
            "history the deterministic layers already cover better."
        ),
    )
    external_research_max_items: int = Field(
        default=15,
        ge=1,
        description=(
            "Most external evidence items one brief may carry. The bound is a "
            "quality lever, not a storage one: five articles about one earnings "
            "release are worse evidence than five about five different events."
        ),
    )
    external_research_max_per_domain: int = Field(
        default=3,
        ge=1,
        description=(
            "Most items one publisher may contribute. Stops a single outlet's "
            "coverage crowding out the diversity of events the set exists for."
        ),
    )
    external_research_max_results: int = Field(
        default=20,
        ge=1,
        description=(
            "Results requested per search query, before filtering. Raising it "
            "costs nothing per search with most vendors but gives the tier and "
            "duplicate filters more to reject."
        ),
    )
    external_research_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Per-request timeout for the external research provider.",
    )

    research_max_run_cost_usd: float = Field(
        default=5.0,
        gt=0,
        description=(
            "Estimated spend allowed in one research run, in US dollars. The run "
            "stops before the request that could cross it; everything already "
            "written stays written. Five dollars is roughly three full passes "
            "over the current candidate set at current prices — high enough that "
            "an intended run finishes, low enough that a mistaken loop over the "
            "whole market does not. The estimate errs high and ignores "
            "introductory discounts, so the real bill lands under it."
        ),
    )

    fx_provider: FxProviderName = Field(
        default="ecb+fallback",
        description=(
            "Where exchange rates come from. Only used for companies that file "
            "in a currency other than the one their shares trade in; a domestic "
            "company needs no rate and triggers no request. `ecb` is the "
            "European Central Bank's official daily reference rates, free and "
            "keyless, covering thirty currencies. `ecb+fallback` adds a broad "
            "community dataset for the currencies the ECB does not publish — "
            "Taiwan's dollar among them, which is the currency TSM files in. "
            "`none` disables fetching and serves only rates already stored, "
            "which is the right setting for an offline run."
        ),
    )

    fx_max_rate_age_days: int = Field(
        default=5,
        ge=0,
        description=(
            "How many days before a score date an exchange rate may be dated "
            "and still be used. Three days covers a weekend and five covers a "
            "weekend with a holiday either side, which is the longest ordinary "
            "gap in a published fixing series. A longer window silently values "
            "a company on a rate from the far side of a real hole in the data."
        ),
    )

    benchmark_symbol: str = Field(
        default="SPY",
        min_length=1,
        description=(
            "Broad-market series relative strength is measured against. SPY is "
            "the default because it is the most liquid proxy for the U.S. market "
            "and comes from the same market-data provider as everything else. "
            "Changing it changes what every momentum score means, so re-score "
            "the market after changing it rather than comparing across the two."
        ),
    )

    # -- jobs ---------------------------------------------------------------

    jobs_enabled: bool = Field(
        default=True,
        description=(
            "Whether the API may start pipeline commands. The job endpoints let "
            "an unauthenticated caller run an hour-long ingest and spend money "
            "on research, which is only reasonable because the API binds "
            "localhost. Set this false before binding it anywhere else."
        ),
    )
    job_log_dir: str = Field(
        default=".jobs",
        min_length=1,
        description=(
            "Directory a spawned command's output is written to, one file per "
            "job. Relative paths resolve against the working directory."
        ),
    )

    # -- eligibility ------------------------------------------------------

    min_price: float = Field(default=2.0, gt=0)
    min_market_cap: float = Field(default=100_000_000.0, gt=0)
    min_avg_dollar_volume: float = Field(default=1_000_000.0, gt=0)
    min_trading_days: int = Field(
        default=20,
        gt=0,
        description=(
            "Sessions of history required before the liquidity figure counts. "
            "Below this a security is excluded as illiquid rather than admitted "
            "on a thin average."
        ),
    )

    @property
    def is_production(self) -> bool:
        """Whether the app is running in the production environment."""
        return self.environment == "production"

    @property
    def bar_volume_basis(self) -> VolumeBasis:
        """What the stored price bars' volume represents.

        A property of the configured feed rather than of the bars themselves.
        Re-ingesting after changing `ALPACA_FEED` mixes two bases in one table,
        so change the feed and re-fetch together.

        Returns:
            `CONSOLIDATED` for the SIP tape, `PARTIAL` for a single exchange,
            and `UNKNOWN` when the bars came from fixtures.
        """
        if self.market_data_provider == "alpaca":
            return VolumeBasis.CONSOLIDATED if self.alpaca_feed == "sip" else VolumeBasis.PARTIAL
        return VolumeBasis.UNKNOWN

    @property
    def eligibility_thresholds(self) -> EligibilityThresholds:
        """Return the screening thresholds as the domain model expects them.

        Returns:
            An `EligibilityThresholds` built from the configured minimums.
        """
        return EligibilityThresholds(
            min_price=self.min_price,
            min_market_cap=self.min_market_cap,
            min_avg_dollar_volume=self.min_avg_dollar_volume,
            min_trading_days=self.min_trading_days,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings.

    Returns:
        The process-wide `Settings` instance, constructed on first call.
    """
    return Settings()
