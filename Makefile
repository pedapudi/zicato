ROOT := $(shell pwd)

.PHONY: help install install-hooks test test-fast test-affected node-test lint import-lint format typecheck check clean supervisor supervisor-test supervisor-check install-supervisor

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

# The FULL suite: both tiers, which is what a merge needs. The explicit -m
# REPLACES the pyproject selector rather than intersecting with it, so this
# line has to restate the two terms the full suite still excludes (the Node
# shim, which `make node-test` owns, and the opt-in cascade measurement).
test:
	@cd $(ROOT) && uv run pytest tests/ -m "not node and not cascade_oc"

# The DEFAULT tier alone — the inner loop. A BARE `pytest` is what drops
# the seven tests measured at 15 s or more alone (tests/conftest.py), so this
# names no path: adding one would select it and run the tier too.
# Run `make test` before merging; pull requests expose both tiers in separate
# workflows so the quick result is available without hiding the slow result.
test-fast:
	@cd $(ROOT) && uv run pytest

# The tests the branch can reach, by static import graph — the inner loop
# narrowed further. NOT a gate: `tools/affected_tests.py` answers with the
# whole suite whenever it cannot establish what a change reaches, but a
# graph is not a proof, so `make test` is still what a merge needs. Pass a
# different range with `RANGE=HEAD~3`.
RANGE ?= origin/main...HEAD
test-affected:
	@cd $(ROOT) && uv run pytest $$(uv run python tools/affected_tests.py --range "$(RANGE)")

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
