# app

> TODO: one-line description of this project.

A Python monorepo template with a uv workspace, a shared skills library that
every AI coding agent reads, and CI wired up out of the box.

## Quick start

```bash
make setup               # create .venv, install everything, link agent skills
make check               # lint + types + tests
```

If this is a fresh copy of the template, run `/init-project` in your agent
(Claude Code, Codex, or Copilot). It interviews you for the project name,
description, author, and which shared packages you want, then scaffolds them.

## Layout

```
src/app/              the application — the deployable
tests/                tests for src/, split unit/ and integration/
packages/<name>/      shared libraries
docs/                 mkdocs site, organised by Diátaxis type
scripts/              standalone PEP 723 scripts — created when first needed
.agents/skills/       the skills every agent follows (single source of truth)
```

The template ships **no packages** — ask your agent for one and it follows the
`python-package` skill to create it, either during `/init-project` or later.

Every shared package has three linked names — directory `api-clients`,
distribution `api-clients`, import `api_clients`:

```python
from api_clients import Client
```

## Commands

| Command | What it does |
| --- | --- |
| `make check` | Everything CI runs: lint, format check, types, tests |
| `make test` | pytest with coverage |
| `make lint` / `make format` | Ruff check / Ruff write |
| `make types` | mypy strict |
| `make docs` | Serve the docs site locally |
| `make sync-skills` | Re-link `.agents/skills/` into `.claude/skills/` |

Full list: `make help`, or [docs/reference/commands.md](docs/reference/commands.md).

## Working with AI agents

Instructions live in [AGENTS.md](AGENTS.md), which every agent reads
(`CLAUDE.md` imports it, `.github/copilot-instructions.md` points at it).

Skills live **once** in [.agents/skills/](.agents/skills/) and load on demand.
Codex and Copilot read that directory natively; Claude Code only reads
`.claude/skills/`, so `make sync-skills` symlinks each skill across. There is
one copy of every skill on disk.

To add a rule, put it in the relevant skill rather than in AGENTS.md, and keep
AGENTS.md to facts that apply to every task.
