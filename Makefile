ROOT := $(shell pwd)

.PHONY: help install install-hooks test test-fast node-test lint import-lint format typecheck check clean supervisor supervisor-test supervisor-check install-supervisor

# Path to the dashboard JS behaviour suite (run standalone under node).
JS_TEST_DIR := $(ROOT)/src/zicato/dashboard/static/test

help:
	@echo "zicato Makefile targets:"
	@echo "  install            Install package + all optional dependencies via uv"
	@echo "  install-hooks      Install the pre-commit git hook into .git/hooks/"
	@echo "  test               Run pytest (the full suite)"
	@echo "  test-fast          Run pytest without the slow real-subprocess/server tests"
	@echo "  node-test          Run the dashboard JS behaviour suite under node"
	@echo "  lint               Run ruff check"
	@echo "  import-lint        Run the import-linter library/driver contracts"
	@echo "  format             Run ruff format"
	@echo "  typecheck          Run mypy over src/zicato/"
	@echo "  check              Run lint + typecheck + test + node-test"
	@echo "                     (independent gates: run 'make -j4 check' to parallelize)"
	@echo "  clean              Remove build, cache, and generated artifacts"
	@echo "  supervisor         Build the Rust zicato-supervisor binary (release)"
	@echo "  supervisor-test    Run the supervisor's cargo tests"
	@echo "  supervisor-check   Build + clippy + fmt + test the supervisor"
	@echo "  install-supervisor Copy the built supervisor binary to ~/.local/bin"

install:
	@cd $(ROOT) && uv sync --all-extras

install-hooks:
	@cd $(ROOT) && uv run pre-commit install

test:
	@cd $(ROOT) && uv run pytest tests/

# The opt-in fast lane: drop the `slow`-marked real-subprocess / real-server
# tests (their runtime IS their coverage — run `make test` before merging).
# A command-line -m REPLACES the pyproject `-m 'not node'`, hence both terms.
test-fast:
	@cd $(ROOT) && uv run pytest tests/ -m "not slow and not node"

# The dashboard's JavaScript behaviour suite. The in-pytest shim
# (tests/test_dashboard_js.py) carries the `node` marker and is EXCLUDED
# from the default pytest run (`-m 'not node'` in pyproject) so it does
# not duplicate this run inside every pytest invocation. This target is
# the canonical standalone Node run and is wired into `make check`.
node-test:
	@cd $(JS_TEST_DIR) && node run-all.mjs

lint:
	@cd $(ROOT) && uv run ruff check .

# The library/driver import contracts ([tool.importlinter] in
# pyproject.toml): lib packages never import the drivers; only the two
# declared driver->driver edges exist.
import-lint:
	@cd $(ROOT) && uv run lint-imports

format:
	@cd $(ROOT) && uv run ruff format .

typecheck:
	@cd $(ROOT) && uv run mypy src/zicato/

# The five gates are independent (no shared state, distinct caches), so
# they parallelize cleanly: `make -j5 check` runs them concurrently and
# finishes in max(gate) instead of sum(gates). Sequential `make check`
# still works exactly as before.
check: lint import-lint typecheck test node-test

clean:
	@rm -rf $(ROOT)/dist $(ROOT)/build $(ROOT)/*.egg-info
	@find $(ROOT) -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@find $(ROOT) -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@find $(ROOT) -type d -name .ruff_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@find $(ROOT) -type d -name .mypy_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf $(ROOT)/target
	@echo "Clean done."

supervisor:
	@cd $(ROOT) && cargo build --release -p zicato-supervisor

supervisor-test:
	@cd $(ROOT) && cargo test -p zicato-supervisor

supervisor-check:
	@cd $(ROOT) && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test

install-supervisor: supervisor
	@mkdir -p $(HOME)/.local/bin
	@cp $(ROOT)/target/release/zicato-supervisor $(HOME)/.local/bin/zicato-supervisor
	@echo "Installed zicato-supervisor to $(HOME)/.local/bin/zicato-supervisor"
