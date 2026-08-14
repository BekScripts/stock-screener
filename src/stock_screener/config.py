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
