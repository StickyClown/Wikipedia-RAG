# ExecPlan 10 — ZIM/Kiwix OpenRouter demo MVP

## Outcome

A local operator can place a real Russian Wikipedia ZIM under `./zim`, start Docker Compose, open the same archive through Kiwix, import exactly 10,000 canonical article pages through libzim, ask a Russian question, receive a cited answer generated through the Model Gateway/OpenRouter aliases, and click each source to the matching Kiwix article.

## Why this plan exists

`docs/production_rag_architecture.md` makes ZIM/libzim the specialized Wikipedia path for the production RAG architecture. The current code already has a working XML-based local MVP; this plan adds the ZIM/Kiwix/OpenRouter demo path without removing the XML adapter, SSE chat, checkpoints, debugger, Docker Compose or tests.

## In scope

- ZIM/libzim ingestion job and `make import-zim-small WIKI_LIMIT=10000`;
- Kiwix service using the same read-only `./zim` directory as the worker;
- `sota_mvp` typed retrieval profile and configurable pipeline switches;
- Model Gateway alias registry for OpenRouter chat, embeddings and rerank;
- 1024-d query/document embeddings through the Model Gateway for `sota_mvp`;
- ZIM source URL generation from exact libzim entry path;
- bounded deterministic Extended Search state expansion;
- UI advanced retrieval settings and clickable Kiwix source links;
- unit/integration/e2e tests and real-demo gate commands where local secrets/ZIM are available.

## Out of scope

- deleting or replacing the existing XML adapter;
- GraphRAG, multi-agent swarms, ColBERT, learned sparse or proposition indexing;
- universal PDF/Office/image parsing;
- local llama.cpp model qualification;
- production OIDC/RBAC hardening.

## Preconditions

- Python 3.12 with `uv`;
- Docker Compose;
- real demo requires `./zim/*.zim` and `OPENROUTER_API_KEY`;
- fixture tests must remain runnable without a large ZIM or a real provider key.

## Contracts and invariants

- Business code uses model aliases only and never physical OpenRouter slugs.
- `sota_mvp` must not silently fall back to mock or hash embeddings.
- Mock provider remains available only for explicit local/test profiles.
- Tenant filters stay server-side for BM25 and dense paths.
- Checkpoints advance only after durable DB/object-storage/OpenSearch writes.
- Source URLs are composed from `KIWIX_PUBLIC_BASE_URL`, resolved book name and exact `zim_entry_path`.
- Redirect entries are persisted as aliases/provenance but do not count toward the 10,000 canonical article limit.

## Milestones

### M10.1 Governance and configuration

- Add ADR superseding the XML-only demo source decision.
- Add model and retrieval config files.
- Add settings, Makefile targets and Compose Kiwix mounts.
- Validation: `uv run pytest tests/integration/test_contracts.py`.

### M10.2 ZIM ingestion

- Add libzim adapter, filtering, redirect accounting, exact source URLs, progress/checkpoints and ZIM job creation.
- Preserve the existing XML job path.
- Validation: `uv run pytest tests/unit -k "zim or wiki"` and `uv run pytest tests/integration -k zim`.

### M10.3 Gateway and retrieval

- Resolve aliases in the Model Gateway.
- Add OpenRouter capability smoke command.
- Route dense embeddings and rerank through the gateway according to `RetrievalProfile`.
- Add index-version naming keyed by profile and embedding dimensions.
- Validation: `uv run pytest tests/unit -k "retrieval or profile or model"` and `uv run pytest tests/integration -k retrieval`.

### M10.4 Answering, harness and UI debugger

- Generate through `generator_main`, validate structured citations and source URL correctness.
- Expand the bounded Python harness and debugger payload.
- Add closed advanced retrieval controls and clickable links.
- Validation: `uv run pytest tests/unit -k "extended or citation"` and UI lint/typecheck/build.

### M10.5 Release gate evidence

- Add fixture browser/e2e checks and real-demo gate.
- Update `docs/STATUS.md` with commands and actual results.
- Validation: `make lint`, `make format-check`, `make typecheck`, `make test-unit`, `make test-integration`, `make test-e2e`, `make smoke`, `make eval`, `make demo-release-gate` where prerequisites exist.

## Acceptance criteria

- `docker compose up` includes API, UI, worker, OpenSearch, Model Gateway and Kiwix.
- `make import-zim-small WIKI_LIMIT=10000` imports exactly 10,000 canonical non-redirect articles when a real ZIM is present.
- Worker restart resumes from checkpoint.
- Dense vectors for `sota_mvp` are obtained through Model Gateway/OpenRouter and are 1024-dimensional.
- Default `sota_mvp` executes BM25 + dense + RRF + rerank.
- BM25-only, rewrite-off and parent-expansion-off profiles reuse the same pipeline.
- Simple questions do not start the harness; multi-hop or insufficient evidence does.
- Duplicate tool calls stop the harness.
- Source links are normal anchors and resolve to Kiwix article URLs.
- No `sota_mvp` silent fallback to mock.

## Validation commands

```bash
make lint
make format-check
make typecheck
make test-unit
make test-integration
make test-e2e
make smoke
make eval
make smoke-models PROVIDER=openrouter
make demo-release-gate
```

## Demo

```bash
cp .env.example .env
# set OPENROUTER_API_KEY and place one Russian Wikipedia .zim in ./zim
docker compose up -d --build
make import-zim-small WIKI_LIMIT=10000
```

Then open `http://localhost:5173`, ask a Russian Wikipedia question and click a cited source.

## Rollback and recovery

Stop the stack with `make down`. To return to XML-only behavior, set `RETRIEVAL_PROFILE=test_mock`, `MODEL_PROVIDER=mock` and use existing `make import-wiki-small`. Do not delete `./zim`, `./zip`, database volumes or OpenSearch volumes without explicit operator approval.

## Progress

- [x] 2026-07-26: Plan created and scoped.
- [x] 2026-07-26: Governance/config/Compose/Kiwix/model/retrieval profile slice implemented.
- [x] 2026-07-26: ZIM adapter, ZIM import job path, source URL provenance, checkpoints and index-version naming implemented.
- [x] 2026-07-26: Profile-driven retrieval, gateway aliases, strict OpenRouter startup probes, answer validation, bounded harness and UI advanced controls implemented.
- [x] 2026-07-26: Deterministic local validation completed for lint, formatting, typecheck, unit/integration/e2e, UI and mock runtime smoke.
- [x] 2026-07-26: Real ZIM/Kiwix archive mounted and Kiwix content route validated after a clean Compose volume reset.
- [x] 2026-07-26: Real ZIM/OpenRouter import completed with exactly 10,000 canonical pages; worker restart/resume, Kiwix provenance, model smoke, cited SSE chat, browser click-through and demo release gate verified.

## Discoveries

- Current MVP stores XML source URLs from reconstructed titles; ZIM path must use exact libzim paths instead.
- Current dense retrieval scans DB hash embeddings; `sota_mvp` requires Model Gateway embeddings.
- YAML scalar `off` must be quoted, otherwise PyYAML loads it as boolean `false`.
- `libzim.writer.Creator` on this Windows host failed under user temp paths with Cyrillic characters; unit fixtures create temporary ZIMs under the ASCII workspace path.
- `kiwix-serve *.zim` exits cleanly when `./zim` is empty; the real demo gate must require `./zim/*.zim` before claiming readiness.
- Early ZIM setup failures must be inside the job failure handler; otherwise a missing `.zim` can leave a job in `running`.
- `kiwix-serve` exposes articles at `/content/<filename-stem>/<exact-entry-path>`; its archive identifier is the filename stem, not necessarily ZIM `Name` metadata.
- OpenRouter's default `/models` response excludes embedding and rerank models. Capability catalog checks must request `?output_modalities=all`.
- The operator replaced the OpenRouter key during the real run. The updated key passed catalog, embedding, typed JSON, streaming and rerank smoke checks.
- Real embedding latency is material for a 10,000-page import. The worker batches up to 96 chunks per provider request, submits at most eight independent requests concurrently, and persists a checkpoint only after the full batch is durable.
- A client-disconnected SSE request raised `asyncio.CancelledError`, which is not an `Exception` in Python 3.12. The API now marks that query run `ClientDisconnected` before re-raising cancellation.

## Decision log

- Redirects do not count toward `WIKI_LIMIT`; they are retained as alias/provenance metadata.
- Fixture tests may use generated/fake ZIM adapters; real demo gate is explicitly prerequisite-gated.
- Compose defaults remain mock/test profile friendly, while `.env.example` documents the `sota_mvp`/OpenRouter real-demo settings.
- `demo-release-gate` now fails before job creation unless a real `./zim/*.zim` and `OPENROUTER_API_KEY` are present.
- A valid real-provider key is a hard prerequisite for the `sota_mvp` import; no mock fallback is allowed.
- The API intentionally does not receive the OpenRouter key. `demo-release-gate` validates required aliases through Model Gateway and probes Kiwix through `KIWIX_INTERNAL_BASE_URL`, while published source links continue to use `KIWIX_PUBLIC_BASE_URL`.

## Final evidence

- `docker compose up -d --build`: exit 0; all non-Kiwix services started, Kiwix configured but exited because `./zim` is empty.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0.
- `uv run mypy src tests`: exit 0.
- `uv run pytest tests/unit tests/integration tests/e2e -q`: exit 0, 19 passed.
- `cd services/ui && pnpm lint`: exit 0.
- `cd services/ui && pnpm typecheck`: exit 0.
- `cd services/ui && pnpm format:check`: exit 0.
- `cd services/ui && pnpm build`: exit 0.
- `uv run python -m wikipediarag.cli smoke`: exit 0.
- `uv run python -m wikipediarag.cli eval`: exit 0.
- `uv run python -m wikipediarag.cli smoke-models --provider mock`: exit 0.
- `uv run python -m wikipediarag.cli smoke-models --provider openrouter`: exit 1, expected without key; no silent fallback.
- `uv run python -m wikipediarag.cli import-zim --limit 10000 --wait`: exit 1, expected without `./zim/*.zim`; job failed with `FileNotFoundError`.
- `uv run python -m wikipediarag.cli demo-release-gate`: exit 1, expected without real ZIM; gate blocked on prereq.
- 2026-07-26 real-run attempt: `docker compose down -v --remove-orphans` removed only the three named persistence volumes, then `docker compose up -d --build` mounted `wikipedia_ru_all_mini_2026-07.zim` successfully in Kiwix.
- 2026-07-26 Kiwix checks: root and `http://localhost:8083/content/wikipedia_ru_all_mini_2026-07/%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F` returned HTTP 200.
- 2026-07-26 OpenRouter catalog with `output_modalities=all`: all requested configured model slugs were present. Startup smoke then failed correctly on HTTP 401 from `/embeddings`; direct chat and embedding probes both reproduced 401. No ZIM job was created.
- 2026-07-26 updated-key smoke: `docker compose exec -T api python -m wikipediarag.cli smoke-models --provider openrouter --gateway http://model-gateway:8080`: exit 0; aliases available, `embed_default` returned 1024 dimensions, `generator_fast` returned typed JSON and `rerank_default` results were ordered.
- 2026-07-26 real ZIM job `be3f325a-a9f2-4a13-b83b-5e749de0148c`: status `completed`, `pages_imported=10000`, `chunks_indexed=14281`, `entries_scanned=19814`, `redirects_seen=9759`, `skipped_entries=35`; checkpoint entry index `19756`.
- 2026-07-26 restart/resume proof: worker was stopped at checkpoint `56` after 20 pages, restarted, and the same job advanced to 40 pages without a second import job. Later batch processing resumed from checkpoint `1874` and completed successfully.
- 2026-07-26 persistence/index proof: PostgreSQL contains 10,000 canonical ZIM documents and 14,281 chunks; every chunk embedding has length 1024. OpenSearch physical index `wiki-chunks-387df2fb225f794d` contains 14,281 documents.
- 2026-07-26 cited chat: question `Что такое 10-гигабитный Ethernet?` completed over SSE with valid citations, provider-returned usage/cost and `generator_main`; source `[S1]` was `http://localhost:8083/content/wikipedia_ru_all_mini_2026-07/10-%D0%B3%D0%B8%D0%B3%D0%B0%D0%B1%D0%B8%D1%82%D0%BD%D1%8B%D0%B9_Ethernet`.
- 2026-07-26 browser E2E: UI rendered the answer and its normal `[S1]` anchor; the exact Kiwix URL opened an article whose visible heading was `10-гигабитный Ethernet`.
- 2026-07-26 final checks: backend Ruff/format/mypy and `pytest tests/unit tests/integration tests/e2e -q` passed (19 tests); UI lint/typecheck/format/build passed; `smoke`, `eval` and `demo-release-gate --job-id be3f325a-a9f2-4a13-b83b-5e749de0148c` passed.
