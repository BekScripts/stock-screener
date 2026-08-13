import logging

import pytest
import structlog

from stock_screener.config import Settings
from stock_screener.logging import configure_logging


@pytest.fixture(autouse=True)
def reset_structlog() -> None:
    structlog.reset_defaults()


@pytest.mark.unit
def test_configures_console_renderer_by_default() -> None:
    configure_logging(Settings())

    processors = structlog.get_config()["processors"]

    assert isinstance(processors[-1], structlog.dev.ConsoleRenderer)


@pytest.mark.unit
def test_configures_json_renderer_when_requested() -> None:
    configure_logging(Settings(log_json=True))

    processors = structlog.get_config()["processors"]

    assert isinstance(processors[-1], structlog.processors.JSONRenderer)


@pytest.mark.unit
def test_applies_the_configured_level() -> None:
    configure_logging(Settings(log_level="ERROR"))

    assert logging.getLogger().level == logging.ERROR


@pytest.mark.unit
def test_emits_structured_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(log_json=True))

    structlog.get_logger(__name__).info("thing happened", widget_id=7)

    captured = capsys.readouterr().out
    assert '"event": "thing happened"' in captured
    assert '"widget_id": 7' in captured
