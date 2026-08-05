# Package file templates

Read this when creating a package. Substitute throughout:

| Placeholder | Means | Example |
| --- | --- | --- |
| `NAME` | kebab-case directory and distribution name | `api-clients` |
| `IMPORT` | `NAME` with hyphens → underscores | `api_clients` |
| `DESCRIPTION` | one line, ends with a period | `Outbound HTTP clients.` |
| `AUTHOR` / `EMAIL` | copied from the root `pyproject.toml` | `Ada Lovelace` |

Create exactly these five files. Do not add `__init__.py` to `tests/`, and do
not create a `conftest.py` unless the package actually needs a fixture.

---

## `packages/NAME/pyproject.toml`

```toml
[project]
name = "NAME"
version = "0.1.0"
description = "DESCRIPTION"
readme = "README.md"
requires-python = ">=3.12"
license = "MIT"
authors = [{ name = "AUTHOR", email = "EMAIL" }]

dependencies = []

[build-system]
requires = ["uv_build>=0.11,<0.12"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "IMPORT"
module-root = "src"
```

Leave `dependencies` empty. Add to it with `uv add --package NAME <dep>`, never
by hand — see the `dependency-management` skill.

---

## `packages/NAME/src/IMPORT/__init__.py`

```python
"""DESCRIPTION

This module is the package's public API. Consumers import from the package root
only::

    from IMPORT import something

Everything re-exported here is a supported contract. Anything not listed in
`__all__` is internal and may change without a version bump.
"""

__all__: list[str] = []

__version__ = "0.1.0"
```

---

## `packages/NAME/tests/test_IMPORT.py`

```python
import IMPORT


def test_IMPORT_imports() -> None:
    assert IMPORT.__version__
```

A placeholder so the package has a passing suite from the first commit. Replace
it with real tests as soon as the package does anything.

---

## `packages/NAME/README.md`

```markdown
# NAME

DESCRIPTION

## Use

​```python
from IMPORT import something
​```

To depend on this package, add it in two places in the consuming
`pyproject.toml`:

​```toml
[project]
dependencies = ["NAME"]

[tool.uv.sources]
NAME = { workspace = true }
​```

## Boundaries

TODO: state what belongs in this package and what does not.
```

---

## `packages/NAME/AGENTS.md`

```markdown
# NAME

DESCRIPTION

Agents working in this directory follow the repository-wide AGENTS.md plus the
`python-package` skill.

## Boundaries

TODO: what belongs here, and what must not.

## Public API

`src/IMPORT/__init__.py` is the only supported import surface. Adding to
`__all__` is a contract; removing from it is a breaking change.

## Dependencies

TODO: which other packages this may depend on. It must not import from the root
app.
```

---

## Then wire it into the root `pyproject.toml`

Three edits. All three are required; the second is the one people forget, and
without it uv resolves the package from PyPI instead of the workspace.

1. Add to `[project] dependencies`, above the `# Third-party` comment:

   ```toml
   dependencies = [
       # Workspace packages: ...
       "NAME",
       # Third-party runtime dependencies.
   ```

2. Add to `[tool.uv.sources]`, below its explanatory comment:

   ```toml
   NAME = { workspace = true }
   ```

3. Append the import name to ruff's first-party list so imports sort correctly:

   ```toml
   known-first-party = ["app", "IMPORT"]
   ```

Then run `uv sync --all-packages --all-groups` and `make check`.

Leave the `TODO` markers in `README.md` and `AGENTS.md` for the user to fill in.
Do not invent boundaries for a package whose purpose you were only told in one
line — point the user at them instead.
