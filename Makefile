# Docker Disk Toolkit — common developer tasks.
# Uses `uv` when available, falling back to the active Python.

PYTHON ?= python
PKG := docker_disk_toolkit

.PHONY: help install dev test cov lint format typecheck build clean all

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package (editable) with dev extras
	uv pip install -e ".[dev]"

dev: install  ## Alias for install

test:  ## Run the test suite
	$(PYTHON) -m pytest

cov:  ## Run tests with coverage gate (>=80% enforced)
	$(PYTHON) -m pytest --cov=$(PKG) --cov-report=term-missing --cov-fail-under=80

lint:  ## Ruff + Black (check only)
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m black --check src tests

format:  ## Auto-format with Black + Ruff --fix
	$(PYTHON) -m ruff check --fix src tests
	$(PYTHON) -m black src tests

typecheck:  ## Static type checking (mypy strict)
	$(PYTHON) -m mypy

build:  ## Build wheel + sdist
	$(PYTHON) -m build

clean:  ## Remove caches and build artifacts
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

all: lint typecheck cov  ## Lint, type-check, and test with coverage gate
