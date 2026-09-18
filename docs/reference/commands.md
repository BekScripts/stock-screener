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
| `make check-web` | In `frontend/`: `npm run typecheck`, `npm run lint`, `npm run build` |
| `make dev` | The API and the dashboard together; Ctrl-C stops both |
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

`make check` is what CI runs. Every Python check in CI is reachable through it.

`make check-web` is the frontend equivalent and is not a prerequisite of
`make check`: the Python gate runs without a node toolchain installed. It
requires Node and an `npm install` in `frontend/`.

## CLI commands

Installed as `stock-screener`; also reachable as `python -m stock_screener`.

### Ingestion and screening

| Command | Does |
| --- | --- |
| `update-universe` | Refreshes the company list from the market-data provider. |
| `update-market` | Refreshes daily OHLCV for stored companies, resuming from the last stored session.  `--since` backfills from a date instead of resuming from each company's last stored session, which is the only way to deepen existing history. |
| `update-eligibility-volume` | Refreshes consolidated average volume for the liquidity screen, from the consolidated tape rather than the price feed. Writes no price history. Run it before `enrich`: it is unmetered, so a company failing the liquidity gate here costs no provider request. |
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

### Point-in-time capture (Phase 8B)

Records what the system knew, on the day it knew it. Neither command calls a
provider for market data, and neither changes a score.

| Command | Does |
| --- | --- |
| `capture` | Copies the current scores and computes the observed market state, writing both as immutable rows for the day. Re-running a completed day writes nothing. A failure rolls the day back whole and records why. `--date` sets the day; it defaults to today in UTC, matching `score`. |
| `update-observations` | Appends what each filing reported, keeping every earlier version with the date it was filed. Costs no extra request straight after `update-fundamentals`, which leaves the filing document cached. |
| `update-acceptance` | Records when the SEC accepted each filing, which is what makes a point-in-time availability test correct. Skips companies already resolved, so a repeat run is nearly free. |
| `derive-signals` | Derives market signals from the captured features — peer percentiles, a multi-label setup, and a tradability verdict. No provider call, no ranking, no score. See [Market signals](market-signals.md). |

The distinction these two exist to preserve is between the operational tables,
which are overwritten as better information arrives, and the point-in-time
record, which is not. `score_snapshots` replaces the day's rows when `score` runs
again; `pit_score_snapshots` never does. `financial_snapshots` overwrites a
restated quarter; `fundamental_observations` appends the restatement beside the
original. See [Project state](PROJECT_STATE.md) for the rules that govern them.

### AI research (Phase 3)

`candidates`, `brief` and `update-filings` are inspection and ingestion; only
`run` calls a model. Assembling a brief touches no provider at all — it reads the
database and nothing else.

A research run never fails a pipeline. A quota, a timeout or an outage becomes a
`FAILED` report for that company and the run continues, so scanning, scoring and
ranking never depend on a model being reachable.

| Command | Does |
| --- | --- |
| `research candidates` | Lists the companies a research run would spend a model call on: the top of the ranking, the biggest score movers, and the hidden gems, deduplicated and capped at 25. |
| `research brief <TICKER>` | Assembles one company's research brief and summarises the evidence it carries — facts, score items, quarters, filings, and everything unknown. |
| `research update-filings` | Refreshes the stored SEC filing index that briefs cite. Metadata only; no filing text is fetched. `--candidates` restricts the pass to the current research set. |
| `research run [TICKER]` | Generates AI research, validates it, and stores what validation accepted. With no ticker, researches every candidate. Reuses a stored report whenever the evidence, the scoring rules and the prompt are all unchanged. |

| Option | Applies to | Effect |
| --- | --- | --- |
| `--ticker` / `-t` | the update commands, `scan`, `score` | Restrict to a symbol. Repeatable. |
| `--limit` / `-n` | `update-market`, `update-eligibility-volume`, `update-fundamentals`, `score` | Process at most this many companies, in ticker order. Bounds a first run against a metered provider. |
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
| `--limit` / `-n` | `research candidates` | Cap the selection across every source. Default 25. |
| `--json` | `research brief` | Print the whole brief as JSON, exactly as a model would receive it. |
| `--ticker` / `-t`, `--limit` / `-n` | `research update-filings` | Restrict the pass, as for the other update commands. |
| `--candidates` | `research update-filings` | Restrict the pass to the current research candidates. A full-universe refresh costs thousands of requests for filings no brief will cite. |
| `--dry-run` | `research run` | Print the exact prompt and brief that would be sent, then stop. No model is called and nothing is stored. |
| `--force` | `research run` | Ignore a stored report and generate a new one. |
| `--prepare` | `research run` | Fetch this company's SEC filing index and text first, so filing-dependent sections have evidence to cite. One ticker only, and nothing is generated if the fetch fails. |

Every command is idempotent: a second run updates rows rather than duplicating
them, including `score`, whose rows are unique per company, day and formula
version. A per-ticker provider failure is logged and counted, and does not end
the run.

Exit codes: `0` on success, `1` when `explain` has no stored score for the
ticker or `research brief` has no scored company to build one from, `2` when a
selected provider is missing its credentials.

### SEC evidence, derived (B2.12–B2.13)

The `evidence` group's derived-data commands read the trusted V2 corpus and
frozen state and write only rebuildable output; neither contacts the SEC,
writes an artifact prefix, or touches a database. Both verify the frozen
databases they are told to (`--expect-database-sha PATH=SHA256`) before and
after, and stop on any drift.

| Command | Does |
| --- | --- |
| `evidence build-index` | Reads every trusted artifact the V2 manifest names — once, verifying its size and SHA-256 against the manifest before parsing — into `derived/sec_evidence_index_v2.db`, in bounded transactions with a ledger that makes an interrupted build resumable and refuses a different manifest. Requires `BASELINE_COMPLETE`. Mirrors the small build metadata on completion; never the index. `--prefetch` overlaps downloads only; `--stop-after` stops cleanly; `--fresh` discards an in-progress build. See [Storage layout](storage-layout.md#b212-the-derived-index). |
| `evidence build-a3-inputs` | Assembles Plan A3's inputs — PIT coverage, class-aware reachability, gaps, candidates, the held set and per-form statistics — from the frozen PIT database, the frozen resolution, the V2 index and the V2 manifest; writes them as a digested package, assembles a second time to prove it deterministic, round-trips it, and mirrors it. Never runs the planner. Requires `DERIVED_INDEX_COMPLETE` and its mirrored marker. See [A3 planning inputs](a3-planning-inputs.md). |

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
