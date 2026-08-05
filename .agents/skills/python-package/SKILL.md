---
name: python-package
description: Create, structure, rename, split, or remove a shared package under packages/. Covers the three-form naming rule (directory, distribution, import), pyproject.toml layout, workspace registration in the root pyproject, public-API boundaries, and inter-package dependencies. Use whenever the task mentions a shared package, a new library, "extract this into a package", moving code out of src/, or wiring one package to use another.
---

# Shared packages

## Decide first: does this need a package?

Create one only when code has **two or more real consumers**, or must be
deployable/testable on its own. One caller means it belongs in `src/app/`.
Extracting speculatively costs a pyproject, a test suite, and a CI matrix entry
for no benefit. If unsure, leave it in `src/` and extract later — that refactor
is cheap; an unnecessary package is not.

## Naming — three forms, derived mechanically

| Form | Rule | Example |
| --- | --- | --- |
| directory | kebab-case | `packages/api-clients/` |
| distribution | identical to the directory | `name = "api-clients"` |
| import | directory with `-` → `_` | `from api_clients import ...` |

Names are lowercase, start with a letter, and use single hyphens. No prefixes,
no suffixes like `-lib` or `-utils`, no plural/singular drift between forms.

Name the package for the concern it owns, not the layer it sits in. `billing`
and `api-clients` say something; `utils`, `common`, `helpers`, `core`, and
`shared` say only "things I could not classify", and they become dumping grounds.

**Before creating a package, check the distribution name is not taken on PyPI.**
A workspace package silently shadows a real one with the same name, which breaks
the moment anything adds the real package as a dependency:

```bash
uv pip index versions <name>    # no output / error means the name is free
```

If it is taken, pick something more specific.

Also reject anything that shadows a stdlib module — `json`, `types`, `logging`,
`config`, `queue`, `secrets`, `email`, `platform`, `select`, `test`. These fail
in ways that look like unrelated bugs, because the import silently resolves to
your package instead of the standard library.

## Create it

**Read [templates.md](templates.md) and write the five files it specifies**,
substituting the name, import name, description, and author. Then make the three
edits it lists in the root `pyproject.toml`, and run:

```bash
uv sync --all-packages --all-groups
make check
```

There is no scaffolding script. The templates are the instruction — read them at
creation time so a change to the templates takes effect immediately, rather than
reproducing them from memory.

The template ships no packages of its own. Everything under `packages/` was
asked for by the user, so never assume one exists — check before importing it.

## Layout

Every package is identical in shape:

```
packages/billing/
├── pyproject.toml            metadata + its own dependencies
├── README.md                 what it is, how to use it, what it excludes
├── AGENTS.md                 boundaries for agents working inside it
├── src/billing/
│   ├── __init__.py           THE public API — re-exports + __all__
│   └── <modules>.py          implementation
└── tests/                    tests for this package only
```

Tests live inside the package, never in the root `tests/`. Root `pyproject.toml`
collects them via `testpaths = ["tests", "packages/*/tests"]`.

## Public API

`__init__.py` is the contract. Everything importable from outside is re-exported
there and listed in `__all__`:

```python
from billing.errors import BillingError
from billing.invoices import Invoice, issue_invoice

__all__ = ["BillingError", "Invoice", "issue_invoice"]
```

- Adding to `__all__` is a feature. Removing or changing a signature in it is a
  breaking change — bump the minor version and note it in the package README.
- Importing a submodule from outside the package (`from billing.invoices import
  ...` in `src/app/`) is a bug. Fix the export, don't reach in.
- Keep `__init__.py` free of logic. Re-exports and `__version__` only.

## Depending on another package

Two edits, both in the **consuming** package's `pyproject.toml`. Missing the
second one makes uv resolve from PyPI instead of the workspace:

```toml
[project]
dependencies = ["billing"]

[tool.uv.sources]
billing = { workspace = true }
```

Then `uv sync`. Note that `[tool.uv.sources]` in the root applies workspace-wide,
but declaring it in the consuming package keeps it self-describing.

Dependency direction is one-way and must stay acyclic:

```
src/app  →  packages/*  →  packages/* (no cycles)
```

A package importing from `app` is always wrong — invert the dependency or move
the shared piece down into a package.

## Renaming or removing

Renaming touches five places. Miss one and the workspace breaks in a way uv
reports confusingly:

1. the directory under `packages/`
2. `name` and `[tool.uv.build-backend] module-name` in its `pyproject.toml`
3. the `src/<import_name>/` directory
4. every consuming `pyproject.toml` — both `dependencies` and `[tool.uv.sources]`
5. every `import` statement (`rg -l 'old_name'`)

Then `uv sync` and `make check`. To remove a package, confirm nothing imports it
(`rg 'from old_name|import old_name'`) before deleting.

## Checklist

- [ ] Two or more consumers, or genuinely standalone
- [ ] Named for a concern, not a layer — no `utils`/`common`/`helpers`
- [ ] Distribution name free on PyPI, not a stdlib module name
- [ ] All five files from `templates.md` created
- [ ] Public API re-exported in `__init__.py` with `__all__`
- [ ] Registered in `dependencies` **and** `[tool.uv.sources]` **and**
      `known-first-party`
- [ ] No dependency on `src/app`, no cycles
- [ ] `README.md` and `AGENTS.md` `TODO`s flagged to the user, not invented
- [ ] `uv sync --all-packages --all-groups && make check` passes
