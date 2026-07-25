SHELL := /bin/sh
WIKI_LIMIT ?= 10000
TEST ?=
PROVIDER ?= mock

.PHONY: bootstrap up dev-up down migrate lint format-check typecheck test-unit test-integration test-e2e smoke eval check-all import-wiki-small import-wiki-full smoke-models release-gate

bootstrap:
	uv sync --all-groups
	cd services/ui && pnpm install

up:
	docker compose up -d --build

dev-up: up migrate

down:
	docker compose down

migrate:
	docker compose run --rm api python -m wikipediarag.migrate

lint:
	uv run ruff check .
	cd services/ui && pnpm lint

format-check:
	uv run ruff format --check .
	cd services/ui && pnpm format:check

typecheck:
	uv run mypy src tests
	cd services/ui && pnpm typecheck

test-unit:
	uv run pytest tests/unit $(if $(TEST),-k $(TEST),)

test-integration:
	uv run pytest tests/integration $(if $(TEST),-k $(TEST),)

test-e2e:
	uv run pytest tests/e2e $(if $(TEST),-k $(TEST),)

smoke:
	uv run python -m wikipediarag.cli smoke

eval:
	uv run python -m wikipediarag.cli eval

check-all: lint format-check typecheck test-unit test-integration test-e2e smoke eval

import-wiki-small:
	uv run python -m wikipediarag.cli import-wiki --limit $(WIKI_LIMIT) --wait

import-wiki-full:
	uv run python -m wikipediarag.cli import-wiki --full --wait

smoke-models:
	uv run python -m wikipediarag.cli smoke-models --provider $(PROVIDER)

release-gate:
	uv run python -m wikipediarag.cli release-gate
