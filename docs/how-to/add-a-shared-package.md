---
type: how-to
title: Add a shared package
---

# Add a shared package

Create a library under `packages/` that other parts of the workspace can import.

## Before you start

Confirm the code has **two or more consumers**, or needs to be testable on its
own. One caller means it belongs in `src/` — extracting later is cheap, an
unnecessary package is not.

Check the name is free on PyPI, since a workspace package shadows a real one
with the same name:

```bash
uv pip index versions <name>
```

Avoid names that shadow a stdlib module (`json`, `types`, `config`, `queue`).

## The quick way

Ask your agent:

> Add a shared package called `billing` for invoicing rules.

It follows the `python-package` skill, which carries the file templates and the
wiring steps. Review the diff as you would any change — the skill leaves `TODO`
markers in the new `README.md` and `AGENTS.md` for you to fill in, deliberately,
rather than inventing boundaries it wasn't told.

## By hand

The package is five files plus three edits to the root `pyproject.toml`. The
full templates live in `.agents/skills/python-package/templates.md`; the shape
is:

```
packages/billing/
├── pyproject.toml        name = "billing", module-name = "billing"
├── README.md             what it does, and what it does not
├── AGENTS.md             boundaries for agents working inside it
├── src/billing/
│   └── __init__.py       the public API — re-exports + __all__
└── tests/
    └── test_billing.py
```

Then in the root `pyproject.toml`, add `"billing"` to `[project] dependencies`,
`billing = { workspace = true }` to `[tool.uv.sources]`, and `"billing"` to
`known-first-party`. Missing the second means uv resolves the package from PyPI
instead of the workspace.

Finally:

```bash
uv sync --all-packages --all-groups
make check
```

## Export the public API

`src/billing/__init__.py` is the whole contract:

```python
from billing.invoices import Invoice, issue_invoice

__all__ = ["Invoice", "issue_invoice"]
```

Importing a submodule from outside the package is a bug — fix the export rather
than reaching in.

## To depend on it from another package

Two edits in the *consuming* `pyproject.toml`:

```toml
[project]
dependencies = ["billing"]

[tool.uv.sources]
billing = { workspace = true }
```

## Related

- `python-package` skill — the full checklist, naming rules, and templates
- [Architecture](../explanation/architecture.md) — why the boundary exists
