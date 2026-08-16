"""Structured logging setup.

Call `configure_logging()` once at process start. Everywhere else, get a logger
with `structlog.get_logger(__name__)` and pass context as keyword arguments
rather than formatting it into the message.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from stock_screener.config import Settings

REDACTED = "REDACTED"

# Query parameters whose value is a credential. `key` is deliberately broad:
# over-redacting a log line costs nothing, and the alternative is a provider
# naming its parameter something this set does not anticipate.
_CREDENTIAL_QUERY_PARAMS = (
    "access_token",
    "api_key",
    "apikey",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
)

# `\b` before the name stops `key=` matching inside `apikey=`, and the
# alternation is ordered longest-first so `signature=` is not truncated to
# `sig`. The value runs to the next separator, so the rest of the URL survives.
_CREDENTIAL_QUERY_RE = re.compile(
    r"\b(" + "|".join(_CREDENTIAL_QUERY_PARAMS) + r")=([^&\s\"'<>]+)",
    re.IGNORECASE,
)


def redact_credentials(text: str) -> str:
    """Replace the value of any credential-bearing query parameter.

    Args:
        text: Arbitrary text that may contain a URL with a credential in its
            query string.

    Returns:
        The same text with each credential value replaced by `REDACTED`. Text
        containing no such parameter is returned unchanged.
    """
    return _CREDENTIAL_QUERY_RE.sub(lambda match: f"{match.group(1)}={REDACTED}", text)


def _redact_argument(argument: object) -> object:
    """Redact one log argument, whatever type it arrives as.

    Args:
        argument: A positional or named argument attached to a log record.

    Returns:
        The redacted argument, or the original when it holds no credential.
    """
    if isinstance(argument, str):
        return redact_credentials(argument)

    if argument is None or isinstance(argument, (int, float, complex)):
        return argument

    # httpx logs the URL as an `httpx.URL`, not a string, and a record is only
    # stringified when it is formatted — long after every filter has run. An
    # object whose text form carries a credential has to be substituted here,
    # or the credential reaches the handler untouched.
    text = str(argument)
    redacted = redact_credentials(text)
    return redacted if redacted != text else argument


class CredentialRedactingFilter(logging.Filter):
    """Strip credentials out of log records before a handler writes them.

    `httpx` logs every request at `INFO`, URL included, so a provider that
    authenticates with a query parameter — FMP's `apikey` — puts its key in
    the log. `SecretStr` cannot help: by then the value is part of a URL a
    third-party library formatted. This filter is the last point before the
    record is written, which is why it belongs on the handler rather than on
    any one logger.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record in place.

        Args:
            record: The record about to be emitted. Mutated, not copied — a
                handler-level filter is the only thing that sees it.

        Returns:
            Always True. This filter rewrites records, it never drops them.
        """
        if isinstance(record.msg, str):
            record.msg = redact_credentials(record.msg)

        if isinstance(record.args, tuple):
            record.args = tuple(_redact_argument(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {name: _redact_argument(value) for name, value in record.args.items()}

        return True


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib logging bridge.

    Args:
        settings: Application settings providing the log level and output format.
    """
    # force=True replaces any handlers already installed. Without it,
    # basicConfig silently does nothing when something else configured logging
    # first (pytest, gunicorn, a library), and the level here is ignored.
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.log_level),
        force=True,
    )

    # After basicConfig, never before: force=True replaces the handler list, so
    # a filter attached earlier would be discarded with the handler holding it.
    redactor = CredentialRedactingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level)),
        cache_logger_on_first_use=True,
    )
