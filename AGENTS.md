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

**Phase 5 (run control) is complete.** The dashboard can now run the pipeline,
not just read it. `src/stock_screener/jobs.py` spawns existing CLI commands as
subprocesses and tracks them in the `jobs` table; the `/api/jobs` endpoints
start and report them; `frontend/app/jobs/` is the control panel. Also added:
ticker search, because the rankings cap at 500 rows over a universe of
thousands, and CSV export of a ranking view. Documented in
`docs/reference/project-phases/phase5.md`.

Three rules govern that layer:

- **A job is an existing command, never new pipeline code.** `JOB_KINDS` maps a
  key to a CLI argument list and is the entire allowlist — a caller picks a
  key, never an argument. Adding a stage means adding a CLI command first.
- **A job carries no result.** Every command persists what it produces, so a
  finished job is read through the endpoint serving that thing. A job record
  answers "is it running, did it work", and nothing else.
- **One run per kind.** Two concurrent market-wide runs would race on the same
  rows and double the load on the providers they read, so a second request
  returns the one in flight rather than starting a competitor.

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
