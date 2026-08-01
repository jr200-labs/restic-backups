.DEFAULT_GOAL := help
.PHONY: help install install-deps init config-check lint check test build docs docs-preview

help:
	@echo "backups — top-level setup"
	@echo ""
	@echo "Targets:"
	@echo "  make install        install uv project dependencies"
	@echo "  make install-deps   brew bundle dependencies from ./Brewfile"
	@echo "  make init      init each enabled restic store referenced by a backup"
	@echo "  make config-check  validate config references and Glacier policy"
	@echo "  make lint       run Ruff checks"
	@echo "  make check      run ruff and mypy"
	@echo "  make test       run unit tests"
	@echo "  make build      build the Python package"
	@echo "  make docs      render the Quarto site to docs/_site"
	@echo "  make docs-preview  preview the Quarto site locally"
	@echo ""

install:
	@echo ">> install: uv project dependencies"
	uv sync

install-deps:
	@echo ">> install-deps: brew bundle dependencies from ./Brewfile"
	brew bundle --file ./Brewfile

init:
	@echo ">> init: initialize each enabled restic store referenced by a backup"
	@uv run --quiet restic-backups generic init

config-check:
	@uv run --quiet restic-backups check-config

check:
	uv run mypy .
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest

build:
	uv build

docs:
	uv run quarto render docs

docs-preview:
	uv run quarto preview docs
