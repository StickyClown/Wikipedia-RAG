# ExecPlan 32: Parser Sandboxing And Autoscaling

Status: implemented
Date: 2026-07-30

## Goal

Harden parser runtime boundaries for Docker Compose and make the worker scale across parser endpoint pools without letting Docling backpressure block the fast Xberg route.

## Scope

- Compose parser services now use `no-new-privileges`, dropped capabilities, read-only root filesystem where feasible, tmpfs temp space and bounded CPU/memory/PID limits.
- Worker/API settings support `XBERG_URLS`, `DOCLING_URLS`, `DOCUMENT_PARSER_XBERG_CONCURRENCY` and `DOCUMENT_PARSER_DOCLING_CONCURRENCY`.
- Worker parser calls use separate Xberg/Docling semaphores and round-robin endpoint pools.
- Parser runtime metadata records safe numeric queue wait, parser latency and endpoint pool size; job progress exposes these fields without leaking endpoint URLs.

## Runtime

Scale parser services in Compose with explicit endpoint pools:

```bash
docker compose up -d --scale xberg=2 --scale docling=1
```

When using scaled replicas, set service-reachable endpoint pools:

```env
XBERG_URLS=http://xberg:8000
DOCLING_URLS=http://docling:5001
DOCUMENT_PARSER_XBERG_CONCURRENCY=2
DOCUMENT_PARSER_DOCLING_CONCURRENCY=1
```

## Validation

```text
uv run pytest tests\unit\test_document_ingestion.py -q
-> exit 0

docker compose config --quiet
-> exit 0
```
