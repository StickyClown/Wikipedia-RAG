# ADR-005 — Model Gateway boundary

Status: accepted

## Decision

All chat, embeddings and reranking calls go through an internal OpenAI-compatible Model Gateway using logical aliases. OpenRouter is the first provider; three separate llama.cpp servers are the local target.

## Consequences

- business code never imports OpenRouter-specific SDKs;
- provider errors are normalized;
- aliases publish only after capability smoke tests;
- retries, budgets, telemetry and fallback policy live in the gateway;
- `/v1/rerank` is an internal extension contract.
