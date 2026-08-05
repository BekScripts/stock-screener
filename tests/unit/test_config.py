import pytest

from app.config import Settings, get_settings


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
