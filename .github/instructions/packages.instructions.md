---
applyTo: "packages/**"
---

# Shared packages

Follow the `python-package` skill in `.agents/skills/python-package/`.

- Directory `foo-bar` → distribution `foo-bar` → import `foo_bar`. Never drift.
- `src/<import_name>/__init__.py` is the entire public API. Exports go in
  `__all__`; anything else is internal.
- A package may depend on other packages, never on `src/app`.
- Dependency changes go in that package's own `pyproject.toml`.
