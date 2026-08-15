import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from api_clients import (
    AlpacaMarketData,
    CompositeFundamentals,
    FmpFundamentals,
    MockFundamentals,
    MockMarketData,
    SecEdgarFundamentals,
)
from stock_screener.config import FundamentalsProviderName, Settings
from stock_screener.providers import (
    ConfigurationError,
    build_fundamentals_provider,
    build_market_data_provider,
    build_profile_provider,
)

FIXTURE = {"companies": [{"ticker": "XYZ", "name": "Example Corp", "exchange": "NASDAQ"}]}


@pytest.fixture
def fixture_file(tmp_path: Path) -> Path:
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return path


@pytest.mark.unit
def test_the_mock_market_data_provider_is_built_from_the_fixture(fixture_file: Path) -> None:
    settings = Settings(market_data_provider="mock", fixture_path=str(fixture_file))

    provider = build_market_data_provider(settings)

    assert isinstance(provider, MockMarketData)
    assert [p.ticker for p in provider.get_stock_universe()] == ["XYZ"]


@pytest.mark.unit
def test_the_mock_fundamentals_provider_is_built_from_the_fixture(fixture_file: Path) -> None:
    settings = Settings(fundamentals_provider="mock", fixture_path=str(fixture_file))

    assert isinstance(build_fundamentals_provider(settings), MockFundamentals)


@pytest.mark.unit
def test_a_missing_fixture_file_names_the_setting_to_change(tmp_path: Path) -> None:
    settings = Settings(market_data_provider="mock", fixture_path=str(tmp_path / "absent.json"))

    with pytest.raises(ConfigurationError, match="FIXTURE_PATH"):
        build_market_data_provider(settings)


@pytest.mark.unit
def test_alpaca_is_built_when_credentials_are_present() -> None:
    settings = Settings(
        market_data_provider="alpaca",
        alpaca_api_key=SecretStr("key"),
        alpaca_secret_key=SecretStr("secret"),
    )

    assert isinstance(build_market_data_provider(settings), AlpacaMarketData)


@pytest.mark.unit
@pytest.mark.parametrize(
    "credentials",
    [{}, {"alpaca_api_key": SecretStr("key")}, {"alpaca_secret_key": SecretStr("secret")}],
    ids=["neither", "no-secret", "no-key"],
)
def test_selecting_alpaca_without_both_credentials_fails_at_startup(
    credentials: dict[str, SecretStr],
) -> None:
    # Failing here, by name, beats discovering it three thousand tickers into a
    # nightly run.
    settings = Settings(market_data_provider="alpaca", **credentials)  # type: ignore[arg-type]

    with pytest.raises(ConfigurationError, match="ALPACA_API_KEY and ALPACA_SECRET_KEY"):
        build_market_data_provider(settings)


@pytest.mark.unit
def test_fmp_is_built_when_a_key_is_present() -> None:
    settings = Settings(
        fundamentals_provider="fmp",
        fundamentals_api_key=SecretStr("key"),
    )

    assert isinstance(build_fundamentals_provider(settings), FmpFundamentals)


@pytest.mark.unit
def test_selecting_fmp_without_a_key_fails_at_startup() -> None:
    settings = Settings(fundamentals_provider="fmp")

    with pytest.raises(ConfigurationError, match="FUNDAMENTALS_API_KEY"):
        build_fundamentals_provider(settings)


@pytest.mark.unit
def test_a_configuration_error_does_not_quote_the_credential() -> None:
    settings = Settings(
        market_data_provider="alpaca",
        alpaca_api_key=SecretStr("super-secret-key"),
    )

    with pytest.raises(ConfigurationError) as exc_info:
        build_market_data_provider(settings)

    assert "super-secret-key" not in str(exc_info.value)


@pytest.mark.unit
def test_edgar_requires_a_contact_string() -> None:
    # The SEC blocks anonymous requests, so this must fail at startup rather
    # than as a wall of 403s partway through a scan.
    settings = Settings(fundamentals_provider="edgar", sec_user_agent="")

    with pytest.raises(ConfigurationError, match="SEC_USER_AGENT"):
        build_fundamentals_provider(settings)


@pytest.mark.unit
def test_edgar_is_built_when_a_contact_string_is_present() -> None:
    settings = Settings(
        fundamentals_provider="edgar", sec_user_agent="Compounder Radar you@example.com"
    )

    assert isinstance(build_fundamentals_provider(settings), SecEdgarFundamentals)


@pytest.mark.unit
def test_the_combined_provider_needs_both_an_fmp_key_and_a_contact_string() -> None:
    settings = Settings(
        fundamentals_provider="edgar+fmp",
        sec_user_agent="Compounder Radar you@example.com",
        fundamentals_api_key=SecretStr("key"),
    )

    assert isinstance(build_fundamentals_provider(settings), CompositeFundamentals)


@pytest.mark.unit
def test_the_combined_provider_fails_without_the_fmp_key() -> None:
    settings = Settings(
        fundamentals_provider="edgar+fmp", sec_user_agent="Compounder Radar you@example.com"
    )

    with pytest.raises(ConfigurationError, match="FUNDAMENTALS_API_KEY"):
        build_fundamentals_provider(settings)


@pytest.mark.unit
@pytest.mark.parametrize("provider", ["fmp", "edgar+fmp"])
def test_the_profile_provider_is_the_vendor_alone(provider: FundamentalsProviderName) -> None:
    # Not the composite. The composite swallows a rate-limit error and falls
    # back to EDGAR, which during enrichment would turn a quota rejection into
    # a lookup that merely returned no market cap — and the pass would work
    # through every remaining candidate into the same wall.
    settings = Settings(
        environment="test",
        fundamentals_provider=provider,
        fundamentals_api_key=SecretStr("key"),
        sec_user_agent="Compounder Radar test@example.com",
    )

    built = build_profile_provider(settings)

    assert isinstance(built, FmpFundamentals)


@pytest.mark.unit
@pytest.mark.parametrize("provider", ["edgar", "mock"])
def test_a_provider_without_vendor_data_has_no_profile_source(
    provider: FundamentalsProviderName,
) -> None:
    settings = Settings(
        environment="test",
        fundamentals_provider=provider,
        sec_user_agent="Compounder Radar test@example.com",
    )

    assert build_profile_provider(settings) is None
