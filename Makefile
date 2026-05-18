ROOT := $(shell pwd)

.PHONY: help install test lint format typecheck check clean supervisor supervisor-test supervisor-check install-supervisor

help:
	@echo "zicato Makefile targets:"
	@echo "  install            Install package + all optional dependencies via uv"
	@echo "  test               Run pytest"
	@echo "  lint               Run ruff check"
	@echo "  format             Run ruff format"
	@echo "  typecheck          Run mypy over src/zicato/"
	@echo "  check              Run lint + typecheck + test"
	@echo "  clean              Remove build, cache, and generated artifacts"
	@echo "  supervisor         Build the Rust zicato-supervisor binary (release)"
	@echo "  supervisor-test    Run the supervisor's cargo tests"
	@echo "  supervisor-check   Build + clippy + fmt + test the supervisor"
	@echo "  install-supervisor Copy the built supervisor binary to ~/.local/bin"

install:
	@cd $(ROOT) && uv sync --all-extras

test:
	@cd $(ROOT) && uv run pytest tests/

lint:
	@cd $(ROOT) && uv run ruff check .

format:
	@cd $(ROOT) && uv run ruff format .

typecheck:
	@cd $(ROOT) && uv run mypy src/zicato/

check: lint typecheck test

clean:
	@rm -rf $(ROOT)/dist $(ROOT)/build $(ROOT)/*.egg-info
	@find $(ROOT) -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@find $(ROOT) -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@find $(ROOT) -type d -name .ruff_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@find $(ROOT) -type d -name .mypy_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf $(ROOT)/supervisor/target
	@echo "Clean done."

supervisor:
	@cd $(ROOT)/supervisor && cargo build --release

supervisor-test:
	@cd $(ROOT)/supervisor && cargo test

supervisor-check:
	@cd $(ROOT)/supervisor && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test

install-supervisor: supervisor
	@mkdir -p $(HOME)/.local/bin
	@cp $(ROOT)/supervisor/target/release/zicato-supervisor $(HOME)/.local/bin/zicato-supervisor
	@echo "Installed zicato-supervisor to $(HOME)/.local/bin/zicato-supervisor"
