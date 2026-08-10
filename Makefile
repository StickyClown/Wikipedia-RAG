SHELL := /bin/sh
WIKI_LIMIT ?= 10000
TEST ?=
PROVIDER ?= mock
EVAL_COUNT ?= 150

.PHONY: bootstrap up dev-up down migrate lint format-check typecheck test-unit test-integration test-e2e smoke eval eval-smoke eval-generate eval-full eval-document-prepare eval-document-ingest eval-document-run eval-document-status check-all import-wiki-small import-wiki-full import-zim-small smoke-models release-gate demo-release-gate verify-document-upload reliability-smoke verify-document-corpus verify-cross-tenant-hardening deep-research-smoke deep-research-hard-smoke deep-research-hard-gate deep-research-matrix deep-research-tool-matrix

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

eval-smoke:
	uv run python -m wikipediarag.cli eval-smoke --count 10

eval-generate:
	uv run python -m wikipediarag.cli eval-generate --count $(EVAL_COUNT)

eval-full:
	uv run python -m wikipediarag.cli eval-full --count $(EVAL_COUNT)

eval-document-prepare:
	uv run python -m wikipediarag.cli eval-document-prepare $(EVAL_DOCUMENT_PREPARE_ARGS)

eval-document-ingest:
	uv run python -m wikipediarag.cli eval-document-ingest $(EVAL_DOCUMENT_INGEST_ARGS)

eval-document-run:
	uv run python -m wikipediarag.cli eval-document-run $(EVAL_DOCUMENT_RUN_ARGS)

eval-document-status:
	uv run python -m wikipediarag.cli eval-document-status --latest $(EVAL_DOCUMENT_STATUS_ARGS)

check-all: lint format-check typecheck test-unit test-integration test-e2e smoke eval

import-wiki-small:
	uv run python -m wikipediarag.cli import-wiki --limit $(WIKI_LIMIT) --wait

import-wiki-full:
	uv run python -m wikipediarag.cli import-wiki --full --wait

import-zim-small:
	uv run python -m wikipediarag.cli import-zim --limit $(WIKI_LIMIT) --wait

smoke-models:
	uv run python -m wikipediarag.cli smoke-models --provider $(PROVIDER)

release-gate:
	uv run python -m wikipediarag.cli release-gate

demo-release-gate:
	uv run python -m wikipediarag.cli demo-release-gate

verify-document-upload:
	uv run python -m wikipediarag.cli verify-document-upload

reliability-smoke:
	uv run python -m wikipediarag.cli reliability-smoke --down-after

verify-document-corpus:
	uv run python -m wikipediarag.cli verify-document-corpus $(DOCUMENT_CORPUS_ARGS)

verify-cross-tenant-hardening:
	uv run python -m wikipediarag.cli verify-cross-tenant-hardening $(CROSS_TENANT_HARDENING_ARGS)

deep-research-smoke:
	uv run python -m wikipediarag.cli deep-research-smoke $(DEEP_RESEARCH_SMOKE_ARGS)

deep-research-hard-smoke:
	uv run python -m wikipediarag.cli deep-research-hard-gate $(DEEP_RESEARCH_SMOKE_ARGS)

deep-research-hard-gate:
	uv run python -m wikipediarag.cli deep-research-hard-gate $(DEEP_RESEARCH_SMOKE_ARGS)

deep-research-matrix:
	uv run python -m wikipediarag.cli deep-research-matrix $(DEEP_RESEARCH_MATRIX_ARGS)

deep-research-tool-matrix:
	uv run python -m wikipediarag.cli deep-research-tool-matrix $(DEEP_RESEARCH_TOOL_MATRIX_ARGS)
