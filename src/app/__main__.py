"""Console entry point.

Wired to the `app` script in pyproject.toml. Keep this thin: parse input,
build dependencies, delegate. Business logic belongs in modules, not here.
"""

from __future__ import annotations

import structlog

from app.config import get_settings
from app.logging import configure_logging


def main() -> int:
    """Run the application.

    Returns:
        A process exit code: 0 on success, non-zero on failure.
    """
    settings = get_settings()
    configure_logging(settings)

    log = structlog.get_logger(__name__)
    log.info("application started", environment=settings.environment)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
