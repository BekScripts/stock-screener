"""Structured logging setup.

Call `configure_logging()` once at process start. Everywhere else, get a logger
with `structlog.get_logger(__name__)` and pass context as keyword arguments
rather than formatting it into the message.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from app.config import Settings


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
