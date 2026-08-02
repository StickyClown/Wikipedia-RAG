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
- API `/ready` checks PostgreSQL and Model Gateway `/ready`.
- Model Gateway `/health` is liveness and `/ready` reports provider/alias readiness.
- Parser and storage containers have Compose healthchecks where configured.
- Redis/Valkey, MinIO and OpenSearch are Compose dependencies but API readiness checks for them were not confirmed.

Local development credentials and modes:

- `.env.example` contains local-only placeholder credentials.
- Default local auth user is `admin` / `admin`.
- `SESSION_COOKIE_SECURE=false` is used for local HTTP.
- `AUTH_DISABLED=true` is a local/demo bypass only.
- `MODEL_PROVIDER=mock` is the local fast path; OpenRouter requires a real local API key.

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

## Backup And Restore Boundary

Authoritative restore boundary:

- PostgreSQL consistent dump.
- MinIO bucket mirror/export aligned with the DB point-in-time.
- Required local ZIM snapshots if Wikipedia rebuild reproducibility matters.

Derived/rebuildable:

- OpenSearch indices should be rebuilt from PostgreSQL chunks/artifacts and index metadata.
- Browser state is transient.
- Redis/Valkey state is not confirmed as used and must not be treated as durable.

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
| MinIO unavailable | Not currently reflected in API `/ready` | Upload, artifact read/write, reprocess and purge fail | Restore MinIO and verify bucket/object availability |
| OpenSearch unavailable | Not currently reflected in API `/ready` | Retrieval and publication fail; source data remains in DB/MinIO | Restore service or rebuild derived index |
| Redis/Valkey unavailable | Not currently reflected in API `/ready` | Not confirmed from the current implementation | Restore service if future transient users are added |
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
```

Provider-backed release gates must only start when API `/ready` is `ok` and,
for OpenRouter, Model Gateway alias smoke passes.
