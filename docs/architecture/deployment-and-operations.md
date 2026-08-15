# Deployment and Operations

WikipediaRag runs as a Compose-based development stack or against separately
managed compatible services. This document describes contracts, not a required
hosting topology.

## Required Runtime Capabilities

- API, worker and PostgreSQL.
- S3-compatible object storage.
- OpenSearch; Redis/Valkey is an optional performance dependency.
- Required parser endpoints for the selected ingestion policy.
- Model Gateway with healthy aliases for each operation used by the active
  retrieval/research profile.

Model endpoints may be remote or local. OpenRouter, vLLM, llama.cpp,
text-generation-webui, generic OpenAI-compatible endpoints and mock are adapter
implementations; none is an architectural prerequisite. The current development
revision uses OpenRouter. Business code and stage aliases remain unchanged when
connections are switched.

## Configuration

- Secrets come from environment variables, mounted secret files or the model
  control plane and are never written to exported YAML or logs.
- `config/models.yaml` is bootstrap/export input. An active persisted revision
  is the runtime source for Gateway alias resolution.
- `config/retrieval.yaml` binds retrieval profiles to operation aliases.
- Each connection declares an adapter and endpoint defaults; provider-specific
  fields terminate at the Gateway driver boundary.
- Chat, embeddings, rerank and token counting are validated independently.

## Readiness

- `/health` proves that a process responds.
- Gateway `/ready` validates the active control-plane snapshot and required
  alias checks with safe reasons.
- API `/ready` aggregates required dependency and worker health.
- A missing optional adapter or inactive local connection does not make a
  different healthy active alias invalid.
- Operational gates may require stricter preflight for the exact aliases and
  data path they measure; they must not silently fall back to mock.

## Operations

Use stable Make targets when available; on Windows run the equivalent `uv`,
`pnpm` or `docker compose` command.

Long ingestion, evaluation and research commands must expose stage, processed
count, total when known, last update and terminal failure. Use bounded batch
size/concurrency. After interruption, inspect remaining processes and durable
state before resuming.

Back up PostgreSQL and object storage consistently. Treat OpenSearch and Redis
as rebuildable. Never delete volumes, indices, databases or retained artifacts
without explicit approval and a resolved target.

## Safe Diagnostics

Readiness, logs and reports may include component, alias, operation, safe error
code, retryability, timing and revision identity. They do not include secrets,
database URLs, prompts, provider payloads, raw documents or object keys.
