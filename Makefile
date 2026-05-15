ROOT := $(shell pwd)

.PHONY: help install test lint format typecheck check clean

help:
	@echo "zicato Makefile targets:"
	@echo "  install     Install package + all optional dependencies via uv"
	@echo "  test        Run pytest"
	@echo "  lint        Run ruff check"
	@echo "  format      Run ruff format"
	@echo "  typecheck   Run mypy over zicato/"
	@echo "  check       Run lint + typecheck + test"
	@echo "  clean       Remove build, cache, and generated artifacts"

install:
	@cd $(ROOT) && uv sync --all-extras

test:
	@cd $(ROOT) && uv run pytest tests/

lint:
	@cd $(ROOT) && uv run ruff check .

format:
	@cd $(ROOT) && uv run ruff format .

typecheck:
	@cd $(ROOT) && uv run mypy zicato/

check: lint typecheck test

clean:
	@rm -rf $(ROOT)/dist $(ROOT)/build $(ROOT)/*.egg-info
	@find $(ROOT) -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@find $(ROOT) -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@find $(ROOT) -type d -name .ruff_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@find $(ROOT) -type d -name .mypy_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "Clean done."
