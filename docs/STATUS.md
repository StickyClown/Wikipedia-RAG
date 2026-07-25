# Project status

Last updated: 2026-07-25 12:59:54 +03:00

## Current phase

Local Docker-first MVP is implemented and running in development profile. The primary Wikipedia source is Wikimedia XML `pages-articles` bzip2 multistream; ZIM/libzim remains a future adapter.

## Active ExecPlan

`docs/exec-plans/02-wikipedia-ingestion.md` is the first active data-plan. MVP work also added foundation, retrieval, answer, debugger, upload, Extended Search, model-gateway and eval slices required for local demonstration.

## Completed

- [x] Governance realigned from ZIM-first to Wikimedia XML multistream via ADR-007.
- [x] `.gitignore` excludes `zip/`, compressed XML dumps, compressed multistream indexes and `openrouter_key.txt`.
- [x] Docker Compose stack: PostgreSQL, Valkey/Redis, MinIO, OpenSearch, OTel collector, API, worker, UI, Model Gateway and mock provider.
- [x] Python 3.12/uv backend package with FastAPI, async SQLAlchemy, deterministic mock embeddings and local tests.
- [x] React/Vite/TypeScript UI with chat, import progress, upload and retrieval debugger.
- [x] Wikipedia XML bzip2 multistream index validation, unique stream grouping, checkpoints, cancel/resume and restart recovery.
- [x] Namespace-0 article import preserving title, page ID, revision ID, timestamp, redirect target and source provenance.
- [x] Deterministic section-aware chunks with parent/neighbor links and deterministic chunk IDs.
- [x] BM25 + dense retrieval, RRF, rerank, citation validation and insufficient-evidence response.
- [x] Bounded Extended Search MVP with persisted evidence ledger and stop reason.
- [x] Upload of small UTF-8 documents for local demo.
- [x] Mock/OpenRouter-compatible Model Gateway; mock is default and requires no external key.
- [x] llama.cpp compose profile scaffold and provider smoke command.
- [x] Mini evaluation and release-gate commands.

## Validation evidence

- `docker compose up -d --build api worker`: exit 0 after schema advisory-lock fix.
- `docker compose run --rm api python -m wikipediarag.migrate`: exit 0, `database schema is ready`.
- `GET http://localhost:8000/ready`: exit 0, status `ok`, components `postgres=ok`, `model_gateway=ok`.
- `GET http://localhost:5173`: exit 0, HTTP 200.
- `uv run ruff check .`: exit 0, all checks passed.
- `uv run ruff format --check .`: exit 0, 30 files already formatted.
- `uv run mypy src tests`: exit 0, no issues in 27 source files.
- `uv run pytest tests/unit tests/integration tests/e2e`: exit 0, 10 passed.
- `cd services/ui && pnpm lint && pnpm typecheck && pnpm format:check && pnpm build`: exit 0; Vite production build succeeded.
- `uv run python -m wikipediarag.cli import-wiki --limit 10 --wait`: exit 0; job `c7f5cf7a-5da2-48b3-bd6f-3f715f97a330`, 10 pages imported, 473 chunks indexed.
- Restart/resume check: job `553d6ac3-3d86-477b-85d3-dcdb69b053da`, worker restarted at 100 pages/3991 chunks, completed after restart with 497 pages imported and 10262 chunks indexed.
- Upload check: `baikal-note.txt` indexed as one chunk; retrieval for `синий ирис Байкал` returned the uploaded document as top evidence.
- `uv run python -m wikipediarag.cli smoke`: exit 0; chat SSE events `run.started`, `message.delta`, `usage.updated`, `run.completed`.
- `POST /api/v1/search:debug` with `{"query":"Россия","top_k":5}`: exit 0; returned BM25, dense, RRF, rerank and context stages.
- `uv run python -m wikipediarag.cli eval`: exit 0; `wiki-mini` produced evidence for both demo cases.
- `uv run python -m wikipediarag.cli smoke-models --provider mock`: exit 0; mock chat, embedding and rerank aliases listed.
- `uv run python -m wikipediarag.cli release-gate`: exit 0; mini eval passed and printed `release gate passed`.

## Full Wikipedia import state

Full import has not been started. The command exists and is resumable:

```bash
make import-wiki-full
```

Routine acceptance currently uses limited imports because the local compressed XML is 6,135,514,301 bytes and the full import may take substantial disk and wall time.

## Environment notes

- Docker Desktop, Docker Compose and the running Compose stack are available.
- Host PowerShell has `uv` and `pnpm`.
- WSL has GNU Make and Docker, but did not expose `uv` and `pnpm` as Linux commands in this Codex environment. On a clean Windows/WSL2 setup, install `uv` and `pnpm` in WSL or invoke equivalent Windows commands directly.

## Known blockers and deferred work

- Real OpenRouter smoke test requires a user-supplied `OPENROUTER_API_KEY` in local `.env`.
- Real llama.cpp execution requires approved local GGUF model files, hardware/GPU choice, checksums and licensing decisions.
- Production OIDC/RBAC, audit log and auth-disabled production guard remain beyond the local MVP.
- Universal PDF/Office/image parsing via Docling/Tika is deferred; MVP upload supports small UTF-8 text documents.
- Alembic migration files are not yet introduced; MVP uses idempotent schema bootstrap with a PostgreSQL advisory lock.
