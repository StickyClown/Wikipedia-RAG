# Project Status

Last updated: 2026-07-30

## Current Phase

ExecPlan 25.1+25.2 is implemented for the local default-tenant RAG platform. The project now supports Russian Wikipedia ingestion plus async document uploads with universal metadata, isolated parser services and corpus verification. ExecPlan 24 production auth/onboarding is not implemented, so this is not a cross-tenant or external deployment readiness claim.

Latest reviewed Wikipedia provider gate remains:

- state: `completed`
- passed: `true`
- blocking_failures: `0`
- report: `artifacts/eval/release-gates/reviewed-wikipedia-smoke-v1/20260729T162834Z-reviewed-wikipedia-smoke-v1-release-gate-a8a2f1ea/report.json`

## Implemented Capabilities

- ZIM/libzim + Kiwix Wikipedia import with deterministic chunks, redirects as provenance, checkpoints and published OpenSearch index versions.
- XML multistream import remains as a regression/local fallback.
- Model Gateway liveness/readiness split, OpenRouter alias smoke checks, mock provider profiles and no direct provider calls from business code.
- Hybrid retrieval with BM25, dense vectors, RRF, rerank, dedup/page quota, parent expansion, answerability and citation validation.
- Bounded Extended Search MVP for controlled multi-query evidence expansion.
- Dated release-gate reports with configuration snapshots, one root run contract, execution path, child retrieval/tool contracts, safe failure fields and per-question step events.
- Async upload sessions: presigned MinIO upload first, completion second, background ingestion job after durable object visibility.
- Forward-only `ensure_schema` expansion for knowledge sources, upload batches/sessions, document versions/artifacts, ingestion job items and chunk locator/publication metadata.
- Universal document metadata on versions: upload/system timestamps, source/document dates, date candidates/source/confidence, detected language/confidence/alternatives, MIME/signature facts, parser route/version/options, hashes, warnings and safe public metadata.
- Isolated parser services: Xberg default parser, Docling Serve CPU fallback/high-quality parser and metadata-service for fast local language/date extraction.
- App-owned `NormalizedDocument` contract with stable blocks, tables, locators, parser reports, warnings and deterministic normalized/chunk hashes.
- Worker item claiming uses bounded `FOR UPDATE SKIP LOCKED`; failed/cancelled/parser-error jobs do not publish searchable chunks.
- UI upload panel creates sessions, uploads to presigned URLs, completes sessions, polls async progress and shows parser route, metadata and terminal errors.
- Document corpus verification uses generated fixtures plus optional pinned URL/SHA/license manifest samples.

## Validation Evidence

Latest local validation for the current working tree after the 2026-07-30 docs refresh:

```text
git status --ignored --short
-> exit 0, generated/secret local state remains ignored: .env, caches, artifacts/, services/ui/dist/, services/ui/node_modules/, zim/, zip/, openrouter_key.txt

git diff --check
-> exit 0, no whitespace errors; Git reported expected LF-to-CRLF working-copy warnings on Windows

rg -n --hidden --glob '!artifacts/**' --glob '!.git/**' --glob '!.venv/**' --glob '!services/ui/node_modules/**' --glob '!zim/**' --glob '!zip/**' --glob '!*.pyc' 'OPENROUTER_API_KEY=sk-|sk-or-v1|BEGIN .*PRIVATE KEY|password\s*=' .
-> exit 1, no matches for real OpenRouter keys, sk-or-v1 keys, private keys or password assignments beyond placeholders

uv run ruff check .
-> exit 0, All checks passed!

uv run ruff format --check .
-> exit 0, 76 files already formatted

uv run mypy src tests
-> exit 0, Success: no issues found in 74 source files

uv run pytest tests/unit tests/integration
-> exit 1, 2 contract-test failures after compact architecture rewrite; fixed by restoring required XML/ZIM architecture contract phrases

uv run pytest tests/unit tests/integration
-> exit 1, 1 contract-test failure after first fix; fixed by restoring "monotonic non-decreasing offsets"

uv run pytest tests\integration\test_contracts.py
-> exit 0, 4 passed in 0.06s

uv run pytest tests/unit tests/integration
-> exit 0, 128 passed, 4 warnings in 14.11s

cd services/ui; pnpm lint
-> exit 0

cd services/ui; pnpm typecheck
-> exit 0

cd services/ui; pnpm build
-> exit 0
```

Latest parser/corpus verification reports retained from the ExecPlan 25.1+25.2 implementation run:

```text
make verify-document-corpus
-> exit 1, GNU Make is not installed in this Windows host PATH

uv run python -m wikipediarag.cli verify-document-corpus --fixture-set standard
-> exit 0, passed=true, total=18, report_dir=artifacts\validation\document-corpus\20260729T205604Z

uv run python -m wikipediarag.cli verify-document-corpus --fixture-set full --skip-compose
-> exit 0, passed=true, total=21, report_dir=artifacts\validation\document-corpus\20260729T205923Z

uv run python -m wikipediarag.cli verify-document-corpus --fixture-set smoke --include-external --skip-compose
-> exit 0, passed=true, total=4, report_dir=artifacts\validation\document-corpus\20260729T210847Z

uv run python -m wikipediarag.cli verify-document-upload --skip-compose
-> exit 0, passed=true, report_dir=artifacts\validation\document-upload\20260729T210047Z
```

Parser image tags/digests recorded during implementation:

```text
ghcr.io/xberg-io/xberg:1.0.3
-> ghcr.io/xberg-io/xberg@sha256:69435354060fbf8495b102494536505a9c45142cd5392d5f79b98906f70fd69c

quay.io/docling-project/docling-serve-cpu:v1.28.0
-> quay.io/docling-project/docling-serve-cpu@sha256:cc207e1eb768878456ed98042c5d84fae56af3729a9c03d3e5c8fef393902956
```

## Local Data State

- Real ZIM pages imported: `10,000` canonical non-redirect pages.
- Real ZIM chunks indexed: `14,281`.
- OpenSearch index: `wiki-chunks-387df2fb225f794d`.
- Redirect provenance is persisted for the local ZIM snapshot.
- Document corpus reports and downloaded external bytes live under ignored `artifacts/`.

## Gitignore Audit

Tracked ignore policy covers current generated and sensitive local state:

- local secrets/config: `.env`, `.env.*`, `openrouter_key.txt`, `secrets/`;
- Python/tool caches: `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, coverage outputs;
- UI/build outputs: `node_modules/`, `dist/`, `build/`;
- runtime/generated artifacts: `artifacts/`, `data/`, `uploads/`, `zip/`;
- large local model/data files: `models/`, `*.zim`, `*.gguf`, XML dump patterns;
- keys/certs: `*.pem`, `*.key`.

No additional `.gitignore` change is required for the current commit. `config/document_corpus_manifest.json` is intentionally tracked because it contains only URLs, checksums, licenses and expected assertions, not downloaded corpus bytes.

## Known Limitations

- Production auth, tenant onboarding, role model and cross-tenant acceptance are not implemented.
- Document ingestion is local/default-tenant only and is not production-hardened for malware scanning, retention/deletion, ACL mirroring, parser autoscaling or external deployment.
- Public multi-file batch creation is not exposed yet; the DB/job framework supports independent job items and bounded worker claiming.
- Language/date metadata is deterministic and local but heuristic; binary/scanned files get final metadata only after parser/OCR text exists.
- OpenRouter-backed gates depend on provider quota, credits, latency and model behavior.
- Warm retrieval p95 has exceeded target in previous real evals and needs profiling.
- Large/legal corpus expansion should stay manifest-driven and outside ordinary CI until explicitly approved.

## Next Improvement Plan

Recommended next stage: harden document ingestion for production-adjacent operation without changing tenant/auth scope.

- Add cancellation/retry/reprocess coverage for multi-item batches exposed through public API.
- Add parser timeout/backoff metrics and structured parser quality reports.
- Add stronger document-date extraction precedence for SEC/contract-style metadata so taxonomy/schema dates do not outrank filing/document dates.
- Add optional nightly corpus gate for CUAD/CourtListener/SEC/GovInfo/EUR-Lex manifests with strict cache and checksum policy.
- Profile retrieval p95 and split latency by BM25, dense, rerank, context packing and generation.
