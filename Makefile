ROOT := $(shell pwd)
VERIFY = uv run --no-sync python tools/verify.py

.PHONY: help install install-hooks test test-fast test-affected check-fast node-test lint import-lint format typecheck parity check clean supervisor supervisor-test supervisor-check install-supervisor

# Path to the dashboard JS behaviour suite (run standalone under node).
JS_TEST_DIR := $(ROOT)/src/zicato/dashboard/static/test

help:
	@echo "zicato Makefile targets:"
	@echo "  install            Install package + all optional dependencies via uv"
	@echo "  install-hooks      Install the pre-commit git hook into .git/hooks/"
	@echo "  test               Run pytest (both tiers — what a merge needs)"
	@echo "  test-fast          Run pytest (default tier only — the inner loop)"
	@echo "  test-affected      Run only the tests the branch's change can reach"
	@echo "  node-test          Run the dashboard JS behaviour suite under node"
	@echo "  lint               Run ruff check"
	@echo "  import-lint        Check architectural import contracts"
	@echo "  format             Run ruff format"
	@echo "  typecheck          Run mypy over src/zicato/"
	@echo "  parity             Run the parity oracle's golden gates"
	@echo "                     (Python suites and types have separate owners)"
	@echo "  check-fast         Run checks affected by branch and worktree changes"
	@echo "  check              Run the complete required verification plan"
	@echo "  clean              Remove build, cache, and generated artifacts"
	@echo "  supervisor         Build the Rust zicato-supervisor binary (release)"
	@echo "  supervisor-test    Run the supervisor's cargo tests"
	@echo "  supervisor-check   Build + clippy + fmt + test the supervisor"
	@echo "  install-supervisor Copy the built supervisor binary to ~/.local/bin"

install:
	@cd $(ROOT) && uv sync --all-extras

install-hooks:
	@cd $(ROOT) && uv run pre-commit install

# Iteration and complete verification share tools/verify.py with CI.
# RANGE requests a committed-only comparison for test-affected.
export AFFECTED_TEST_RANGE = $(RANGE)

test:
	@cd "$(ROOT)" && $(VERIFY) --only python-default,python-slow

test-fast:
	@cd "$(ROOT)" && $(VERIFY) --only python-default

test-affected:
	@cd "$(ROOT)" && $(VERIFY) --only python-affected

check-fast:
	@cd "$(ROOT)" && $(VERIFY) --mode iteration

check:
	@cd "$(ROOT)" && $(VERIFY) --mode complete

node-test:
	@cd "$(ROOT)" && $(VERIFY) --only dashboard-javascript

lint:
	@cd "$(ROOT)" && $(VERIFY) --only python-style

import-lint:
	@cd "$(ROOT)" && $(VERIFY) --only import-boundaries

format:
	@cd "$(ROOT)" && uv run --no-sync ruff format .

typecheck:
	@cd "$(ROOT)" && $(VERIFY) --only python-types

parity:
	@cd "$(ROOT)" && $(VERIFY) --only parity

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
	@cd "$(ROOT)" && $(VERIFY) --only rust-tests

supervisor-check:
	@cd "$(ROOT)" && $(VERIFY) --only rust-format,rust-clippy,rust-tests

install-supervisor: supervisor
	@mkdir -p $(HOME)/.local/bin
	@cp $(ROOT)/target/release/zicato-supervisor $(HOME)/.local/bin/zicato-supervisor
	@echo "Installed zicato-supervisor to $(HOME)/.local/bin/zicato-supervisor"
