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
FORCE ?=

# `trap 'kill 0'` signals the whole process group, not just the two children.
# Both `uvicorn --reload` and `next dev` spawn workers of their own, and killing
# only the direct children would leave those grandchildren holding the ports —
# which is what makes the *next* `make dev` fail with EADDRINUSE.
#
# The ports are checked first because that failure is otherwise a Node stack
# trace that says nothing about which process is in the way.
#
# The holder is named by walking up from whatever `lsof` reports. A reloading
# uvicorn answers on its port from a multiprocessing fork child, so the pid
# holding the socket prints as a bare interpreter path and tells you nothing;
# its ancestor is the invocation you recognise.
#
# `make dev FORCE=1` frees the ports first — but only of *this* application.
# Ours is decided by finding our own uvicorn invocation in the holder's
# ancestry, never by the port number: 8000 is a port half the tools on a laptop
# want, and killing whatever answers there would eventually kill something that
# was not this. Anything unrecognised still just reports and stops.
#
# Worth knowing before forcing: jobs are spawned detached, so stopping the API
# does not stop a running ingest. It orphans it — the work continues, its exit
# code goes with the API process, and the row reconciles to UNKNOWN.
dev:  ## Run the API and the dashboard together; Ctrl-C stops both. FORCE=1 reclaims our own ports
	@command -v npm >/dev/null || { echo "npm is required for the dashboard"; exit 1; }
	@test -d frontend/node_modules || (cd frontend && npm install)
	@for port in $(API_PORT) $(WEB_PORT); do \
	  holder=$$(lsof -nP -iTCP:$$port -sTCP:LISTEN -t 2>/dev/null | head -1); \
	  [ -n "$$holder" ] || continue; \
	  ours=""; owner=$$holder; walk=$$holder; \
	  for _ in 1 2 3 4 5 6; do \
	    command=$$(ps -p $$walk -o command= 2>/dev/null); \
	    [ -n "$$command" ] || break; \
	    case "$$command" in *stock_screener.api*|*"--prefix frontend"*) ours=$$walk; owner=$$walk;; esac; \
	    parent=$$(ps -p $$walk -o ppid= 2>/dev/null | tr -d ' '); \
	    case "$$parent" in ''|0|1) break;; esac; \
	    walk=$$parent; \
	  done; \
	  label=$$(ps -p $$owner -o command= 2>/dev/null | cut -c1-100); \
	  if [ -z "$$ours" ]; then \
	    echo "port $$port is held by pid $$holder, which is not this project:"; \
	    echo "  $$label"; \
	    echo "stop it yourself, or choose other ports:  make dev API_PORT=8002 WEB_PORT=3002"; \
	    exit 1; \
	  fi; \
	  if [ -z "$(FORCE)" ]; then \
	    echo "port $$port is already served by this project (pid $$ours):"; \
	    echo "  $$label"; \
	    echo "reclaim it:  make dev FORCE=1"; \
	    echo "or run beside it:  make dev API_PORT=8002 WEB_PORT=3002"; \
	    exit 1; \
	  fi; \
	  echo "stopping our own pid $$ours on port $$port"; \
	  kill $$ours 2>/dev/null || true; \
	  for _ in 1 2 3 4 5 6 7 8 9 10; do \
	    lsof -nP -iTCP:$$port -sTCP:LISTEN -t >/dev/null 2>&1 || break; \
	    sleep 0.5; \
	  done; \
	  if lsof -nP -iTCP:$$port -sTCP:LISTEN -t >/dev/null 2>&1; then \
	    echo "port $$port is still held after asking pid $$ours to stop; not escalating"; \
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
