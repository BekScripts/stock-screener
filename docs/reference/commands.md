---
type: reference
title: Commands
---

# Commands

Every `make` target. Each wraps `uv run`, so the workspace environment is always
used.

| Target | Runs |
| --- | --- |
| `make setup` | `uv sync --all-packages --all-groups`, links agent skills, creates `.env` |
| `make sync` | `uv sync --all-packages --all-groups` |
| `make check` | `lint`, `format-check`, `types`, `test` |
| `make lint` | `ruff check .` |
| `make format` | `ruff format .` then `ruff check --fix .` |
| `make format-check` | `ruff format --check .` |
| `make types` | `mypy` over `src`, `tests`, and any packages/scripts present |
| `make test` | `pytest --cov --cov-report=term-missing` |
| `make test-unit` | `pytest -m unit -q` |
| `make migrate` | `alembic upgrade head` |
| `make migrate-down` | `alembic downgrade -1` |
| `make scan` | `stock-screener run-scan` |
| `make cov` | `pytest --cov --cov-report=html` |
| `make docs` | `mkdocs serve` |
| `make docs-build` | `mkdocs build --strict` |
| `make sync-skills` | Symlinks `.agents/skills/` into `.claude/skills/` |
| `make clean` | Removes caches and build artifacts |

`make check` is what CI runs. Every check in CI is reachable through it.

## CLI commands

Installed as `stock-screener`; also reachable as `python -m stock_screener`.

| Command | Does |
| --- | --- |
| `update-universe` | Refreshes the company list from the market-data provider. |
| `update-market` | Refreshes daily OHLCV for stored companies, resuming from the last stored session. |
| `update-fundamentals` | Refreshes profiles and quarterly statements, skipping companies already current. |
| `scan` | Screens stored data and prints the eligible companies. Calculates no score. |
| `run-scan` | The three update commands followed by `scan`. |

| Option | Applies to | Effect |
| --- | --- | --- |
| `--ticker` / `-t` | the three update commands, `scan` | Restrict to a symbol. Repeatable. |
| `--limit` / `-n` | `update-market`, `update-fundamentals` | Process at most this many companies, in ticker order. Bounds a first run against a metered provider. |
| `--force` | `update-fundamentals` | Re-store every period even when the provider has nothing newer. The incremental skip compares reporting dates and cannot tell that the adapter changed, so this is what picks up a normalisation fix. |
| `--output` / `-o` | `scan`, `run-scan` | Write every scanned company to a CSV, including exclusions. |
| `--all` | `scan` | Show excluded companies and their reasons in the table. |
| `--limit` / `-n` | `scan`, `run-scan` | Cap the printed rows. |

Every command is idempotent: a second run updates rows rather than duplicating
them. A per-ticker provider failure is logged and counted, and does not end the
run.

Exit codes: `0` on success, `2` when a selected provider is missing its
credentials.

## Scripts

Standalone tools, run with `uv` and not part of the deployable.

| Script | Purpose |
| --- | --- |
| `scripts/verify_fundamentals.py` | Checks a fundamentals provider's live response against the fields, units and sign conventions the adapter assumes. Read-only. Exits 0 when everything is present and plausible, 1 on a mapping problem, 2 when the provider is unreachable. |

```bash
uv run scripts/verify_fundamentals.py --ticker AAPL --expected-market-cap 3.4e12
```

## Test markers

| Marker | Selects |
| --- | --- |
| `unit` | Pure logic; no network, database, or filesystem |
| `integration` | Touches a real dependency |
| `slow` | Takes over a second |

```bash
uv run pytest -m unit
uv run pytest -m "not slow"
```
