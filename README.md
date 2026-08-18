# stock-screener — Compounder Radar

> Scans U.S. equities and produces a short list worth researching.

Compounder Radar answers one question:

> Which stocks deserve deeper research because they show strong or improving
> growth, reasonable financial quality, and enough liquidity to be investable?

It is not a trading system and it does not predict prices. It reduces thousands
of listings to a small group, and explains what it saw.

## Scope — what exists today

The MVP was built in four phases and all four are complete: data in, a ranked
and explainable shortlist out, AI research that cites its evidence, and a
dashboard to read it on. A fifth added run control — the pipeline starts from
the dashboard rather than only from a terminal.

```text
Alpaca + EDGAR  →  Scanner  →  Metrics  →  CompounderScore  →  Preliminary ranking
                                                                      ↓
                                              FMP enrichment of the top candidates
                                                                      ↓
                                                            Final ranking, Top 50
                                                                      ↓
                              research candidates  →  SEC filing excerpts  →  brief
                                                                      ↓
                                    Claude draft  →  validation  →  grounded report
```

The broad scan runs entirely on free data: Alpaca for prices, SEC EDGAR for
filings, and a market capitalisation multiplied out from the cover-page share
count. A metered provider is spent only on the few hundred companies a ranking
actually shows — see [ADR-0008](docs/adr/0008-broad-scan-on-free-data-metered-enrichment-last.md).

| Built | Not built |
| --- | --- |
| Universe loader, price, benchmark and fundamentals ingestion | A second LLM provider |
| A broad scan that needs no metered provider | Filing exhibits (99.1 earnings releases) |
| The full derived-metric engine | Alerts and notifications |
| Eligibility filters with reasons | Authentication and deployment config |
| CompounderScore v1: growth, quality, valuation, market confirmation | Charts, portfolios, positions, price targets |
| Risk penalties, daily score snapshots, score history | |
| Top Opportunities, Hidden Gems, Wrong Price, Improving Fast | |
| CLI with CSV export and a per-company explanation | |
| FastAPI over the rankings, company detail and research | |
| Deterministic SEC filing-text extraction | |
| AI research whose every claim cites the evidence it rests on | |
| Next.js dashboard and a persistent watchlist | |
| Running the pipeline from the dashboard, with job history | |
| Ticker search across the whole universe, and CSV export | |

Every ranking is explainable: `stock-screener explain NVDA` prints the points
each metric earned and the value it earned them on. A metric the data cannot
support is shown as unavailable, never as zero.

Research is grounded the same way. `stock-screener research run` writes a report
per candidate in which every claim cites a score line, a metric, a reported
quarter or a verbatim SEC filing excerpt — and validation drops anything the
evidence does not support before it is stored. A section with no evidence behind
it answers `UNKNOWN` rather than sounding informative. See the
[Phase 3 brief](docs/reference/project-phases/phase3.md) for the full workflow,
the guardrails and the known limitations.

## Architecture

Two deployables, four shared packages, dependencies pointing one way:

```text
frontend  →  src/stock_screener  →  packages/*  →  packages/domain
```

| Where | Owns |
| --- | --- |
| `packages/domain/` | The metric engine, eligibility rules and CompounderScore. Pure functions, zero I/O — every number is testable against a hand-worked example. |
| `packages/api-clients/` | Provider protocols and the Alpaca, EDGAR, FMP and mock adapters. Vendor field names stop here. |
| `packages/data-access/` | The nine tables, idempotent upserts, and row↔model translation. |
| `packages/research/` | The AI research contract, prompt and validation. Also zero I/O. |
| `src/stock_screener/` | Config, logging, ingestion, the scanner, the scoring run, the CLI and the API — the thin layer that wires the rest together. |
| `frontend/` | The dashboard. Renders what the API returns and calculates nothing. |
| `migrations/` | Alembic revisions. |

The frontend is the second deployable and the only exception to what was a
one-deployable rule. It lives here because its types mirror these endpoints, so
a response-shape change lands on both sides in one commit. A third deployable
would need an ADR first — see [AGENTS.md](AGENTS.md).

Three rules run through all of it:

- **Missing is not zero.** A metric the data cannot support is `None`, all the
  way through to a blank cell in the CSV. `0.0` means the company reported zero.
  In scoring it is neither zero points nor full marks: its weight is carried by
  the other metrics in the same component.
- **No vendor vocabulary escapes `api-clients`.** The metric engine sees
  `revenue` and `PriceBar.close`, never `mktCap` or `"c"`.
- **A score records the rules it was produced by.** Every snapshot carries
  `COMPOUNDER_V1`, and no comparison crosses versions.

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

Add `uv run stock-screener score` to have something for the rankings to show,
or run `uv run stock-screener run-daily` to ingest, score and rank in one pass.
The dashboard is optional and needs Node — see
[Running the dashboard](#running-the-dashboard).

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
| `FMP_ENRICHMENT_LIMIT` | `200` | Candidates the enrichment pass may spend metered requests on. |
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
uv run stock-screener update-benchmark       # refresh the SPY series scoring needs
uv run stock-screener update-fundamentals    # refresh quarterly statements

uv run stock-screener update-fundamentals --limit 50   # bound a metered first run
```

`FUNDAMENTALS_PROVIDER=edgar` is enough to scan and rank the whole market: SEC
EDGAR is free, covers every U.S. filer with full history, and supplies the
cover-page share count that turns a price into a market capitalisation.
`edgar+fmp` adds a vendor market cap and consolidated volume, which are metered
and therefore spent by `enrich` on top candidates rather than on the scan. See
[Run a scan](docs/how-to/run-a-scan.md).

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

Rows are sorted by ticker — the scan screens, it does not rank. `-` marks a
metric the data could not support.

The CSV always contains **every** company scanned, including exclusions and the
reasons for them, because the manual review this project depends on needs to see
what was thrown away.

## Scoring and rankings

```bash
uv run stock-screener score                      # score everything, save today's snapshots
uv run stock-screener enrich                     # verify the top candidates, re-score them
uv run stock-screener rankings --limit 20        # Top Opportunities
uv run stock-screener hidden-gems                # small, fast, already scoring well
uv run stock-screener wrong-price                # strong business, poor valuation score
uv run stock-screener improving                  # biggest score gain over 30 days
uv run stock-screener explain NVDA               # why this company scored what it did
uv run stock-screener run-daily                  # ingest, score and rank in one pass
```

### Example output

```text
Rank  Ticker  Score  Raw    Risk   Growth  Quality  Valuation  Momentum  Rev Growth  Mkt Cap   30D
--------------------------------------------------------------------------------------------------
1     NVEX    88.4   93.0   -4.6   32.1    22.4     22.7       15.0      51.0%       $1.2B     +8.0
2     HLXB    84.7   87.0   -2.3   30.5    23.0     19.2       14.3      37.0%       $3.1B     -1.4
```

```text
Growth: 30.4 / 35   [SCORED]
  revenue_growth                    9.1 / 12   38.2%
  growth_acceleration               6.1 / 8    +15.4pp
  revenue_cagr_3y            unavailable / 6   -
  gross_profit_growth               4.2 / 5    55.0%
  growth_persistence                4.0 / 4    4  (4 of 4 comparable quarters observed)
```

The raw score, the risk penalty and the final score are always shown together,
and `data_coverage` says how much of the company was actually measurable. Every
rule is in [CompounderScore v1](docs/reference/compounder-score.md).

Scoring reads only stored data, so it is cheap to re-run, and `--dry-run` lets a
formula change be inspected before it enters the score history.

Every ranking row carries a state. **`PRELIMINARY` is a valid ranking** built
from Alpaca and EDGAR alone, with a calculated market cap and unverified
liquidity. **`FINAL`** means `enrich` obtained a vendor market cap and checked
the company against consolidated volume. Enrichment is optional and
quota-dependent — a `429` stops that pass and nothing else, so scanning, scoring
and ranking always work without it.

## API

```bash
uv run uvicorn stock_screener.api:app --reload      # http://localhost:8000
```

Served entirely from stored snapshots — the API calculates nothing, so it can
never disagree with the CLI.

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/health` | Liveness |
| `GET` | `/api/companies` | Stored companies |
| `GET` | `/api/scan` | The eligibility scan |
| `GET` | `/api/rankings` | Top Opportunities |
| `GET` | `/api/rankings/hidden-gems` | Small, fast, already scoring well |
| `GET` | `/api/rankings/wrong-price` | Strong business, poor valuation score |
| `GET` | `/api/rankings/improving` | Biggest score gain over 30 days |
| `GET` | `/api/companies/{ticker}/score` | One company's snapshot |
| `GET` | `/api/stocks/{ticker}` | Overview, score breakdown, metrics, watched flag |
| `GET` | `/api/stocks/{ticker}/research` | The latest validated research report |
| `GET` | `/api/watchlist` | Watched companies with their current scores |
| `POST` | `/api/watchlist/{ticker}` | Adds a company — idempotent |
| `DELETE` | `/api/watchlist/{ticker}` | Removes a company |

The watchlist is the only write in the surface, and the only thing a person
decides rather than the pipeline. Because of it, CORS names
`localhost:3000` and `127.0.0.1:3000` explicitly rather than wildcarding —
a wildcard would let any page a browser happens to be on edit the watchlist.

## Running the dashboard

One command starts both:

```bash
make dev            # API on :8000, dashboard on :3000, Ctrl-C stops both
make dev FORCE=1    # same, first reclaiming ports this project left behind
```

Or run them separately, which is what `make dev` does under the hood:

```bash
uv run uvicorn stock_screener.api:app --reload      # http://localhost:8000
cd frontend && npm install && npm run dev           # http://localhost:3000
```

Open <http://localhost:3000>. Four ranking tabs, a company page with the score
breakdown and its grounded research, the watchlist, and a **Jobs** tab that runs
the pipeline: the daily run, scoring, enrichment and the individual ingest
stages, with history and a live log. AI research runs per company, from that
company's page.

Search in the masthead reaches any company by ticker or name — necessary
because the rankings cap at 500 rows over a universe of thousands.

The pages render stored snapshots and nothing else, so **run `score` before
expecting anything to appear** — a database that has been scanned but never
scored produces empty rankings, correctly. `stock-screener run-daily` does the
whole pipeline in one pass.

The dashboard calculates nothing: every score, subscore, metric and claim on a
screen came out of the API. A metric the data cannot support renders as an em
dash, never as `0`. More in [frontend/README.md](frontend/README.md) and the
[Phase 4 brief](docs/reference/project-phases/phase4.md).

## Running the tests

```bash
make check       # the gate: lint, format, mypy strict, tests with coverage
make test-unit   # the fast loop
make check-web   # the frontend gate: tsc, ESLint, next build
```

No test reaches the network. Provider adapters are exercised through
`httpx.MockTransport`, and the database tests run against SQLite.

`make check-web` is separate on purpose: the Python gate runs in CI without a
node toolchain, so a backend change never waits for an npm install. Run it when
you touch `frontend/`.

## Commands

| Command | What it does |
| --- | --- |
| `make check` | Everything CI runs — the Python gate |
| `make check-web` | The frontend gate: tsc, ESLint, `next build` (needs Node) |
| `make dev` | The API and the dashboard together; Ctrl-C stops both. `FORCE=1` reclaims ports held by this project, never by anything else |
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
