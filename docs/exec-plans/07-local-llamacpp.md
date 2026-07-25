# ExecPlan 07 — Local llama.cpp model serving

## Outcome

The same application and retrieval code can switch chat, embeddings and rerank aliases between OpenRouter and three separate local llama.cpp servers.

## Preconditions

All hardware and licensing questions in `docs/DECISIONS_REQUIRED.md` for local models are resolved.

## In scope

- pinned llama.cpp images and model artifact registry;
- chat/embed/rerank containers;
- qualification suite: tokenizer, templates, Russian/English, JSON, tools, citations, embeddings and reranker sanity;
- gateway provider adapter;
- concurrency/load/GPU metrics;
- policy-controlled fallback.

## Out of scope

Training/fine-tuning and automatic downloading of unapproved model artifacts.

## Acceptance criteria

- provider switch is configuration-only outside gateway;
- artifact checksums and licenses are recorded;
- qualification failures keep aliases unhealthy;
- load report includes VRAM, TTFT, throughput and concurrency;
- fallback does not leak protected data to remote provider contrary to policy.

## Validation

```bash
make smoke-models PROVIDER=llamacpp
make test-integration TEST=model-provider-parity
make load MODEL_PROFILE=local
```

## Progress

- [ ] Hardware decisions completed.
- [x] Plan refined for local MVP scaffold.
- [x] Docker profile scaffold implemented.
- [x] Mock provider contract reviewed.

## MVP status

- Added optional `compose.llamacpp.yaml` profile and `make smoke-models PROVIDER=llamacpp` command surface.
- Default MVP remains mock/OpenRouter-compatible and does not require GPU or GGUF files.

## Remaining production work

- Real llama.cpp execution requires local model artifacts, licenses, checksums, GPU/RAM decision and provider parity/load tests.
