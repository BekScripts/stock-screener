# Copilot instructions

The authoritative instructions for this repository are in
[AGENTS.md](../AGENTS.md). Read that file first and follow it.

Skills live in `.agents/skills/<name>/SKILL.md`, which Copilot discovers
automatically. Before writing code, load the skill covering the task — the
table in AGENTS.md maps tasks to skills. Path-scoped rules that load
automatically are in [.github/instructions/](instructions/).

Key points, repeated here because Copilot reads this file first:

- `make check` must pass before work is considered done.
- Packages: directory `api-clients` → distribution `api-clients` →
  import `api_clients`. Import from the package root: `from api_clients import X`.
- `src/stock_screener/` is the application, `packages/*` are shared libraries.
- Documentation is classified by Diátaxis: `docs/tutorials/`, `docs/how-to/`,
  `docs/reference/`, `docs/explanation/`. Every page declares `type:`.
- mypy runs in strict mode. Type every function.
- Configuration goes through `src/stock_screener/config.py`, never `os.environ`.
- Never commit secrets.
