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

Screens equities against configurable fundamental and technical filters. A
filter set is defined as data, evaluated against a universe of securities, and
the matches are returned for export or further analysis.

The screening rules themselves live in the `domain` package, deliberately free
of I/O. Market data arrives through `api-clients` (external HTTP providers) and
is persisted or cached via `data-access`. `src/stock_screener/` is the thin
deployable that wires those together behind a CLI.

## Layout

```
src/stock_screener/   the application — the deployable
tests/                tests for src/, split unit/ and integration/
packages/<name>/      shared libraries, each a workspace member
docs/                 mkdocs site, organised by Diátaxis type
scripts/              standalone PEP 723 scripts — created when first needed
```

Where new code goes:

- **One caller** → a module in `src/stock_screener/`.
- **Two or more callers** → a package in `packages/`. Never create one
  speculatively; extracting later is cheap.

This repo builds one deployable. A second service belongs in its own repository,
not a second directory here.

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
| `make test` | pytest with coverage (fails under 80%) |
| `make lint` / `make format` | Ruff check / Ruff write |
| `make types` | mypy strict |
| `make sync` | Reinstall the workspace after dependency changes |

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
