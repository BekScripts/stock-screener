"""Console entry point.

Wired to the `stock-screener` script in pyproject.toml. Keep this thin: parse
input, build dependencies, delegate. Business logic belongs in modules, not here.

Everything is delegated to `cli.run()`, which owns argument parsing and turns a
configuration failure into an exit code.
"""

from __future__ import annotations

from stock_screener.cli import run


def main() -> int:
    """Run the application.

    Returns:
        A process exit code: 0 on success, non-zero on failure.
    """
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
