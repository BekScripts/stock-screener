# AGENTS.md

Instructions for every AI agent working in this repository. Read by Codex,
Copilot, Cursor, Gemini CLI, and — via the `@AGENTS.md` import in `CLAUDE.md` —
by Claude Code.

## The one rule

**Before you write or change anything, load the skill that covers it.** The
skills below are not reference material to consult when stuck; they are the
required procedure. If a task matches a skill, follow that skill's checklist.
If a skill and this file disagree, the skill wins — it is more specific.

If you are about to do something no skill covers, say so before proceeding.

## Skills

Skills live in `.agents/skills/<name>/SKILL.md` — one canonical copy, symlinked
into `.claude/skills/` for Claude Code. Invoke one directly with `/<name>` (or
`$<name>` in Codex), or let it load automatically.

| Skill | Load it when you are… |
| --- | --- |
| `python-package` | creating, renaming, splitting, or removing anything in `packages/` |
| `python-app` | writing application code in `src/` |
| `testing` | writing or changing any test, or asked why tests fail |
| `dependency-management` | adding, upgrading, or removing a dependency |
| `config-and-secrets` | reading configuration, adding an env var, handling a credential |
| `observability` | adding logging, metrics, or error handling |
| `ci-cd` | changing anything in `.github/workflows/`, or CI is failing |
| `documentation` | writing docstrings, README, or anything under `docs/` |
| `adr` | making a decision that is expensive to reverse |
| `scripts` | adding a one-off or maintenance script |
| `git-workflow` | branching, committing, or opening a PR |

These are the skills that apply to any Python project. Domain-specific
procedures — a database layer, an HTTP client convention, a queue protocol —
are written per project as they earn their place. Add one with a new directory
in `.agents/skills/` and a row in this table, then run `make sync-skills`.

## Project

**Compounder Radar** scans U.S. equities and produces a short list worth
researching. A filter set is defined as data, evaluated against a universe of
securities, and the matches are returned for export or further analysis.

The screening rules themselves live in the `domain` package, deliberately free
of I/O. Market data arrives through `api-clients` (external HTTP providers) and
is persisted or cached via `data-access`. The `research` package holds the AI
research contract — also free of I/O. `src/stock_screener/` is the thin
deployable that wires those together behind a CLI.

The MVP is built in four phases, specified in
`docs/reference/project-intro.md`. **Phases 1 and 2 are complete**: ingestion,
the derived-metric engine, eligibility filtering, CompounderScore v1, risk
penalties, daily score snapshots, the four ranking views, the CLI and a
read-only API.

**Phase 3 (AI research) is complete and frozen.** The contract and prompt live in
`packages/research`; the `ResearchProvider` boundary and its one real
implementation in `api-clients`; deterministic SEC filing-text extraction beside
it; the application layer in `src/stock_screener/research/` — candidate
selection, brief assembly, the generate-validate-persist pipeline, persistence,
caching and a run-cost guard; and the `research` CLI group. Its workflow and
known limitations are documented in `docs/reference/project-phases/phase3.md`.

**Phase 4 (dashboard and watchlist) is complete.** The MVP is done. The
dashboard lives in `frontend/`, the endpoints that feed it in
`src/stock_screener/api.py`, their read models in
`src/stock_screener/dashboard.py`, and the watchlist write in the `watchlist`
table. Its scope and known limitations are documented in
`docs/reference/project-phases/phase4.md`.

**Phase 5 (run control) is complete and frozen.** The dashboard can now run the
pipeline, not just read it. `src/stock_screener/jobs.py` spawns existing CLI
commands as subprocesses and tracks them in the `jobs` table; the `/api/jobs`
endpoints start and report them; `frontend/app/jobs/` is the control panel. Also
added: ticker search, because the rankings cap at 500 rows over a universe of
thousands, and CSV export of a ranking view. `make dev` starts both processes
and stops both. Its scope and known limitations are documented in
`docs/reference/project-phases/phase5.md`.

The MVP is finished. Phases 1 through 5 are all complete, and none of them is a
place to add to without being asked.

**Phase 6 (on-demand deep research) is complete and frozen.** Deep research is a separate layer
over the screener, not a change to it: a person types a ticker and one company
is investigated against its current fundamentals, its filings and what has since
been published about it. `packages/deep-research` holds the contract — the `W.`
external evidence namespace, `DeepResearchBrief` and its three fingerprints, the
seventeen report sections, and the provenance rules — and
`deep_research_reports` (migration `0013`) stores a validated report. Its scope
and what it deliberately excludes are documented in
`docs/reference/project-phases/phase6.md`, and the placement decision in ADR
0010.

Four rules govern that layer:

- **Web evidence never changes the CompounderScore.** `research.EvidenceKind`
  has no `W.` member, so an external id is unresolvable to the Phase 3
  validator; the sections that explain the score accept `DETERMINISTIC` claims
  only; and `DeepResearchReport` has no score field. All three are load-bearing.
- **Phase 3 is frozen and reused, never edited.** `deep-research` imports
  `ScoreEvidence`, `MetricFact`, `ReportedPeriod`, `FilingText` and the rest
  outright. Never add a member to a Phase 3 enum, never subclass a Phase 3
  class. `ResearchReport` and `DeepResearchReport` are unrelated types in
  different tables.
- **Deep reports append; they never overwrite.** `research_reports` upserts on
  its cache key. `deep_research_reports` has no unique constraint, because a
  deep report is a dated investigation and the history of what was concluded is
  the point.
- **A deep report issues no instructions.** No BUY/SELL/HOLD, no price target,
  no position sizing — absent from the contract structurally, not forbidden by a
  prompt.

Phase 6B adds `src/stock_screener/deep_research/`: `prepare_company` refreshes
one ticker by running the **existing** ingestion and scoring passes narrowed by
their `tickers` argument, and `assemble_deep_brief` reads the result back with no
network access at all. `deep-research prepare <TICKER>` is the CLI. Two rules
govern it:

- **A stage is an existing pass, never new pipeline code.** No second
  fundamentals engine, no deep-research metric, and no second score — the
  snapshot written is an ordinary `COMPOUNDER_V1_1` row.
- **Network first, assembly second.** Everything that fetches lives in
  `preparation`; `brief` only reads. That is what makes fingerprints reproducible
  and the cache usable.

Phase 6C adds the `W.` namespace in practice: `ExternalResearchProvider` in
`api-clients` (one `search` method, `TavilySearch` and `MockExternalResearch`),
`deep_research/sources.py` for the tier policy, and
`deep_research/collection.py` for the collector. `prepare <TICKER> --external`
runs it. Three rules:

- **An unrecognised domain is rejected, not demoted.** The allowlist in
  `sources.py` is the whole policy, and its default is refusal. Social platforms,
  forums and aggregator finance sites are additionally named in a denylist.
- **`X.` stays authoritative for filings.** A web copy of a filing the brief
  already quotes is rejected; commentary about one is not.
- **Collection is optional and never fatal.** A vendor being unconfigured,
  unreachable or out of quota costs a brief nothing — `external=()` is complete.

Phase 6D generates and validates: `api-clients` holds the provider,
`deep_research.validation` decides what survives, and
`src/stock_screener/deep_research/runner.py` runs the sequence — prepare,
collect, assemble, measure, cache, guard, generate, validate, persist. The cache
is consulted before the provider and the prompt is measured before it is sent,
because both exist to avoid spending badly. Phase 6E is the dashboard:
`/research` and `/research/[ticker]`, with execution going through the existing
job system rather than a second path.

Two more rules govern those layers:

- **Nothing unvalidated is stored or served.** The provider returns a draft; only
  validation constructs a report. Rejected text never reaches the database, the
  API or a screen — publishing what validation refused to publish would defeat
  the refusal.
- **An empty section says which kind of empty it is.** `NO_EVIDENCE` means the
  brief had nothing; `NO_VALID_CLAIMS` means it had plenty and nothing survived.
  Collapsing them blames the data for a failure of the generation.

Not built, and not to be started without being asked: scheduled or automatic
deep research, report diffing or comparison, alerts, charts, and any second
execution path for a job.

**Phase 7 (international coverage) is complete.** Foreign private issuers run
through the same pipeline as everything else — same ingestion, same metric
engine, same CompounderScore V1.1, same rankings. There is no international
score and no second fundamentals engine. Every fix landed in the input layer:
`ifrs-full` is a second concept table in `api-clients/edgar.py` selected per
company, the money unit is read from the filing rather than assumed, and
`score_snapshots.exclusion_reasons` (migration `0016`) records why an ineligible
company was not scored. Its scope and limits are in
`docs/reference/project-phases/phase7.md`.

Three rules govern that layer:

- **The filing's unit is authoritative, never the vendor's.** `reporting_currency`
  comes from the XBRL unit key; `quote_currency` is what the listed share trades
  in, and the two are separate fields because one field held both and whichever
  provider wrote last won. Mixing TSM's TWD statements with its USD market cap
  yields an EV/Revenue of 0.38x against a true 24.07x — a factor of 63, and the
  error this layer exists to prevent.
- **Convert the market side, never the statements.** Reported history stays in
  the money it was filed in; restating it would put FX movement into revenue
  growth and margins, which are properties of the business. Only the market
  capitalisation crosses. Every ratio against it reads
  `CompanyMetrics.market_cap_for_ratios` — converted where a rate exists, **None**
  where one does not, never the unconverted figure. `CompanyMetrics` refuses to
  hold an enterprise value without the conversion that would justify it.
- **A rate belongs to a date and a source.** `fx_rates` (migration `0017`) stores
  one row per `(base, quote, rate_date, provider)` so a score reproduces rather
  than re-deriving at today's rate. The ECB answers first and names the business
  day it used; a broad dataset answers only for pairs the ECB does not publish,
  TWD among them. Rates are bounded to five days before the score date, never
  after it, and resolved once per pair per run. An FX outage costs foreign
  companies their currency-sensitive sub-scores and nothing else —
  `FX_UNAVAILABLE` is a warning, not an exclusion.
- **Coverage is never bought with a fabricated period.** Four annual periods are
  not a trailing year and six-month figures are never halved into quarters. TSM,
  ASML, SAP and NVO are annual-only and remain unrankable; they gain a correct
  currency, a stated exclusion reason and an indexed 20-F, not a score.
- **A share count that cannot be multiplied by the price is not read.** Cover-page
  counts on a 20-F or 40-F are ordinary shares while the listed security is an
  ADS, and no XBRL field gives the ratio — TSM's is five, the other three are
  one. `MarketCapSource.CALCULATED` is never produced for those filers; it falls
  to `PROVIDER` or `UNKNOWN`.

Not built, and not to be started without being asked: converting the *statements*
into another currency, annual or semiannual period semantics, ADR ratio lookup,
and reading filing documents to recover figures the companyfacts API omits.
Period semantics is Phase 7D and is what still keeps TSM, ASML, SAP and NVO
unrankable — currency is no longer their blocker.

Three rules govern that layer:

- **A job is an existing command, never new pipeline code.** `JOB_KINDS` maps a
  key to a CLI argument list and is the entire allowlist — a caller picks a
  key, never an argument. Adding a stage means adding a CLI command first.
- **A job carries no result.** Every command persists what it produces, so a
  finished job is read through the endpoint serving that thing. A job record
  answers "is it running, did it work", and nothing else.
- **One pipeline run at a time, and research beside it.** Every kind except
  `research` writes the shared tables, so only one of them runs at once — `scan`
  and `score` are different commands over the same rows, and a second request
  returns the one in flight rather than starting a competitor. `research` writes
  only its own company's report and reads a stored snapshot, so it neither
  blocks a pipeline run nor waits for one; it is guarded per ticker instead.

Not built, and not to be started without being asked: a second LLM provider,
filing exhibits, alerts, notifications, authentication, deployment config,
charts, portfolios, positions and price targets. Those exclusions are
deliberate, not a backlog.

**The job endpoints spend money and have no authentication.** They are only
reasonable because the API binds localhost. `JOBS_ENABLED=false` is the off
switch, and binding the API anywhere else without one is a mistake.

Three rules govern that layer:

- **A brief is bounded by the `score_date` of the snapshot it explains**, and
  prefers the figures the score itself recorded. A brief that reached a provider,
  or that explained an old score with new data, would be the two failures it
  exists to prevent.
- **Nothing reaches the database without passing validation.** The order is
  brief → draft → `validate_report` → persist, and `save_report` takes a
  `ResearchReport`, which only validation builds.
- **A provider failure is a `FAILED` report, never an exception that ends a
  run.** Scanning, scoring and ranking never depend on a model being reachable.
- **`D.` proves a filing exists; `X.` quotes what it says.** An `EXTRACTED` claim
  must cite an `X.` excerpt, and a number found in filing text supports only a
  claim citing the excerpt it appears in — never pooled across a brief.

Three rules run through the whole codebase and are the ones most worth
protecting:

- **Missing is not zero.** A metric the data cannot support is `None` all the way
  through — model, database column, CSV cell, JSON. `0.0` means the company
  reported zero. Never conflate them in either direction. In scoring, a missing
  metric earns neither zero points nor full marks: its weight is carried by the
  other metrics in the same component, never by another component.
- **The score records its own rules.** Every snapshot carries a `score_version`,
  and no comparison crosses versions. Changing a curve, a weight or a coverage
  policy means a new version and a new `CURRENT_SCORE_VERSION` — never an edit
  to an existing one — and `docs/reference/compounder-score.md` changes in the
  same commit. `COMPOUNDER_V1_1` is current; `COMPOUNDER_V1` history is kept.

  **A data-quality fix is not a policy change.** The two are distinguished by one
  question: *does any company's number change?*

  | Change | Version |
  | --- | --- |
  | A curve, a weight, a redistribution cap, a coverage minimum, or which statuses may be ranked | **new version** |
  | Identifying a company the existing policy already excluded, or correcting an input that was read wrongly | **no version change** |

  Excluding banks is V1.1 policy already. Recognising a bank that both vendors
  mislabelled enforces that policy; it does not alter it, and every company that
  still scores scores exactly what it scored before. Bumping the version there
  would invalidate the whole score history to record that nothing about the
  arithmetic moved. Prove it rather than assert it: a test asserting an
  unclassified and a classified operating company produce identical scores is
  what makes this rule checkable.

  Historical snapshots are never rewritten either way. A company excluded today
  keeps the rows it earned under the rules of the day it was scored.
- **Not every company gets a number.** A bank, an ineligible security and a
  company with two quarters of history get a stored row carrying the status that
  says why. Forcing a score onto them would put meaningless values into a
  ranking that sorts on exactly that field.
- **No metered provider gates the market.** The broad scan runs on Alpaca and
  EDGAR alone; FMP is optional enrichment for the top candidates. A `PRELIMINARY`
  ranking is a valid ranking; `FINAL` only means a vendor verified the market cap
  and consolidated liquidity. A provider failure or `429` must never prevent
  scanning, scoring or ranking. A figure that was calculated rather than supplied
  carries its source — never let one become indistinguishable from the other.

## Layout

```
src/stock_screener/   the application — the Python backend and API
tests/                tests for src/, split unit/ and integration/
packages/<name>/      shared libraries, each a workspace member
frontend/             the Compounder Radar dashboard — Next.js, reads the API
migrations/           alembic revisions — one per schema change
fixtures/             sample data the mock providers read
docs/                 mkdocs site, organised by Diátaxis type
scripts/              standalone PEP 723 scripts — created when first needed
```

Where new Python code goes:

- **One caller** → a module in `src/stock_screener/`.
- **Two or more callers** → a package in `packages/`. Never create one
  speculatively; extracting later is cheap.

### Two deployables, one repository

This repo builds **exactly two** deployables, and that is a deliberate exception
to the one-deployable rule it used to state:

| Deployable | Is | Owns |
| --- | --- | --- |
| `src/stock_screener/` | the Python backend, CLI and read-only API | all data, every calculation, every score |
| `frontend/` | the Compounder Radar dashboard, Next.js | rendering what the API returns |

They live together on purpose. The dashboard is a reading surface over this
API and nothing else — its types mirror these endpoints, and a change to a
response shape has to land on both sides in the same commit. Splitting them
across repositories would buy nothing and cost that atomicity. **Do not move
the frontend to another repository.**

The boundary between them is the rule that matters: **the frontend calculates
nothing.** Every score, subscore, metric and claim on a screen came out of the
database through `stock_screener.api`. Ranking and scoring arithmetic is
backend-only, so the dashboard and the CLI cannot disagree.

A third deployable is not covered by this exception. Adding one — a worker, a
second service, a separate admin app — needs a deliberate architecture
decision recorded as an ADR first, and is more likely to belong in its own
repository.

Documentation is classified by Diátaxis — `docs/tutorials/`, `docs/how-to/`,
`docs/reference/`, `docs/explanation/`, plus `docs/adr/` for decisions. Every
page declares `type:` in frontmatter and belongs to exactly one.

## Naming

Every shared package has three linked names, derived mechanically. Taking
`billing` as an example:

| Form | Example | Where |
| --- | --- | --- |
| directory | `billing` | `packages/billing/` |
| distribution | `billing` | `name` in its `pyproject.toml` |
| import | `billing` | `src/billing/`, and in every import |

A hyphenated name converts to underscores for the import form only:
`api-clients` on disk becomes `api_clients` in Python. kebab-case on disk and in
metadata, snake_case in code. Never invent a fourth form, and never let the
import name drift from the directory name.

```python
from billing import Invoice  # correct
from api_clients import Client  # correct — api-clients on disk
import packages.billing  # wrong — never do this
```

## Commands

Run these through `make`; it wraps `uv run` so the right environment is always
used. Never invoke `pip`, `python -m venv`, or a bare `python`.

| Command | Purpose |
| --- | --- |
| `make check` | Everything CI runs. Run this before saying you are done. |
| `make check-web` | The frontend gate: tsc, ESLint, `next build`. Needs Node. |
| `make dev` | Run the API and the dashboard together. Ctrl-C stops both. |
| `make test` | pytest with coverage (fails under 80%) |
| `make lint` / `make format` | Ruff check / Ruff write |
| `make types` | mypy strict |
| `make sync` | Reinstall the workspace after dependency changes |

`make check` is the Python gate and does not require a node toolchain — that is
why `check-web` is separate rather than a prerequisite. Touching `frontend/`
means running both.

## Non-negotiables

- **Type everything.** mypy runs in strict mode. No bare `Any`, no untyped
  defs, no blanket `# type: ignore` — narrow it to a code and explain why.
- **Never read `os.environ` directly.** Add a typed field to
  `src/stock_screener/config.py` and a matching line in `.env.example`.
- **Never commit a secret.** Not in code, tests, fixtures, or docs. `.env` is
  gitignored; `.env.example` holds names with placeholder values only.
- **Never edit `uv.lock` by hand.** Use `uv add` / `uv remove`.
- **Never leave a failing or skipped test** without saying so explicitly.
- **Public API is `__init__.py`.** Anything a package exports lives in
  `__all__`. Reaching into a package's submodules from outside is a bug.
- **Docstrings are Google style** on every public module, class, and function.
- **Ask before** deleting files, rewriting history, force-pushing, or changing
  anything under `.github/workflows/`.

## Reporting back

State what you changed, what you ran, and what the result was. If `make check`
did not pass, say that plainly and show the failure — do not describe work as
finished when it is not. If you skipped part of a task, say which part and why.
