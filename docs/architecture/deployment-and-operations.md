# Deployment And Operations

WikipediaRag currently supports a local Docker Compose production-shaped MVP.
External production deployment is planned work and must satisfy the requirements
below before being called supported.

## Local Docker Compose

Primary file: `compose.yaml`.

Processes and containers:

- `postgres`: PostgreSQL 16.4-alpine, port `5432`, volume `postgres-data`.
- `redis`: Valkey 8.0.2-alpine, port `6379`, healthcheck enabled.
- `minio`: MinIO, ports `9000` and `9001`, volume `minio-data`.
- `opensearch`: OpenSearch 2.17.1, port `9200`, single-node, security disabled for local use, volume `opensearch-data`.
- `otel-collector`: OpenTelemetry collector, ports `4317` and `4318`.
- `kiwix`: Kiwix serve, port `8083`, read-only `./zim:/data`.
- `xberg`: parser service, port `8091`, sandbox settings and cache volume.
- `docling`: CPU parser service, port `8092`, sandbox settings and cache volume.
- `metadata-service`: Python metadata extraction service, port `8090`.
- `mock-provider`: local model mock provider, port `8082`.
- `model-gateway`: model alias gateway, port `8081`.
- `api`: FastAPI API, port `8000`.
- `worker`: ingestion worker loop.
- `ui`: React/Vite UI, port `5173`.

Network boundary: Compose services communicate on the default Compose network.
Browser-visible local ports are published for UI, API, dependencies and helper
services. Parser services are reachable on host ports for local validation but
must still be treated as untrusted helpers.

Startup order:

- API waits for healthy PostgreSQL, Redis/Valkey, MinIO and OpenSearch and for Model Gateway service start.
- Worker starts after API, metadata-service, Xberg and Docling service start.
- UI depends on API service start.
- Model Gateway depends on mock-provider service start.

Health and readiness:

- API `/health` is liveness after startup.
- API `/ready` checks PostgreSQL, a fresh worker heartbeat, Model Gateway
  `/ready`, OpenSearch and MinIO. When parser services are configured as
  required, it also checks Xberg, Docling and metadata-service. Redis/Valkey
  remains degraded-only because the application has a fallback path.
- Model Gateway `/health` is liveness and `/ready` reports provider/alias readiness.
- Parser and storage containers have Compose healthchecks where configured.
- Redis/Valkey is a Compose dependency with a non-fatal cache path; MinIO and
  OpenSearch are included in API readiness.

Local development credentials and modes:

- `.env.example` contains local-only placeholder credentials.
- Default local auth user is `admin` / `admin`.
- `SESSION_COOKIE_SECURE=false` is used for local HTTP.
- `AUTH_DISABLED=true` is a local/demo bypass only.
- `MODEL_PROVIDER=mock` is the local fast path; OpenRouter validation requires
  an available key resolved by shared settings from `OPENROUTER_API_KEY`,
  `OPENROUTER_API_KEY_FILE` or `.env`, without printing the value.

Start:

```bash
cp .env.example .env
make up
```

Stop:

```bash
make down
```

If `make` is unavailable, inspect the Makefile and use the equivalent
`docker compose` and `uv` commands.

## Keycloak Smoke Profile

`compose.keycloak.yaml` adds a local Keycloak profile:

```bash
docker compose -f compose.yaml -f compose.keycloak.yaml --profile keycloak-smoke up -d keycloak
```

It uses pinned `quay.io/keycloak/keycloak:26.7.0`, imports
`infra/keycloak/wikipediarag-realm.json` and mounts local smoke secret fixtures.
Those fixtures are not production credentials.

## Optional llama.cpp Profile

`compose.llamacpp.yaml` defines optional local model containers behind Model
Gateway aliases. This is not the primary supported provider path yet. Model
choices, checksums, hardware sizing and quality thresholds remain open.

## Isolated Deep Research Hard Gate

`make deep-research-hard-gate` runs the hard Deep Research fixture pack in a
fresh Compose project rather than the operator's shared stack. The CLI creates
a unique project name and loopback-only API, MinIO and PostgreSQL ports, then
passes the isolated endpoints through API auth, presigned upload and ACL-viewer
setup. `compose.deep-research-gate.yaml` gives PostgreSQL, MinIO and
OpenSearch project-scoped volumes. The gate stops only its containers when it
finishes and intentionally retains volumes for diagnostics; it does not remove
shared-stack containers or volumes. Startup retries at most three times and
only for an identified port conflict.

The default gate uses `upload_sota_mvp`, `MODEL_PROVIDER=openrouter`, Qwen
aliases through Model Gateway and one 900-second post-readiness deadline shared
by upload, ingestion and research lifecycle waits. `--skip-compose` is the
explicit mode for a separately managed API. Hard-gate reports are written under
`artifacts/validation/deep-research-hard-gate/<timestamp>/` with safe project
and endpoint metadata only: no database URL, key value, raw document text,
planner prompt, provider payload, storage object key or raw tool query.

The required mock preflight passed one synthetic alias chain end-to-end. The
full OpenRouter/Qwen default-45% baseline did not complete the four-case pack
within the shared deadline, so it is an infrastructure/provider runtime
diagnostic, not a basis for changing the 45% default. Run a clean default
baseline before any isolated 35% candidate comparison.

## Reliability Foundation V1

The local Compose runtime uses a bounded, durable reliability contract:

- `OperationDeadline` uses a monotonic clock. Chat has a 300-second root
  deadline, emits heartbeats during long retrieval/generation stages, and
  propagates the remaining time plus a correlation ID into every normal-RAG
  Model Gateway call.
- Safe public failures contain only request/operation IDs, stage, attempt,
  retryability, safe code and remaining deadline. They never contain document
  text, prompts, provider payloads, object keys, stack traces or secrets.
- Only pre-response connect/read timeouts and HTTP `429`/`502`/`503`/`504` are
  retryable; the default is two total attempts. Validation, authorization,
  checksum, parser rejection, retrieval-contract mismatch and cancellation are
  terminal.
- Model Gateway keeps an in-process circuit per model alias: three consecutive
  transient failures open it for 15 seconds and allow one half-open probe.
- `Idempotency-Key` is optional for legacy clients and supplied by the UI/eval
  paths for expensive asynchronous creation. The tuple `(tenant, actor, route,
  key)` stores an opaque body hash and a safe previous response for 24 hours.
- Upload items checkpoint a delayed retry before becoming claimable again. A
  worker lease loss cancels work before publication; published work is not
  automatically deleted or recreated.

Run the deterministic source checks before a live benchmark:

```bash
uv run pytest tests/unit/test_reliability.py tests/unit/test_eval_document_benchmark.py tests/integration/test_eval_runner.py -q
uv run ruff check src/wikipediarag tests
uv run ruff format --check src/wikipediarag tests
uv run mypy src/wikipediarag
```

## Backup And Restore Boundary

Authoritative restore boundary:

- PostgreSQL consistent dump.
- MinIO bucket mirror/export aligned with the DB point-in-time.
- Required local ZIM snapshots if Wikipedia rebuild reproducibility matters.

Derived/rebuildable:

- OpenSearch indices should be rebuilt from PostgreSQL chunks/artifacts and index metadata.
- Browser state is transient.
- Redis/Valkey contains only rebuildable public-search cache windows and must
  not be treated as durable.

Restore drills and automation are not yet implemented.

## External Deployment

External deployment is not currently supported as production operation. Before
using this system externally, require:

- HTTPS and a reverse proxy with correct public URLs.
- `SESSION_COOKIE_SECURE=true` and appropriate SameSite policy.
- Mounted secrets for app secret, bootstrap password, OIDC client secret and provider keys.
- Persistent PostgreSQL and S3-compatible object storage with backups.
- Restore drills and documented RPO/RTO.
- External identity provider configuration and tenant onboarding policy.
- Production OpenSearch topology and resource limits.
- Observability backend, retention, alerting and ownership.
- Parser isolation beyond local Compose defaults as needed.
- Malware scanning policy for uploaded files. Malware scanning is not implemented.
- Resource limits, worker concurrency policy and parser autoscaling plan.
- Explicit policy on whether user document contents may be sent to external model providers.

## Failure And Degraded Matrix

| Dependency failure | API readiness | User-visible effect | Recovery |
| --- | --- | --- | --- |
| PostgreSQL unavailable | `/ready` degraded or API unavailable | Auth, KBs, jobs, chat and document operations fail | Restore DB service or fail over; verify schema and data |
| Model Gateway unavailable or degraded | `/ready` degraded | Chat/generation/embedding/rerank and release gates fail or are blocked | Fix provider/gateway, restart gateway, rerun smoke-models |
| OpenRouter quota/access failure | Model Gateway `/ready` degraded for OpenRouter profiles | Provider-backed gates and real-model chat fail | Restore account access/credits/key and restart gateway |
| MinIO unavailable | `/ready` degraded | Upload, artifact read/write, reprocess and purge fail | Restore MinIO and verify bucket/object availability |
| OpenSearch unavailable | `/ready` degraded | Retrieval and publication fail; source data remains in DB/MinIO | Restore service or rebuild derived index |
| Worker heartbeat stale | `/ready` degraded | New ingestion, source sync and research work may not advance | Inspect worker logs, restore worker, then resume the durable job |
| Redis/Valkey unavailable | Degraded-only cache condition | Public search falls back to uncached retrieval; pagination may be slower | Restore Valkey or continue with the fallback path |
| Kiwix unavailable or ZIM missing | Not reflected in API `/ready` | ZIM import and source viewing fail | Mount valid ZIM and restart Kiwix/import |
| Xberg unavailable | Not reflected in API `/ready` | Default parser route fails; worker may use fallback or fail item | Restart/scale Xberg or route to Docling |
| Docling unavailable | Not reflected in API `/ready` | High-quality fallback parser fails | Restart/scale Docling or retry later |
| Metadata service unavailable | Not reflected in API `/ready` | Worker falls back to in-process metadata extraction | Restart metadata service |
| OpenTelemetry collector unavailable | Not reflected in API `/ready` | Observability degraded | Restart collector or external backend |

## Operational Checks

Common local checks:

```bash
make smoke
make smoke-models PROVIDER=mock
make verify-document-upload
make verify-document-corpus
make verify-cross-tenant-hardening
make deep-research-smoke
make deep-research-matrix
make deep-research-hard-gate
```

Provider-backed release gates must only start when API `/ready` is `ok` and,
for OpenRouter, Model Gateway alias smoke passes.
