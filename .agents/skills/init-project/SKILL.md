---
name: init-project
description: Initialise a fresh copy of this template into a real project. Interviews the user for the project name, description, author, and the shared packages they want, then renames the root module, rewrites the placeholder metadata, creates each package, and verifies the result. Use only when explicitly invoked on a fresh clone of the template.
disable-model-invocation: true
argument-hint: "[project-name]"
---

# Initialise project

Turns this template into a named project. Run once, on a fresh clone.

You do this yourself with file edits — there is no script. Work through the
steps in order and verify at the end.

The template ships **no packages**. Whatever the user names in the interview is
what gets created; never invent one that was not asked for.

## Step 0 — Confirm it is uninitialised

```bash
grep -q '^name = "app"' pyproject.toml && echo "TEMPLATE — ok to init" \
  || echo "ALREADY INITIALISED — stop and confirm with the user"
```

If it is already initialised, stop and ask. Running this twice renames things
twice and produces a mess that is tedious to unpick.

## Step 1 — Interview

Ask all of these in **one** message, not one at a time. Show the defaults and
let the user accept them wholesale.

1. **Project name** — kebab-case, e.g. `invoice-service`. Becomes the
   distribution name; the import name is its snake_case form
   (`invoice_service`), and the root module becomes `src/invoice_service/`.
   Validate: lowercase, starts with a letter, hyphen-separated. If the user
   gives spaces or capitals, convert and show what you converted to.

2. **Description** — one line. Goes in `pyproject.toml`, the README, the docs,
   and `AGENTS.md`.

3. **Author** — name and email. Default to git config:
   ```bash
   git config user.name; git config user.email
   ```

4. **Shared packages** — ask open-ended:

   > What shared packages should I create under `packages/`? Give me
   > kebab-case names, or say none and add them later.

   Do not present a fixed menu as if it were the only choice. If the user is
   unsure, offer these as *examples of the kind of thing that belongs there*,
   making clear they are suggestions, not defaults:

   | Example | For a project that… |
   | --- | --- |
   | `data-access` | talks to a database |
   | `api-clients` | calls external HTTP services |
   | `domain` | has business logic worth isolating from I/O |
   | `messaging` | publishes or consumes a queue |

   Say plainly: **none is a valid answer.** An empty package is pure overhead —
   a `pyproject.toml`, a test suite, and a CI entry for nothing. Adding one
   later is a two-minute job.

5. **Python version** — default `3.13`, floor `>=3.12`. Only ask if the user
   hints at a constraint.

Validate every name before proceeding: lowercase kebab-case, free on PyPI
(`uv pip index versions <name>`), and not a stdlib module name. Catch it here so
the user can pick again rather than discovering it later.

## Step 2 — Rename the root module

`IMPORT` below is the project name with hyphens converted to underscores.

1. `git mv src/app src/IMPORT` (or `mv` outside a repo).

2. Rewrite every reference. Use word boundaries — a blind `app` → `IMPORT`
   replacement corrupts `application`, `apply`, and `happy`:

   ```bash
   rg -l --hidden '\bfrom app\.|\bimport app\b|"app"|\bsrc/app\b|^site_name: app$' \
      --glob '!uv.lock' --glob '!.venv' --glob '!.agents' --glob '!.git'
   ```

   `--hidden` is not optional. Without it ripgrep skips `.github/` and
   `.claude/` outright, and the rename silently misses every instruction file
   in them.

   The hits are: `pyproject.toml` (the `[project] name`, the `[project.scripts]`
   entry, `module-name`, and `known-first-party`), `src/IMPORT/*.py`,
   `tests/**`, `README.md`, `AGENTS.md`, `docs/**`, `.env.example`,
   `.github/copilot-instructions.md`, `.github/instructions/*.instructions.md`,
   and `.claude/rules/*.md`.

   `mkdocs.yml` needs `site_name: app` changed to the **project name** — it is
   the only hit that is not the import name, which is why the pattern above
   matches it separately. Leave `docs/adr/*` alone: an ADR records what was
   decided at the time and is not rewritten.

   Leave `.agents/skills/**` alone — the skills use `app` generically to mean
   "the root application", and rewriting them breaks that meaning.

## Step 3 — Rewrite the placeholder metadata

Replace these everywhere they appear, excluding `uv.lock`, `.venv/`, and
`.agents/`:

| Placeholder | Replace with |
| --- | --- |
| `TODO: one-line description of this project.` | the description |
| `TODO Author` | the author name |
| `todo@example.com` | the author email |
| `# app` (heading, start of line) | `# <project-name>` |

Then in `pyproject.toml` set `name = "<project-name>"` — the kebab-case
distribution name, not the import name.

Delete the comments that describe the template to itself, which are false once
this has run and are not caught by any pattern above:

- `pyproject.toml` — the two header comment lines starting "`/init-project`
  rewrites: name, version, description, authors…"
- `src/IMPORT/__init__.py` — the docstring line "Renamed from `app` to the
  project slug by `/init-project`."

Keep the comments that still guide future work, such as the `[tool.uv.sources]`
and `known-first-party` notes about registering a new package.

Also fill in the **Project** section of `AGENTS.md`, which currently says
`TODO: two or three sentences on what this project does.` Write those sentences
from what the user told you; ask if you cannot write them honestly.

## Step 4 — Create the requested packages

For each name the user gave, follow the `python-package` skill — read its
`templates.md` and write the five files, then make the three root
`pyproject.toml` edits. Repeat per package.

Skip this step entirely if the user asked for none. Do not leave `packages/` as
an empty directory; git does not track it and it only adds noise.

## Step 5 — Verify

```bash
uv sync --all-packages --all-groups
make check
```

Both must pass before you report success. Then confirm nothing was missed:

```bash
# 1. No placeholder metadata left
rg -n --hidden 'TODO Author|todo@example\.com|TODO: one-line description|TODO: two or three' \
   --glob '!uv.lock' --glob '!.venv' --glob '!.agents' --glob '!.git' .

# 2. No stray root-module references left
rg -n --hidden '\bfrom app\.|\bimport app\b|"app"|\bsrc/app\b|^site_name: app$' \
   --glob '!uv.lock' --glob '!.venv' --glob '!.agents' --glob '!.git' .
```

Both must come back empty. Any hit is a leftover — fix it. Run the second one
even if step 2 looked clean; it is the check that catches a rename that missed
`.github/` or `.claude/`.

The per-package `TODO` boundary sections are expected and stay; point the user
at them rather than inventing content.

## Step 6 — Hand back

Report what was set, then say exactly what remains for the user:

```
Initialised invoice-service.
  module      src/invoice_service/
  packages    billing, ledger
  checks      make check passed

Still yours to do:
  1. Fill in the TODO boundary sections in each packages/*/AGENTS.md
     and README.md
  2. cp .env.example .env
  3. git add -A && git commit -m "chore: initialise project from template"
  4. gh repo create invoice-service --private --source=. --push
  5. Authenticate the MCP servers: /mcp in Claude Code, then log in to
     GitHub and Atlassian
```

**Do not commit, and do not create the remote repository** unless asked.

## Rules

- Ask everything in one message, up front.
- Never guess the project name from the directory — ask.
- Never create a package the user did not name.
- Never leave the repo half-initialised. If a step fails, say so and stop rather
  than patching around it — a partial rename is worse than none.
