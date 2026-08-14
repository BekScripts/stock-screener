"""Construct provider adapters from settings.

This is the only module that knows both which adapters exist and which one the
configuration asked for. Everything downstream takes a `MarketDataProvider` or a
`FundamentalsProvider` as an argument, so ingestion can be tested against a fake
without touching this file.

Missing credentials fail here, at startup, with a message naming the variable to
set. The alternative — discovering it three thousand tickers into a nightly run —
is the failure mode a typed configuration layer exists to prevent.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from api_clients import (
    AlpacaMarketData,
    CompositeFundamentals,
    FmpFundamentals,
    FundamentalsProvider,
    MarketDataProvider,
    RateLimiter,
    RetryPolicy,
    SecEdgarFundamentals,
    load_fixture_providers,
)

if TYPE_CHECKING:
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)


class ConfigurationError(RuntimeError):
    """A provider was selected without the configuration it needs."""


def _rate_limiter(settings: Settings) -> RateLimiter:
    """Build a rate limiter from the configured minimum request interval."""
    return RateLimiter(settings.provider_min_request_interval_seconds)


def _retry_policy(settings: Settings) -> RetryPolicy:
    """Build a retry policy from the configured attempts and backoff."""
    return RetryPolicy(
        attempts=settings.provider_max_attempts,
        backoff_seconds=settings.provider_retry_backoff_seconds,
    )


def build_market_data_provider(settings: Settings) -> MarketDataProvider:
    """Return the market-data adapter the configuration selects.

    Args:
        settings: Application settings.

    Returns:
        An adapter satisfying `MarketDataProvider`.

    Raises:
        ConfigurationError: If `alpaca` is selected without credentials, or the
            mock fixture file is missing or unreadable.
    """
    if settings.market_data_provider == "mock":
        market_data, _ = _load_fixtures(settings)
        return market_data

    if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
        raise ConfigurationError(
            "MARKET_DATA_PROVIDER=alpaca requires ALPACA_API_KEY and ALPACA_SECRET_KEY"
        )

    if settings.alpaca_feed == "iex":
        # Loud, because the consequence is silent: prices are fine, but volume
        # is a fraction of consolidated, so the liquidity screen will exclude
        # companies that actually trade well above the threshold.
        log.warning(
            "using the IEX feed: reported volume is a fraction of consolidated, "
            "so the liquidity filter will exclude genuinely liquid companies",
            min_avg_dollar_volume=settings.min_avg_dollar_volume,
        )

    log.debug("building market data provider", provider="alpaca")
    return AlpacaMarketData(
        api_key=settings.alpaca_api_key.get_secret_value(),
        secret_key=settings.alpaca_secret_key.get_secret_value(),
        trading_base_url=settings.alpaca_trading_base_url,
        data_base_url=settings.alpaca_data_base_url,
        timeout_seconds=settings.provider_timeout_seconds,
        limiter=_rate_limiter(settings),
        retry=_retry_policy(settings),
        batch_size=settings.provider_batch_size,
        feed=settings.alpaca_feed,
    )


def build_fundamentals_provider(settings: Settings) -> FundamentalsProvider:
    """Return the fundamentals adapter the configuration selects.

    Args:
        settings: Application settings.

    Returns:
        An adapter satisfying `FundamentalsProvider`.

    Raises:
        ConfigurationError: If the selected provider lacks the credentials or
            contact string it needs, or the mock fixture file is unreadable.
    """
    if settings.fundamentals_provider == "mock":
        _, fundamentals = _load_fixtures(settings)
        return fundamentals

    if settings.fundamentals_provider == "edgar":
        log.warning(
            "EDGAR supplies no market capitalisation, so every company will be "
            "excluded as missing required data; use edgar+fmp to fill the profile"
        )
        return _build_edgar(settings)

    if settings.fundamentals_provider == "edgar+fmp":
        # EDGAR for the statements it covers completely, FMP for the market cap
        # and sector it has for every symbol even on a free plan.
        return CompositeFundamentals(
            profile_source=_build_fmp(settings),
            statement_source=_build_edgar(settings),
        )

    return _build_fmp(settings)


def _build_edgar(settings: Settings) -> FundamentalsProvider:
    """Build the SEC EDGAR adapter.

    Raises:
        ConfigurationError: If no contact string is configured. The SEC blocks
            requests that do not identify the caller, so this fails at startup
            rather than as a wall of 403s.
    """
    if not settings.sec_user_agent.strip():
        raise ConfigurationError(
            "EDGAR requires SEC_USER_AGENT naming the application and a contact "
            "email, e.g. 'Compounder Radar you@example.com' — the SEC blocks "
            "requests without one"
        )

    log.debug("building fundamentals provider", provider="edgar")
    return SecEdgarFundamentals(
        user_agent=settings.sec_user_agent,
        timeout_seconds=settings.provider_timeout_seconds,
        retry=_retry_policy(settings),
    )


def _build_fmp(settings: Settings) -> FundamentalsProvider:
    """Build the FMP adapter.

    Raises:
        ConfigurationError: If no API key is configured.
    """
    if settings.fundamentals_api_key is None:
        raise ConfigurationError("this fundamentals provider requires FUNDAMENTALS_API_KEY")

    log.debug("building fundamentals provider", provider="fmp")
    return FmpFundamentals(
        api_key=settings.fundamentals_api_key.get_secret_value(),
        base_url=settings.fundamentals_base_url,
        api_root=settings.fundamentals_api_root,
        timeout_seconds=settings.provider_timeout_seconds,
        limiter=_rate_limiter(settings),
        retry=_retry_policy(settings),
    )


def _load_fixtures(settings: Settings) -> tuple[MarketDataProvider, FundamentalsProvider]:
    """Load both mock providers from the configured fixture file.

    Raises:
        ConfigurationError: If the fixture file is absent or malformed.
    """
    path = Path(settings.fixture_path)
    if not path.is_file():
        raise ConfigurationError(
            f"fixture file not found at {path}; set FIXTURE_PATH or select a real provider"
        )

    log.debug("loading fixture providers", path=str(path))
    market_data, fundamentals = load_fixture_providers(path)
    return market_data, fundamentals
