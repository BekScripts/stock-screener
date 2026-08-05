.DEFAULT_GOAL := help
.PHONY: help setup sync check lint format format-check types test test-ci \
        test-unit cov docs docs-build sync-skills clean

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
