---
name: dependency-management
description: Add, upgrade, remove, or pin a dependency with uv. Covers which pyproject.toml to edit in a workspace, dependency groups, workspace sources, the lockfile, version constraints, and vetting a new dependency before adding it. Use when the task mentions installing a library, an import error, a version conflict, upgrading packages, or uv.lock.
---

# Dependencies

`uv` manages everything. **Never run `pip`, `python -m venv`, `poetry`, or edit
`uv.lock` by hand.** The lockfile is generated; a manual edit desynchronises it
from the pyprojects and produces failures that look like resolver bugs.

## Which pyproject?

The single most common mistake in this repo. The dependency goes in the
pyproject of the thing that **imports** it:

| The import appears in | Edit | Command |
| --- | --- | --- |
| `src/app/` | root `pyproject.toml` | `uv add httpx` |
| `packages/billing/src/` | `packages/billing/pyproject.toml` | `uv add --package billing httpx` |
| tests or tooling only | root, `dev` group | `uv add --dev pytest-mock` |

A package that imports a library it doesn't declare works locally — because
something else in the shared venv pulled it in — and breaks the moment the
package is used elsewhere. uv cannot catch this for you.

## Adding

```bash
uv add httpx                        # runtime dep, root app
uv add --package billing sqlalchemy       # runtime dep, specific member
uv add --dev pytest-mock            # dev tooling
uv add --group docs mkdocs-material # a specific group
```

`uv add` edits the right `pyproject.toml`, re-locks, and syncs in one step.

### Before adding anything, vet it

Every dependency is permanent surface area — supply-chain risk, upgrade burden,
and someone else's bugs. Check:

- **Is it needed?** Ten lines of stdlib beats a transitive tree of forty
  packages. `left-pad` problems are self-inflicted.
- **Is it maintained?** Recent releases, open issues being answered, more than
  one maintainer.
- **Is it the one already here?** Don't add `requests` when `httpx` is present,
  or `black` when Ruff formats. Check `pyproject.toml` first.
- **What does it pull in?** `uv add --dry-run <name>` shows the tree.

If a dependency is questionable, say so and ask before adding it.

## Workspace packages

A workspace dependency needs **two** entries. With only the first, uv silently
resolves it from PyPI:

```toml
[project]
dependencies = ["billing"]

[tool.uv.sources]
billing = { workspace = true }
```

`uv add --package app billing` does not add the source entry — add it
yourself, then `uv sync`.

## Constraints

- Runtime deps get a lower bound only: `"httpx>=0.28"`. The lockfile pins the
  exact version, so an upper bound just blocks compatible upgrades.
- Add an upper bound only for a known incompatibility, with a comment saying
  what broke and when to remove it.
- Build backend is the exception — `uv_build>=0.11,<0.12` is pinned on purpose.

## Upgrading

```bash
uv lock --upgrade                   # everything
uv lock --upgrade-package httpx     # one package
uv sync --all-packages --all-groups && make check  # always verify after
```

Upgrade in its own commit, never mixed with a feature. If `make check` fails
after an upgrade, the fix is to adapt the code or pin that one package with a
comment — not to revert the whole lockfile.

## Removing

```bash
uv remove httpx
rg 'import httpx|from httpx'        # confirm nothing still imports it
```

## Checklist

- [ ] Dependency vetted: needed, maintained, not already present
- [ ] Declared in the pyproject of the code that imports it
- [ ] Workspace deps have both `dependencies` and `[tool.uv.sources]`
- [ ] Lower bound only, unless an upper bound is justified in a comment
- [ ] `uv.lock` committed, never hand-edited
- [ ] `uv sync --all-packages --all-groups && make check` passes
