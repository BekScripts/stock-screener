---
type: reference
title: Commands
---

# Commands

Every `make` target. Each wraps `uv run`, so the workspace environment is always
used.

| Target | Runs |
| --- | --- |
| `make setup` | `uv sync --all-packages --all-groups`, links agent skills, creates `.env` |
| `make sync` | `uv sync --all-packages --all-groups` |
| `make check` | `lint`, `format-check`, `types`, `test` |
| `make lint` | `ruff check .` |
| `make format` | `ruff format .` then `ruff check --fix .` |
| `make format-check` | `ruff format --check .` |
| `make types` | `mypy` over `src`, `tests`, and any packages/scripts present |
| `make test` | `pytest --cov --cov-report=term-missing` |
| `make test-unit` | `pytest -m unit -q` |
| `make cov` | `pytest --cov --cov-report=html` |
| `make docs` | `mkdocs serve` |
| `make docs-build` | `mkdocs build --strict` |
| `make sync-skills` | Symlinks `.agents/skills/` into `.claude/skills/` |
| `make clean` | Removes caches and build artifacts |

`make check` is what CI runs. Every check in CI is reachable through it.

## Test markers

| Marker | Selects |
| --- | --- |
| `unit` | Pure logic; no network, database, or filesystem |
| `integration` | Touches a real dependency |
| `slow` | Takes over a second |

```bash
uv run pytest -m unit
uv run pytest -m "not slow"
```
