---
name: scripts
description: Add a maintenance, migration, or one-off script under scripts/. Covers PEP 723 inline dependencies, running with uv, argument parsing, safety rules for destructive operations, and when a script should become a real command instead. Use when the task needs a standalone script, a data backfill, a maintenance job, or automation that is not part of the application.
---

# Scripts

`scripts/` holds standalone tools: backfills, migrations, maintenance. They are
not part of the deployable and are not imported by it.

The directory does not exist in a fresh clone — create it with the first script.
Project scaffolding is **not** done here: creating a package is a procedure you
follow from the `python-package` skill, not a script you run.

## Self-contained with PEP 723

A script declares its own dependencies inline, so it runs anywhere `uv` exists
without touching the project environment:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28"]
# ///
"""One-line summary of what this script does.

Usage:
    uv run scripts/backfill_users.py --dry-run
"""
```

```bash
uv run scripts/backfill_users.py --dry-run
```

Prefer stdlib-only where possible — it makes the script instant to start and
keeps it type-checkable against the project's mypy config without extra installs.

## Rules

**Every script takes arguments, never hardcoded values.** Use `argparse` with
helpful `help=` text. A script with the target environment hardcoded gets run
against the wrong one eventually.

**Anything destructive defaults to a dry run.** Deleting, overwriting, or
mutating in bulk requires an explicit `--apply` (or `--yes`) to actually do it.
Print what *would* happen first.

```python
parser.add_argument(
    "--apply", action="store_true", help="actually perform the changes (default: dry run)"
)
```

**Print progress and a summary.** Long-running scripts that print nothing are
indistinguishable from hung ones. Say what was processed, skipped, and failed at
the end. `print()` is fine here — Ruff's `T20` is disabled for `scripts/`.

**Make it re-runnable.** Assume it will be interrupted halfway. Idempotent
scripts can be re-run; scripts that double-charge cannot.

**Exit with a meaningful code.** `return 0` on success, non-zero on failure, via
`raise SystemExit(main())`. Anything calling it needs that.

**Same standards as the rest of the repo.** Type annotations, docstrings, and
`make check` must pass. `make types` picks up `scripts/*.py` automatically once
the directory exists.

## When it shouldn't be a script

- Run on a schedule → a proper job in the application, with logging and alerting
- Run by users regularly → a subcommand of the app's CLI
- Imported by anything → a module in `src/` or a package

A script is for things done occasionally, by a developer, at a terminal.

## Checklist

- [ ] PEP 723 header with pinned-ish dependencies, or stdlib only
- [ ] Module docstring with a usage example
- [ ] `argparse` for all inputs; nothing hardcoded
- [ ] Destructive actions dry-run by default, `--apply` to commit
- [ ] Progress output and an end-of-run summary
- [ ] Idempotent / safe to re-run after interruption
- [ ] Meaningful exit code
- [ ] `make check` passes
