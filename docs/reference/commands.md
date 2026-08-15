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

### Ingestion and screening

| Command | Does |
| --- | --- |
| `update-universe` | Refreshes the company list from the market-data provider. |
| `update-market` | Refreshes daily OHLCV for stored companies, resuming from the last stored session. |
| `update-benchmark` | Refreshes the benchmark's price history. Relative strength cannot be calculated without it. |
| `update-fundamentals` | Refreshes profiles and quarterly statements, skipping companies already current. |
| `scan` | Screens stored data and prints the eligible companies. Calculates no score. |
| `run-scan` | The universe, market and fundamentals updates followed by `scan`. |

### Scoring and rankings

| Command | Does |
| --- | --- |
| `score` | Scores every stored company and saves one snapshot per company for the day. Reads only stored data; calls no provider. Produces `PRELIMINARY` rows. |
| `enrich` | Spends one metered request per top candidate to verify market cap and consolidated volume, then re-scores them as `FINAL`. Stops when the quota runs out. |
| `rankings` | Prints Top Opportunities from the most recent scoring run. |
| `hidden-gems` | Small, fast-growing companies that already score well. |
| `wrong-price` | Great Company, Wrong Price — strong businesses whose valuation score is poor. |
| `improving` | The companies whose score has risen most over the window. |
| `explain` | One company's latest score, metric by metric. Takes a ticker. |
| `run-daily` | The whole nightly job: every update, then `score`, then `enrich`, then the ranking. |

| Option | Applies to | Effect |
| --- | --- | --- |
| `--ticker` / `-t` | the update commands, `scan`, `score` | Restrict to a symbol. Repeatable. |
| `--limit` / `-n` | `update-market`, `update-fundamentals`, `score` | Process at most this many companies, in ticker order. Bounds a first run against a metered provider. |
| `--force` | `update-fundamentals` | Re-store every period even when the provider has nothing newer. The incremental skip compares reporting dates and cannot tell that the adapter changed, so this is what picks up a normalisation fix. |
| `--output` / `-o` | `scan`, `run-scan`, the ranking commands | Write the result to a CSV. |
| `--all` | `scan` | Show excluded companies and their reasons in the table. |
| `--limit` / `-n` | `scan`, `run-scan`, the ranking commands | Cap the printed rows. Rankings default to 50. |
| `--dry-run` | `score` | Calculate without saving, to inspect a formula change before it enters the score history. |
| `--limit` / `-n` | `enrich` | Candidates to enrich. Defaults to `FMP_ENRICHMENT_LIMIT`. |
| `--preview` | `score` | How many top-scoring companies to print after scoring. Default 10. |
| `--min-score` | `rankings` | Only companies at or above this final score. |
| `--window` | `improving` | Days to compare back over. Default 30. |
| `--min-change` | `improving` | Minimum improvement, in score points. |

Every command is idempotent: a second run updates rows rather than duplicating
them, including `score`, whose rows are unique per company, day and formula
version. A per-ticker provider failure is logged and counted, and does not end
the run.

Exit codes: `0` on success, `1` when `explain` has no stored score for the
ticker, `2` when a selected provider is missing its credentials.

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
