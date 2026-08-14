# stock-screener — Compounder Radar

> Scans U.S. equities and produces a short list worth researching.

Compounder Radar answers one question:

> Which stocks deserve deeper research because they show strong or improving
> growth, reasonable financial quality, and enough liquidity to be investable?

It is not a trading system and it does not predict prices. It reduces thousands
of listings to a small group, and explains what it saw.

## Phase 1 scope — what exists today

The MVP is built in four phases. **Phase 1 is complete**: data in, screened
companies out.

```text
Market Data  →  Scanner  →  Financial Metrics  →  Eligible Company Dataset
```

| Built | Not built yet |
| --- | --- |
| Universe loader, price and fundamentals ingestion | Compounder Score (Phase 2) |
| The full derived-metric engine | Risk penalties, rankings (Phase 2) |
| Eligibility filters with reasons | Hidden Gems, Improving Fast (Phase 2) |
| CLI with CSV export | AI research (Phase 3) |
| Minimal FastAPI foundation | Next.js dashboard (Phase 4) |

There is deliberately **no score** anywhere in the codebase. A placeholder score
would be a number people trust before it has been earned.

## Architecture

One deployable, three shared packages, dependencies pointing one way:

```text
src/stock_screener  →  packages/*  →  packages/domain
```

| Where | Owns |
| --- | --- |
| `packages/domain/` | The metric engine and eligibility rules. Pure functions, zero I/O — every number is testable against a hand-worked example. |
| `packages/api-clients/` | Provider protocols and the Alpaca, EDGAR, FMP and mock adapters. Vendor field names stop here. |
| `packages/data-access/` | The three tables, idempotent upserts, and row↔model translation. |
| `src/stock_screener/` | Config, logging, ingestion, the scanner, the CLI and the API — the thin layer that wires the rest together. |
| `migrations/` | Alembic revisions. |

Two rules run through all of it:

- **Missing is not zero.** A metric the data cannot support is `None`, all the
  way through to a blank cell in the CSV. `0.0` means the company reported zero.
- **No vendor vocabulary escapes `api-clients`.** The metric engine sees
  `revenue` and `PriceBar.close`, never `mktCap` or `"c"`.

Longer discussion in [docs/explanation/architecture.md](docs/explanation/architecture.md);
the decision behind the boundary is [ADR-0003](docs/adr/0003-normalise-provider-data-at-the-boundary.md).

## Local setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
make setup        # install the workspace, link agent skills, create .env
make migrate      # create the schema (SQLite by default)
make scan         # run the whole pipeline against the sample fixture
```

That works with **no credentials**: both providers default to `mock`, which
reads `fixtures/sample_universe.json`.

## Environment variables

Full table in [docs/reference/configuration.md](docs/reference/configuration.md).
The ones that matter first:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./compounder_radar.db` | PostgreSQL in production. |
| `MARKET_DATA_PROVIDER` | `mock` | `alpaca` for real prices and the universe. |
| `FUNDAMENTALS_PROVIDER` | `mock` | `edgar+fmp` for real statements plus market cap. |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | unset | Required when the provider is `alpaca`. Paper keys (`PK…`) also need `ALPACA_TRADING_BASE_URL=https://paper-api.alpaca.markets`. |
| `ALPACA_FEED` | `iex` | `sip` needs a paid data plan. IEX under-reports volume ~25x. |
| `SEC_USER_AGENT` | unset | Required for EDGAR; the SEC blocks anonymous callers. |
| `FUNDAMENTALS_API_KEY` | unset | Required when the provider involves `fmp`. |
| `MIN_PRICE` | `2.0` | Eligibility: minimum share price. |
| `MIN_MARKET_CAP` | `100000000` | Eligibility: minimum market cap. |
| `MIN_AVG_DOLLAR_VOLUME` | `1000000` | Eligibility: minimum 20-day average dollar volume. |

Credentials are `SecretStr`, so a settings object in a log line or a traceback
prints `**********`. `.env` is gitignored; `.env.example` holds names and
placeholders only.

## Database setup

SQLite needs nothing. For PostgreSQL, create the database and point at it:

```bash
createdb compounder_radar
export DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/compounder_radar"
```

### Migrations

Alembic reads the URL from `Settings`, so there is one source of truth.

```bash
make migrate                       # alembic upgrade head
make migrate-down                  # roll back one revision
uv run alembic -x url=postgresql+psycopg://... upgrade head   # one-off override
```

## Loading data

```bash
uv run stock-screener update-universe        # refresh the company list
uv run stock-screener update-market          # refresh daily OHLCV
uv run stock-screener update-fundamentals    # refresh quarterly statements

uv run stock-screener update-fundamentals --limit 50   # bound a metered first run
```

The recommended real configuration is `FUNDAMENTALS_PROVIDER=edgar+fmp`:
statements from SEC EDGAR, which is free and covers every U.S. filer with full
history, and market cap and sector from FMP's profile endpoint, which EDGAR does
not publish. See [Run a scan](docs/how-to/run-a-scan.md).

After changing anything about how a provider is parsed, re-check its mapping
against a live response and re-ingest with `--force` — the incremental skip
compares reporting dates and cannot tell the adapter changed:

```bash
uv run scripts/verify_fundamentals.py --ticker AAPL --expected-market-cap 4.5e12
uv run stock-screener update-fundamentals --force
```

All three are incremental and idempotent: prices resume from the last stored
session, fundamentals are skipped when the provider has nothing newer, and
unique constraints turn a repeat run into an update rather than a duplicate.

One failing ticker never ends a run. Failures are logged with their symbol and
counted in the summary.

## Running the scanner

```bash
uv run stock-screener scan                          # eligible companies
uv run stock-screener scan --all                    # plus exclusions and reasons
uv run stock-screener scan --output eligible.csv    # CSV export
uv run stock-screener run-scan                      # ingest everything, then scan
```

### Example output

```text
Ticker  Market Cap  Price    Rev Growth  Acceleration  Gross Margin  FCF Margin  Net Cash   ADV
------------------------------------------------------------------------------------------------------
CPRA    $310.0M     $15.05   4.0%        -1.3pp        22.4%         3.1%        -$86.0M    $3.0M
HLXB    $540.0M     $11.36   71.0%       +2.5pp        27.1%         -8.2%       $44.0M     $5.8M
MRDN    $8.9B       $78.21   6.0%        -0.2pp        31.2%         9.8%        -$1.2B     $74.8M
NVEX    $1.2B       $38.59   42.5%       +1.6pp        38.2%         6.5%        $315.0M    $25.4M

Processed: 9   Eligible: 4   Failed: 0
```

Rows are sorted by ticker. Nothing is ranked, because nothing is scored yet.
`-` marks a metric the data could not support.

The CSV always contains **every** company scanned, including exclusions and the
reasons for them, because the manual review this project depends on needs to see
what was thrown away.

## API

```bash
uv run uvicorn stock_screener.api:app --reload
```

`GET /health`, `GET /api/companies`, `GET /api/scan`. A foundation for Phase 4,
not a dashboard API.

## Running the tests

```bash
make check       # the gate: lint, format, mypy strict, tests with coverage
make test-unit   # the fast loop
```

No test reaches the network. Provider adapters are exercised through
`httpx.MockTransport`, and the database tests run against SQLite.

## Commands

| Command | What it does |
| --- | --- |
| `make check` | Everything CI runs |
| `make migrate` | Apply database migrations |
| `make scan` | Full pipeline, then print the table |
| `make test` / `make test-unit` | Tests with coverage / fast unit loop |
| `make lint` / `make format` | Ruff check / Ruff write |
| `make types` | mypy strict |
| `make docs` | Serve the docs site |

Full list: `make help`, or [docs/reference/commands.md](docs/reference/commands.md).

## Working with AI agents

Instructions live in [AGENTS.md](AGENTS.md), which every agent reads
(`CLAUDE.md` imports it, `.github/copilot-instructions.md` points at it).

Skills live **once** in [.agents/skills/](.agents/skills/) and load on demand.
Codex and Copilot read that directory natively; Claude Code only reads
`.claude/skills/`, so `make sync-skills` symlinks each skill across.

To add a rule, put it in the relevant skill rather than in AGENTS.md, and keep
AGENTS.md to facts that apply to every task.
