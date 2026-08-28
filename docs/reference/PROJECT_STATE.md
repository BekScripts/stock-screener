---
type: reference
title: Project state
---

# Project state

The single checkpoint of what Compounder Radar *is* right now: which phase is
current, which decisions are frozen, what the schema and score definitions are,
and what must not be changed without being asked.

This page is a **state record, not a plan**. It describes what exists. Intent
lives in the phase briefs; rationale lives in the ADRs; procedure lives in the
skills.

## How this page is maintained

Update it **in the same commit** as any of the following. A change to one of
these that leaves this page stale is an incomplete change.

| Trigger | What to update |
| --- | --- |
| A phase completes or is frozen | Current phase, Frozen decisions, Next phase |
| A new `score_version` | Score definitions, Regression requirements |
| A new migration | Database migrations, Data contracts |
| A new frozen rule or ADR | Frozen decisions, Things explicitly not to change |
| A limitation found and accepted | Known limitations |
| A validated rollout or measurement | Completed experiments |
| Data promoted to the primary database | Completed experiments, Latest checkpoint |
| Any merge to `main` | Latest checkpoint |

Everything asserted here must be checkable against the repository. No number
appears without the run or the test that produced it.

---

## Current phase

**Phases 1 through 7 are complete. All of them are frozen.** The MVP finished at
Phase 5; Phases 6 and 7 are layers over it, not revisions of it.

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Ingestion, eligibility screen, universe | complete, frozen |
| 2 | Metric engine, CompounderScore, rankings, CLI, read-only API | complete, frozen |
| 3 | AI research — contract, provider, brief, pipeline | complete, frozen |
| 4 | Dashboard and watchlist | complete, frozen |
| 5 | Run control — jobs, ticker search, CSV export | complete, frozen |
| 6 | On-demand deep research (6A–6E: contract, prepare, collect, generate, dashboard) | complete, frozen |
| 7 | International coverage (7B–7F: currency, IFRS, cadence, FX, rollout) | complete, frozen |

**Nothing is in progress.** No phase 8 exists and none is to be started without
being asked.

### Data state

**The primary database has not received the Phase 7F foreign cohort.**
`compounder_radar.db` is byte-identical to its pre-rollout state — the entire
Phase 7 validation ran against copies, verified by row count and checksum before
and after each stage.

Code completion and data promotion are separate operations. Promoting the cohort
is a deliberate, separate step: apply the migrations, then run the ordinary
passes — `update-eligibility-volume`, `enrich`, `update-fundamentals`, `score` —
against the real database.

---

## Frozen decisions

These are load-bearing. Each is enforced by structure or by a test, not by
convention, and none may be relaxed without an explicit instruction.

### Cross-cutting

- **Missing is not zero.** A metric the data cannot support is `None` all the way
  through — model, column, CSV cell, JSON. `0.0` means the company reported zero.
  In scoring, a missing metric earns neither zero points nor full marks; its
  weight is carried by the other metrics in its own component, never by another
  component. (ADR-0007)
- **The score records its own rules.** Every snapshot carries a `score_version`
  and no comparison crosses versions. A curve, weight, cap, coverage minimum or
  ranking-admission rule means a *new* version — never an edit to an existing
  one — and `docs/reference/compounder-score.md` changes in the same commit.
  (ADR-0006)
- **A data-quality fix is not a policy change.** The test is one question: *does
  any company's number change?* Recognising a company the existing policy already
  excluded, or correcting a misread input, is not a version bump. Prove it with a
  test rather than asserting it.
- **Historical snapshots are never rewritten.** A company excluded today keeps
  the rows it earned under the rules of the day it was scored.
- **Not every company gets a number.** A bank, an ineligible security and a
  company with two quarters of history each get a stored row carrying the status
  that says why.
- **No metered provider gates the market.** The broad scan runs on Alpaca and
  EDGAR alone. FMP is optional enrichment. A `PRELIMINARY` ranking is a valid
  ranking. A provider failure or `429` must never prevent scanning, scoring or
  ranking. (ADR-0008)
- **The frontend calculates nothing.** Every score, subscore, metric and claim on
  a screen came out of the database through `stock_screener.api`.

### Phase 3 — research

- A brief is bounded by the `score_date` of the snapshot it explains and prefers
  the figures the score itself recorded.
- Nothing reaches the database without passing validation. The order is
  brief → draft → `validate_report` → persist; `save_report` takes a
  `ResearchReport`, which only validation builds.
- A provider failure is a `FAILED` report, never an exception that ends a run.
- **`D.` proves a filing exists; `X.` quotes what it says.** An `EXTRACTED` claim
  must cite an `X.` excerpt, and a number found in filing text supports only a
  claim citing the excerpt it appears in — never pooled across a brief.

### Phase 5 — jobs

- **A job is an existing command, never new pipeline code.** `JOB_KINDS` maps a
  key to a CLI argument list and is the entire allowlist. A caller picks a key,
  never an argument.
- **A job carries no result.** It answers "is it running, did it work" and
  nothing else.
- **One pipeline run at a time, and research beside it.** Pipeline kinds are
  mutually exclusive; `research` is guarded per ticker.
- **The job endpoints spend money and have no authentication.** They are only
  reasonable because the API binds localhost. `JOBS_ENABLED=false` is the off
  switch, and binding the API elsewhere without one is a mistake.

### Phase 6 — deep research

- **Web evidence never changes the CompounderScore.** `research.EvidenceKind` has
  no `W.` member; score-explaining sections accept `DETERMINISTIC` claims only;
  `DeepResearchReport` has no score field. All three are load-bearing.
- **Phase 3 is frozen and reused, never edited.** `deep-research` imports Phase 3
  types outright. Never add a member to a Phase 3 enum, never subclass a Phase 3
  class. `ResearchReport` and `DeepResearchReport` are unrelated types in
  different tables.
- **Deep reports append; they never overwrite.** `deep_research_reports` has no
  unique constraint, because a deep report is a dated investigation.
- **A deep report issues no instructions.** No BUY/SELL/HOLD, no price target, no
  position sizing — absent from the contract structurally.
- **A stage is an existing pass, never new pipeline code.** No second
  fundamentals engine, no deep-research metric, no second score.
- **Network first, assembly second.** Everything that fetches lives in
  `preparation`; `brief` only reads.
- **An unrecognised domain is rejected, not demoted.** The allowlist in
  `sources.py` is the whole policy and its default is refusal.
- **`X.` stays authoritative for filings.** A web copy of a filing the brief
  already quotes is rejected; commentary about one is not.
- **Collection is optional and never fatal.** `external=()` is a complete brief.
- **Nothing unvalidated is stored or served.**
- **An empty section says which kind of empty it is.** `NO_EVIDENCE` means the
  brief had nothing; `NO_VALID_CLAIMS` means nothing survived validation.

### Phase 7 — international

- **The filing's unit is authoritative, never the vendor's.** `reporting_currency`
  comes from the XBRL unit key; `quote_currency` is what the listed share trades
  in. One field held both and whichever provider wrote last won — mixing TSM's
  TWD statements with its USD market cap yields an EV/Revenue of 0.38x against a
  true 24.07x.
- **Convert the market side, never the statements.** Only the market
  capitalisation crosses currencies. Every ratio reads
  `CompanyMetrics.market_cap_for_ratios` — converted where a rate exists, `None`
  where one does not, never the unconverted figure.
- **A rate belongs to a date and a source.** `fx_rates` stores one row per
  `(base, quote, rate_date, provider)`. Rates are bounded to five days before the
  score date, never after it. `FX_UNAVAILABLE` is a warning, not an exclusion.
- **Coverage is never bought with a fabricated period.** Four annual periods are
  not a trailing year; six-month figures are never halved into quarters.
- **A share count that cannot be multiplied by the price is not read.**
  `MarketCapSource.CALCULATED` is never produced for 20-F/40-F filers.
- **A score built on `STALE` fundamentals keeps its number everywhere the number
  describes the company, and is excluded from current rankings.**

---

## Architecture

### Two deployables, one repository

A deliberate exception to the one-deployable rule. They live together because the
dashboard's types mirror these endpoints and a response-shape change must land on
both sides in the same commit. **Do not move the frontend to another repository.**

| Deployable | Is | Owns |
| --- | --- | --- |
| `src/stock_screener/` | Python backend, CLI, read-only API | all data, every calculation, every score |
| `frontend/` | the dashboard, Next.js | rendering what the API returns |

A third deployable needs an ADR first and probably its own repository.

### Packages

| Package | Import | Holds | I/O |
| --- | --- | --- | --- |
| `packages/domain` | `domain` | models, metric engine, eligibility, CompounderScore, curves, sectors, statements, cadence | none |
| `packages/data-access` | `data_access` | SQLAlchemy models, repositories, session | database |
| `packages/api-clients` | `api_clients` | Alpaca, EDGAR, FMP, research providers, external search, FX | HTTP |
| `packages/research` | `research` | Phase 3 contract, prompt, validation | none |
| `packages/deep-research` | `deep_research` | Phase 6 contract, brief, evidence, provenance, prompt, validation | none |

Placement rule: one caller → a module in `src/stock_screener/`; two or more
callers → a package. Never create one speculatively.

### Application modules

```
src/stock_screener/
  cli.py            every command; the only entry point a job may spawn
  api.py            the read-only API plus the job endpoints
  config.py         the typed settings — the only reader of the environment
  dashboard.py      read models for the dashboard endpoints
  providers.py      provider wiring and composition
  fx.py             rate resolution and storage
  jobs.py           JOB_KINDS, spawn, track
  logging.py        structlog setup
  scanning/         ingestion, scanner, report
  scoring/          engine, enrichment, rankings, report
  research/         candidates, brief, runner, report, store
  deep_research/    preparation, collection, sources, brief, runner, report
```

### Frontend routes

`/` · `/stocks/[ticker]` · `/watchlist` · `/jobs` · `/research` ·
`/research/[ticker]`

---

## Score definitions

### Versions

| Version | State | What it is |
| --- | --- | --- |
| `COMPOUNDER_V1` | history only | the original rules |
| `COMPOUNDER_V1_1` | history only | V1 plus three guards; no weight or curve changed |
| `COMPOUNDER_V1_2` | **current** | V1.1's formulas exactly, plus one ranking-admission policy |

`CURRENT_SCORE_VERSION = COMPOUNDER_V1_2`, in
`packages/domain/src/domain/scores.py`.

**V1.1's guards:** redistribution capped at a 1.15 uplift; acceleration credit
withheld from a rebound against a flat three-year trend; revenue growth plus
acceleration capped where growth does not persist.

**V1.2's policy:** a score built on `STALE` fundamentals is excluded from current
rankings. Every formula, curve, weight, threshold and penalty is V1.1's. It
needed a version because it changes what a ranking *is*, not because a number
moved.

### Components

| Component | Points |
| --- | --- |
| Growth | 35 |
| Financial Quality | 25 |
| Valuation | 25 |
| Market Confirmation | 15 |

Risk penalties are applied after; full rules in
[CompounderScore](compounder-score.md).

### Scoring status

`SCORED` · `INSUFFICIENT_DATA` · `UNSUPPORTED_SECTOR` · `NOT_ELIGIBLE` · `ERROR`.
`final_score` is a number only for `SCORED`.

### Freshness

`CURRENT` · `STALE`. Recorded on the snapshot, never derived by the reader,
because the bound scales with reporting cadence. `domain.is_rank_eligible` is the
single place the question is answered. Rows written before migration `0020` read
as `CURRENT`.

| | Stale score |
| --- | --- |
| Numerical score | kept |
| Stock detail page | shown, with a notice |
| Research and Deep Research | available |
| Historical snapshots | untouched |
| Current rankings — all four views | excluded |

### Ranking views

`rankings` (top) · `hidden-gems` · `wrong-price` · `improving`. All four are
current views and all four exclude stale scores.

---

## Data contracts

### Domain enums

| Enum | Members |
| --- | --- |
| `ScoringStatus` | `SCORED`, `INSUFFICIENT_DATA`, `UNSUPPORTED_SECTOR`, `NOT_ELIGIBLE`, `ERROR` |
| `Freshness` | `CURRENT`, `STALE` |
| `ExclusionReason` | `PRICE_BELOW_MINIMUM`, `MARKET_CAP_BELOW_MINIMUM`, `LOW_LIQUIDITY`, `INACTIVE`, `UNSUPPORTED_SECURITY_TYPE`, `UNSUPPORTED_CURRENCY`, `MISSING_REQUIRED_DATA` |
| `PeriodCadence` | `QUARTERLY`, `SEMIANNUAL`, `ANNUAL`, `UNKNOWN` |
| `StatementProfile` | `GENERAL`, `FINANCIAL_INSTITUTION` |
| `MarketCapSource` | provenance of a market cap; `CALCULATED` is withheld from 20-F/40-F filers |
| `VolumeBasis` | which tape a liquidity figure came from |

Anything a package exports lives in its `__all__`. Reaching into a submodule from
outside is a bug.

### Eligibility thresholds

`min_price` 2.0 · `min_market_cap` 100,000,000 · `min_avg_dollar_volume`
1,000,000 · `min_trading_days` 20. Defaults mirror `.env.example`; the
application builds `EligibilityThresholds` from `Settings`.

### Prompt versions

`research.CURRENT_PROMPT_VERSION = RESEARCH_PROMPT_V2` ·
`deep_research.CURRENT_DEEP_PROMPT_VERSION = DEEP_RESEARCH_PROMPT_V2`. A prompt
version change invalidates the deep-research cache.

### API surface

Read-only unless marked.

```
GET    /health
GET    /api/companies
GET    /api/companies/search
GET    /api/companies/{ticker}/score
GET    /api/scan
GET    /api/rankings
GET    /api/rankings/hidden-gems
GET    /api/rankings/wrong-price
GET    /api/rankings/improving
GET    /api/rankings/{view}/export
GET    /api/stocks/{ticker}
GET    /api/stocks/{ticker}/research
GET    /api/deep-research/{ticker}
GET    /api/deep-research/{ticker}/history
GET    /api/deep-research/reports/{report_id}
GET    /api/watchlist
POST   /api/watchlist/{ticker}          write
DELETE /api/watchlist/{ticker}          write
GET    /api/jobs/kinds
POST   /api/jobs                        write, spawns a process
GET    /api/jobs
GET    /api/jobs/{job_id}
```

### Job kinds

`daily` · `scan` · `score` · `enrich` (spends money) · plus the research kinds.
The map in `src/stock_screener/jobs.py` is the entire allowlist.

### CLI

`update-universe` · `update-market` · `update-eligibility-volume` ·
`update-benchmark` · `update-fundamentals` · `scan` · `run-scan` · `score` ·
`enrich` · `rankings` · `hidden-gems` · `wrong-price` · `improving` · `explain` ·
`run-daily`, plus the `research` group (`candidates`, `brief`, `update-filings`,
`update-filing-text`, `run`) and the `deep-research` group (`prepare`, `run`).

---

## Database migrations

Head is **`0020`**. One revision per schema change; the chain is linear.

| Rev | Adds |
| --- | --- |
| `0001` | initial schema — `companies`, `financial_snapshots`, `price_history` |
| `0002` | `reporting_currency` |
| `0003` | consolidated average daily share volume |
| `0004` | `score_snapshots` — one score per company per day per version |
| `0005` | `benchmark_prices` |
| `0006` | point-in-time share counts and market-cap provenance |
| `0007` | gross-profit basis — which concept produced the figure |
| `0008` | `research_reports` |
| `0009` | `filings` |
| `0010` | `filing_excerpts` |
| `0011` | `watchlist` |
| `0012` | `jobs` |
| `0013` | `deep_research_reports` |
| `0014` | external collection reuse |
| `0015` | score coverage — market run or single company |
| `0016` | `score_snapshots.exclusion_reasons` |
| `0017` | `fx_rates`; splits reporting currency from quote currency |
| `0018` | `period_start` and `cadence`; existing rows backfilled `QUARTERLY` |
| `0019` | `statement_profile`, `consolidated_avg_volume`, `volume_source` |
| `0020` | score freshness; NULL reads as `CURRENT` |

### Tables

`companies` · `financial_snapshots` · `price_history` · `benchmark_prices` ·
`fx_rates` · `score_snapshots` · `filings` · `filing_excerpts` ·
`research_reports` · `deep_research_reports` · `watchlist` · `jobs`

### The migration safety rule

**Never verify a migration against `compounder_radar.db`.** Use a temporary
database or a throwaway copy.

A migration is not only DDL: `batch_alter_table` rebuilds a table on SQLite, and
dropping `companies` under enforced foreign keys cascades through every child
declaring `ON DELETE CASCADE` — price history, fundamentals, scores, filings,
excerpts, both research tables and the watchlist. Migration `0017` did exactly
that and deleted about 1.6 million rows from the primary database. That is why
`tests/integration/test_migrations.py::test_migrating_a_populated_database_preserves_its_rows`
exists.

---

## Known limitations

Documented rather than fixed. Each is a deliberate stop, not a backlog item.

| Limitation | Consequence |
| --- | --- |
| **6-K interim statements** — an untyped envelope; most carry no financial statements and the ones that do are indistinguishable without opening them (hundreds of MB per filer) | Foreign quarterly reporters that publish interims only via 6-K become `STALE` and leave current rankings. All 191 current-rank-eligible foreign companies are annual filers. 6-K exhibit discovery is not to be implemented without being asked. |
| **us-gaap capital expenditure basis** (`KNOWN_CAPEX_BASIS_LIMITATION`) | 7 affected names, none in the Top 100, maximum score sensitivity 0.04 points. Normalisation unchanged. |
| **Filing-instance bandwidth** | 458.7 MB of 613.2 MB total traffic; median instance 5.45 MB, maximum 49.6 MB. Where it fails it fails safely — CGAU kept its 2023 history. |
| **Annual-only period semantics** | TSM, ASML, SAP and NVO remain unrankable. Currency is no longer their blocker; period semantics is. They get a correct currency, a stated exclusion reason and an indexed 20-F — not a score. |
| **No ADR ratio lookup** | Cover-page share counts on 20-F/40-F are not read; `market_cap_source` falls to `PROVIDER` or `UNKNOWN`. |
| **Single-exchange price bars** | The IEX share of consolidated volume ranged from 0.006% to 12% across 259 companies, so the $1M gate cannot be applied to stored bars. `update-eligibility-volume` reads the consolidated tape instead. |
| **No authentication on the job endpoints** | They spend money. Only reasonable because the API binds localhost. |

---

## Completed experiments

Measured runs, kept because they are the evidence behind decisions above.

### Phase 7F rollout — validated Stage C, against a copy

| | |
| --- | --- |
| Foreign cohort | 762 |
| Fundamentals recovered | 289 |
| Numerical scores | 206 |
| **Current-rank eligible** | **191** |
| Stale scored but excluded | 15 |
| Insufficient | 82 |
| Not eligible | 461 |
| Unsupported sector | 13 |
| Provider unavailable | 0 |

Cadence of the current-rank eligible: 191 `ANNUAL`, 0 `SEMIANNUAL`, 0
`QUARTERLY`. Taxonomy: 147 IFRS, 44 us-gaap.

Foreign entrants: **GFI (#11)** and **ERO (#19)** in the Top 20; DLO, ARIS, CYD,
KGC, IAG in the Top 50; TBBB, B, AU, MTA in the Top 100. Every Top-20 and Top-50
entrant was reconciled against its filed statements.

**CGAU keeps its 73.48 and leaves the ranking** — its newest statement is from
2023, against a 2026 market capitalisation.

### V1.1 → V1.2 parity

1,929 companies scored under both versions with **zero** numeric differences.
Every scoring file is byte-unchanged since the last V1.1 commit.

### Three defects the rollout exposed

Each was caught by running real companies, not by reasoning:

- The composite provider replaced the EDGAR profile wholesale, erasing the
  reporting currency only EDGAR knew. The profile is now merged field by field.
- The enrichment pass re-scored without an FX resolver, turning a complete
  `PRELIMINARY` score into a `FINAL` one missing every currency-sensitive metric
  — verification made the score *worse* for exactly the companies it verified.
- A deposit-funded bank (Kaspi.kz) scored 74.78 and ranked twenty-first because
  both available classifications called it a technology company.
  `domain.statements` now reads the statements instead.

### Liquidity pass economics

Running `update-eligibility-volume` before the metered pass took provider
requests for the 762-company cohort from **762 to 353**.

---

## Regression requirements

These must keep passing. Each one encodes a decision that cost something to
learn.

| Test | Protects |
| --- | --- |
| `tests/integration/test_migrations.py::test_migrating_a_populated_database_preserves_its_rows` | the 1.6-million-row deletion never recurs |
| `tests/integration/test_migrations.py::test_the_migrated_schema_matches_the_models` | migrations and models cannot drift |
| `tests/integration/test_scoring_pipeline.py::test_a_current_company_scores_exactly_what_v1_1_scored` | V1.2 moved no number — the golden value was measured against the last V1.1 commit |
| `tests/integration/test_scoring_pipeline.py::test_every_current_ranking_view_excludes_a_stale_score` | the V1.2 policy applies to all four views |
| `tests/integration/test_scoring_pipeline.py::test_a_stale_company_keeps_its_score_and_leaves_the_ranking` | a stale score is withheld from rankings, not deleted |
| `tests/integration/test_scoring_pipeline.py::test_a_snapshot_predating_freshness_still_ranks` | pre-`0020` rows are not retroactively stale |
| `tests/integration/test_scoring_pipeline.py::test_a_bank_stays_in_the_database_and_out_of_the_ranking` | unsupported sectors are recorded, not dropped |
| `packages/domain/tests/test_audit_regressions.py` | ticker normalisation, bar validation, boundary inclusivity, consolidated-volume preference |
| `packages/domain/tests/test_scoring_v11_guards.py` | the three V1.1 guards, their stacking, and component maximums |
| `packages/domain/tests/test_period_cadence.py` | staleness scales with cadence; `UNKNOWN` is never reported stale |
| `packages/domain/tests/test_scoring_currency_safety.py` | an enterprise value without a conversion is rejected outright |
| `packages/domain/tests/test_statements.py` | a bank is recognised from its statements; a manufacturer with a finance arm is not |
| `packages/deep-research/tests/test_brief.py` | fingerprints are reproducible across reassembly and refetch |

### The gate

`make check` — lint, format check, mypy strict, pytest with coverage failing
under **80%**. It is what CI runs; run it rather than the individual tools.
`make check-web` is separate and needs Node. Touching `frontend/` means both.

---

## Next phase

**None is planned or started.** The MVP is finished and every layer over it is
frozen.

Two pieces of work are *identified* but not authorised, and neither is to be
started without being asked:

1. **Promoting the Phase 7F cohort to the primary database.** The code is done;
   the data operation is not. It means applying the migrations and running the
   ordinary passes against `compounder_radar.db`.
2. **Phase 7D — period semantics.** Annual and semiannual reporting periods as
   first-class citizens. This is what still keeps TSM, ASML, SAP and NVO
   unrankable.

A longer-term direction document exists at `docs/reference/roadmap.md`. It is
aspiration, not commitment: nothing in it is authorised, scoped or scheduled, and
nothing in it overrides *Things explicitly not to change*.

---

## Things explicitly not to change

Not a backlog. These exclusions are deliberate.

**Do not build without being asked:**

- a second LLM provider, or a second execution path for a job
- scheduled or automatic deep research, report diffing, alerts, notifications
- charts, portfolios, positions, price targets
- authentication or deployment config
- filing exhibits, 6-K exhibit discovery
- converting *statements* into another currency
- annual or semiannual period semantics, ADR ratio lookup
- reading filing documents to recover figures the companyfacts API omits

**Do not touch:**

- any scoring file, without a new `score_version` and a doc change in the same
  commit
- a Phase 3 enum or class — `deep-research` imports them, never extends them
- `uv.lock` by hand — use `uv add` / `uv remove`
- `.github/workflows/` — ask first
- the frontend's location — it stays in this repository
- `compounder_radar.db`, for any migration verification

**Never:**

- read `os.environ` directly — add a typed field to `config.py` and a line in
  `.env.example`
- commit a secret, in code, tests, fixtures or docs
- leave a failing or skipped test without saying so explicitly
- let a calculated figure become indistinguishable from a supplied one

---

## Latest checkpoint

| | |
| --- | --- |
| Commit | `9ca5dfb` — feat: complete foreign issuer coverage and current rankings |
| Branch | `feat/phase-6b-single-stock-prepare` (the branch name predates the Phase 7 work it now carries) |
| Migration head | `0020` |
| Score version | `COMPOUNDER_V1_2` |
| Primary database | pre-rollout state; Phase 7F cohort **not** promoted |
| Checkpoint date | 2026-08-27 |
