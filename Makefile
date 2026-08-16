.DEFAULT_GOAL := help
.PHONY: help setup sync check check-web dev lint format format-check types test test-ci \
        test-unit cov migrate migrate-down scan docs docs-build sync-skills clean

UV := uv

# Optional trees, resolved at call time: mypy errors on a configured path that
# matches nothing, and packages/ and scripts/ may be absent or empty.
EXTRA_SRC := $(wildcard packages/*/src) $(wildcard scripts/*.py)

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup:  ## First-time setup: install everything and wire up agent skills
	$(UV) sync --all-packages --all-groups
	$(MAKE) sync-skills
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")
	@echo "Ready. Run 'make check' to verify."

sync:  ## Reinstall the workspace after dependency changes
	$(UV) sync --all-packages --all-groups

check: lint format-check types test  ## Everything CI runs — the gate before you push

# Deliberately not a prerequisite of `check`. The Python gate runs in CI without
# a node toolchain, and making it depend on one would mean every backend change
# waits for an npm install. Run this one when you touch frontend/.
#
# The build writes to its own `distDir`. Sharing one with `next dev` means
# running this gate during a dev session corrupts that session's module graph,
# and the error it throws names webpack rather than the cause.
check-web:  ## The frontend gate: typecheck, lint, production build (needs Node)
	cd frontend && npm run typecheck && npm run lint && NEXT_DIST_DIR=.next-check npm run build

API_PORT ?= 8000
WEB_PORT ?= 3000

# `trap 'kill 0'` signals the whole process group, not just the two children.
# Both `uvicorn --reload` and `next dev` spawn workers of their own, and killing
# only the direct children would leave those grandchildren holding the ports —
# which is what makes the *next* `make dev` fail with EADDRINUSE.
#
# The ports are checked first because that failure is otherwise a Node stack
# trace that says nothing about which process is in the way.
dev:  ## Run the API and the dashboard together; Ctrl-C stops both
	@command -v npm >/dev/null || { echo "npm is required for the dashboard"; exit 1; }
	@test -d frontend/node_modules || (cd frontend && npm install)
	@for port in $(API_PORT) $(WEB_PORT); do \
	  holder=$$(lsof -nP -iTCP:$$port -sTCP:LISTEN -t 2>/dev/null | head -1); \
	  if [ -n "$$holder" ]; then \
	    echo "port $$port is already in use by pid $$holder ($$(ps -p $$holder -o comm= 2>/dev/null))"; \
	    echo "stop it, or choose other ports:  make dev API_PORT=8002 WEB_PORT=3002"; \
	    exit 1; \
	  fi; \
	done
	@echo "API  http://localhost:$(API_PORT)"
	@echo "Web  http://localhost:$(WEB_PORT)"
	@echo
	@trap 'kill 0' EXIT INT TERM; \
	  $(UV) run uvicorn stock_screener.api:app --reload --port $(API_PORT) & \
	  NEXT_PUBLIC_API_URL=http://localhost:$(API_PORT) \
	    npm --prefix frontend run dev -- --port $(WEB_PORT) & \
	  wait

lint:  ## Ruff lint
	$(UV) run ruff check .

format:  ## Ruff format (writes)
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

format-check:  ## Ruff format check (no writes)
	$(UV) run ruff format --check .

types:  ## mypy strict
	$(UV) run mypy src tests $(EXTRA_SRC)

test:  ## Full test suite with coverage
	$(UV) run pytest --cov --cov-report=term-missing

test-ci:  ## Test suite with an XML coverage report, for CI
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=xml

test-unit:  ## Fast loop — unit tests only, no coverage
	$(UV) run pytest -m unit -q

migrate:  ## Apply database migrations (alembic upgrade head)
	$(UV) run alembic upgrade head

migrate-down:  ## Roll back the last migration
	$(UV) run alembic downgrade -1

scan:  ## Run the full pipeline and print the eligible companies
	$(UV) run stock-screener run-scan

cov:  ## Coverage report as HTML
	$(UV) run pytest --cov --cov-report=html
	@echo "open htmlcov/index.html"

docs:  ## Serve the docs site locally
	$(UV) run mkdocs serve

docs-build:  ## Build the docs site
	$(UV) run mkdocs build --strict

sync-skills:  ## Symlink .agents/skills into .claude/skills for Claude Code
	@mkdir -p .claude/skills
	@find .claude/skills -maxdepth 1 -type l -delete
	@for dir in .agents/skills/*/; do \
	  name=$$(basename "$$dir"); \
	  ln -sfn "../../.agents/skills/$$name" ".claude/skills/$$name"; \
	done
	@echo "linked $$(ls -1 .agents/skills | wc -l | tr -d ' ') skills into .claude/skills/"

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov site dist build .coverage
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
