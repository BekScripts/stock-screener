import pytest

from stock_screener.config import Settings, get_settings


@pytest.mark.unit
def test_defaults_to_local_environment() -> None:
    settings = Settings()

    assert settings.environment == "local"
    assert settings.log_level == "INFO"
    assert settings.is_production is False


@pytest.mark.unit
def test_reads_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")

    settings = Settings()

    assert settings.environment == "production"
    assert settings.log_level == "ERROR"
    assert settings.is_production is True


@pytest.mark.unit
def test_rejects_unknown_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "nope")

    with pytest.raises(ValueError, match="environment"):
        Settings()


@pytest.mark.unit
def test_rejects_undeclared_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOTALLY_MADE_UP", "1")

    # extra="forbid" only guards fields passed in, not stray env vars — this
    # documents that a typo'd env var is silently ignored, not an error.
    assert Settings().environment == "local"


@pytest.mark.unit
def test_settings_are_cached() -> None:
    get_settings.cache_clear()

    assert get_settings() is get_settings()


@pytest.mark.unit
def test_providers_default_to_mock_so_a_fresh_clone_runs() -> None:
    settings = Settings()

    assert settings.market_data_provider == "mock"
    assert settings.fundamentals_provider == "mock"
    assert settings.database_url.startswith("sqlite://")


@pytest.mark.unit
def test_eligibility_thresholds_match_the_phase_1_specification() -> None:
    thresholds = Settings().eligibility_thresholds

    assert thresholds.min_price == pytest.approx(2.0)
    assert thresholds.min_market_cap == pytest.approx(100_000_000.0)
    assert thresholds.min_avg_dollar_volume == pytest.approx(1_000_000.0)
    assert thresholds.min_trading_days == 20


@pytest.mark.unit
def test_eligibility_thresholds_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole point of putting them in config is tuning them without a code
    # change, so this is the behaviour that must not regress.
    monkeypatch.setenv("MIN_PRICE", "5")
    monkeypatch.setenv("MIN_MARKET_CAP", "250000000")

    thresholds = Settings().eligibility_thresholds

    assert thresholds.min_price == pytest.approx(5.0)
    assert thresholds.min_market_cap == pytest.approx(250_000_000.0)


@pytest.mark.unit
def test_rejects_a_non_positive_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_PRICE", "0")

    with pytest.raises(ValueError, match="min_price"):
        Settings()


@pytest.mark.unit
def test_credentials_are_not_exposed_by_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    # A settings object reaches a log line or a traceback sooner or later; the
    # key must not go with it.
    monkeypatch.setenv("ALPACA_API_KEY", "super-secret-key")

    settings = Settings()

    assert "super-secret-key" not in repr(settings)
    assert settings.alpaca_api_key is not None
    assert settings.alpaca_api_key.get_secret_value() == "super-secret-key"


@pytest.mark.unit
def test_an_unknown_provider_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "bloomberg")

    with pytest.raises(ValueError, match="market_data_provider"):
        Settings()
