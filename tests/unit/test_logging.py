import logging

import pytest
import structlog

from stock_screener.config import Settings
from stock_screener.logging import (
    REDACTED,
    CredentialRedactingFilter,
    configure_logging,
    redact_credentials,
)


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


@pytest.mark.unit
@pytest.mark.parametrize(
    "parameter",
    ["apikey", "api_key", "key", "token", "access_token", "secret", "password", "signature"],
)
def test_redacts_every_credential_query_parameter(parameter: str) -> None:
    redacted = redact_credentials(f"https://example.test/profile?symbol=MSFT&{parameter}=s3cret")

    assert "s3cret" not in redacted
    assert f"{parameter}={REDACTED}" in redacted


@pytest.mark.unit
def test_redaction_keeps_the_rest_of_the_url_readable() -> None:
    redacted = redact_credentials(
        "https://financialmodelingprep.com/stable/profile?symbol=MSFT&apikey=nDLux&limit=5"
    )

    assert redacted == (
        f"https://financialmodelingprep.com/stable/profile?symbol=MSFT&apikey={REDACTED}&limit=5"
    )


@pytest.mark.unit
def test_redaction_does_not_truncate_a_parameter_that_merely_starts_with_a_short_name() -> None:
    redacted = redact_credentials("https://example.test/x?signature=abc")

    assert redacted == f"https://example.test/x?signature={REDACTED}"


@pytest.mark.unit
def test_redaction_leaves_text_without_a_credential_unchanged() -> None:
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json"

    assert redact_credentials(url) == url


def _httpx_record(*args: object) -> logging.LogRecord:
    """Build the record httpx emits for every request.

    Args:
        *args: The positional arguments httpx interpolates — the URL among them.

    Returns:
        An unformatted record, the state a filter actually sees.
    """
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: GET %s "%s"',
        args=args,
        exc_info=None,
    )


@pytest.mark.unit
def test_configure_logging_attaches_the_redactor_to_every_handler() -> None:
    configure_logging(Settings())

    handlers = logging.getLogger().handlers

    assert handlers
    assert all(
        any(isinstance(f, CredentialRedactingFilter) for f in handler.filters)
        for handler in handlers
    )


@pytest.mark.unit
def test_a_credential_in_a_url_argument_is_redacted() -> None:
    record = _httpx_record("https://x.test/p?apikey=nDLux", "HTTP/1.1 200 OK")

    CredentialRedactingFilter().filter(record)

    assert "nDLux" not in record.getMessage()
    assert f"apikey={REDACTED}" in record.getMessage()


@pytest.mark.unit
def test_a_credential_in_a_url_object_is_redacted() -> None:
    # httpx passes an `httpx.URL`, not a string. A filter that inspected only
    # `str` arguments skipped it, and the record was stringified at format time
    # — after every filter had run — so the key was logged in full.
    class Url:
        def __str__(self) -> str:
            return "https://x.test/p?apikey=nDLux"

    record = _httpx_record(Url(), "HTTP/1.1 200 OK")

    CredentialRedactingFilter().filter(record)

    assert "nDLux" not in record.getMessage()
    assert f"apikey={REDACTED}" in record.getMessage()


@pytest.mark.unit
def test_a_credential_in_mapping_style_arguments_is_redacted() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="called %(url)s",
        args=({"url": "https://x.test/p?token=nDLux"},),
        exc_info=None,
    )

    CredentialRedactingFilter().filter(record)

    assert "nDLux" not in record.getMessage()
    assert f"token={REDACTED}" in record.getMessage()


@pytest.mark.unit
def test_a_credential_in_the_message_itself_is_redacted() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="calling https://x.test/p?secret=nDLux",
        args=None,
        exc_info=None,
    )

    CredentialRedactingFilter().filter(record)

    assert "nDLux" not in record.getMessage()
    assert f"secret={REDACTED}" in record.getMessage()


@pytest.mark.unit
def test_arguments_without_a_credential_survive_intact() -> None:
    record = _httpx_record("https://x.test/p?symbol=MSFT", "HTTP/1.1 200 OK")

    CredentialRedactingFilter().filter(record)

    assert record.getMessage() == 'HTTP Request: GET https://x.test/p?symbol=MSFT "HTTP/1.1 200 OK"'


@pytest.mark.unit
def test_the_filter_never_drops_a_record() -> None:
    record = _httpx_record("https://x.test/p?apikey=nDLux", "HTTP/1.1 200 OK")

    assert CredentialRedactingFilter().filter(record) is True
