---
name: init-project
description: Initialise a fresh copy of this template into a real project. Interviews the user for the project name, description, author, the shared packages they want, and how to handle template cleanup and the git remote; then renames the root module, rewrites the placeholder metadata, creates each package, strips the template-only scaffolding, verifies the result, and sets up the GitHub remote. Use only when explicitly invoked on a fresh clone of the template.
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

Interview the user in **two rounds**, live, before touching a single file. Two
rounds and no more: never trickle one question per message, and never assume an
answer you were not given.

The split is forced by the tooling. `AskUserQuestion` renders real choices the
user clicks, but every question needs two to four discrete options — the project
name and description have none, so they are asked as free text.

### Round 1 — free text, one message

1. **Project name** — kebab-case, e.g. `invoice-service`. Becomes the
   distribution name; the import name is its snake_case form
   (`invoice_service`), and the root module becomes `src/invoice_service/`.
   Validate: lowercase, starts with a letter, hyphen-separated. If the user
   gives spaces or capitals, convert and show what you converted to.

2. **Description** — one line. Goes in `pyproject.toml`, the README, the docs,
   and `AGENTS.md`.

3. **Author** — name and email. Default to git config, and show the values so
   the user can correct them:
   ```bash
   git config user.name; git config user.email
   ```

4. **Python version** — default `3.13`, floor `>=3.12`. Only ask if the user
   hints at a constraint.

### Round 2 — `AskUserQuestion`, one call, four questions

Ask these together. The first is about what to build; the other three each
authorise something destructive or outward-facing, which is exactly why they are
asked up front rather than sprung on the user at the end.

1. **Shared packages** — `multiSelect: true`. Do not present the options as if
   they were the only choice; they are *examples of the kind of thing that
   belongs under `packages/`*, and "Other" takes any kebab-case names the user
   types instead.

   | Example | For a project that… |
   | --- | --- |
   | `data-access` | talks to a database |
   | `api-clients` | calls external HTTP services |
   | `domain` | has business logic worth isolating from I/O |
   | `messaging` | publishes or consumes a queue |

   Make **None — add them later (recommended)** the first option and say why in
   its description: an empty package is pure overhead — a `pyproject.toml`, a
   test suite, and a CI entry for nothing. Adding one later is a two-minute job.

2. **Template scaffolding** — *Strip it (recommended)* or *Keep everything*.
   Strip removes this skill and the README's this-is-a-template framing; see
   Step 5 for the exact list.

3. **Git history** — *Keep the template's history (recommended)* or *Start
   fresh*. Start fresh deletes `.git` and re-initialises, which destroys the
   template's commits irreversibly. Never pick this for the user, and never
   act on it without this explicit answer.

4. **GitHub remote** — *Create a private repo and push*, *Create a public repo
   and push*, *Add a remote I already have*, or *Skip*. Creating a repo
   publishes the code under the user's account, so it happens only on an
   explicit choice here. If they pick *Add a remote I already have*, get the URL
   before Step 7.

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

## Step 5 — Strip the template scaffolding

Only if the user chose to strip it in Step 1. Skip the whole step otherwise.

What goes is the scaffolding that exists to turn the template into a project and
is false or dead once it has. Everything else stays — it is the project now.

1. **This skill.** It is single-use, and re-running it on an initialised repo
   renames everything twice. Delete the source and let the Makefile prune the
   Claude Code symlink:

   ```bash
   rm -rf .agents/skills/init-project
   make sync-skills          # deletes the stale .claude/skills/init-project link
   ```

   Then delete its row from the skills table in `AGENTS.md`:

   ```
   | `init-project` | setting up a fresh copy of this template |
   ```

2. **`README.md`.** Three edits, all of them the template describing itself:
   - the blurb under the title ("A Python monorepo template with a uv
     workspace…") — replace with a sentence about the project;
   - the "If this is a fresh copy of the template, run `/init-project`…"
     paragraph in Quick start — delete it;
   - "The template ships **no packages** — ask your agent for one…" — delete it
     if packages were created, otherwise reword without the `/init-project`
     reference.

3. **Comments that name the tooling that has now run:**
   - `pyproject.toml` — `# Console entry point. Renamed by /init-project
     alongside the module.` → `# Console entry point.`
   - `pyproject.toml` — `# init-project appends each package and app import
     name here.` → `# Add each package's import name here.` The note itself is
     worth keeping; only the dead reference goes.
   - `.github/copilot-instructions.md` — "The template ships no packages —
     never assume one exists." → drop that sentence, keeping the `src/<IMPORT>/`
     versus `packages/*` line it hangs off.

4. **Build artefacts**, so the first commit is clean:

   ```bash
   make clean                # caches, htmlcov, site/, dist, build
   ```

Leave `.agents/skills/**` otherwise untouched, and leave `docs/adr/*` alone. The
other skills are the project's working procedure, and an ADR records what was
decided at the time.

If the user chose **Start fresh** for git history in Step 1, and only then:

```bash
rm -rf .git && git init -b main
```

This is irreversible and destroys the template's commits. Confirm the answer is
what you think it is before running it; if there is any doubt, ask again.

## Step 6 — Verify

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

If the template scaffolding was stripped, one more:

```bash
# 3. No dangling references to the skill that just deleted itself
rg -n --hidden 'init-project' --glob '!uv.lock' --glob '!.venv' --glob '!.git' .
```

The per-package `TODO` boundary sections are expected and stay; point the user
at them rather than inventing content.

## Step 7 — Commit and set the remote

Driven entirely by the Step 1 answers. If the user chose *Skip*, do nothing here
and say so in the hand-back.

`make check` must have passed first. Do not commit a repo that does not build —
a red first commit is a bad starting point and hides which change broke it.

1. **Commit.** Creating a repo needs something to push, so the commit comes
   first:

   ```bash
   git add -A
   git commit -m "chore: initialise project from template"
   ```

   On a fresh history this is the initial commit; use
   `feat: initial commit` instead. Follow the `git-workflow` skill for the
   message either way.

2. **Remote.** Check authentication before assuming it works:

   ```bash
   gh auth status
   ```

   If `gh` is missing or not logged in, stop and tell the user — do not fall
   back to constructing an HTTPS remote by hand and hoping their credentials
   are cached.

   Then, for a repo they asked you to create:

   ```bash
   gh repo create <project-name> --private --source=. --push   # or --public
   ```

   For a remote they already have:

   ```bash
   git remote add origin <url>
   git push -u origin main
   ```

   Confirm the exact repo name and visibility back to the user in the same
   message you report the push — creating a public repo publishes their code,
   and the difference between `--private` and `--public` is not recoverable by
   deleting it afterwards.

If `gh repo create` fails because the name is taken, do not silently pick
another name. Report it and let the user choose.

## Step 8 — Hand back

Report what was set, then say exactly what remains for the user:

```
Initialised invoice-service.
  module      src/invoice_service/
  packages    billing, ledger
  template    scaffolding stripped, history kept
  checks      make check passed
  remote      github.com/you/invoice-service (private), pushed

Still yours to do:
  1. Fill in the TODO boundary sections in each packages/*/AGENTS.md
     and README.md
  2. cp .env.example .env
  3. Authenticate the MCP servers: /mcp in Claude Code, then log in to
     GitHub and Atlassian
```

Name any step the user declined rather than dropping it from the report — "no
remote set, you asked to skip it" is the useful line, not silence.

## Rules

- Ask everything up front, in the two rounds of Step 1 — never one question per
  message, and never mid-run once files are being written.
- Never guess the project name from the directory — ask.
- Never create a package the user did not name.
- Never delete `.git`, create a GitHub repo, or push without the explicit Step 1
  answer authorising it. An unanswered question is a no.
- Never leave the repo half-initialised. If a step fails, say so and stop rather
  than patching around it — a partial rename is worse than none.
