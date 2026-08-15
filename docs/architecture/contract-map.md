# Canonical Contract Map

Code and tests are authoritative; this map points to the owner and executable
boundary for high-value contracts. Use the trace:

`code → contract → owner → boundary → executable test`

If a change cannot identify each element, ownership or enforcement is unclear.

## Contracts

| ID | Contract and owner | Boundary/invariant | Executable evidence |
| --- | --- | --- | --- |
| AUTH-001 | Server-owned `ActorContext`; auth service | Session produces actor; request fields cannot create identity or tenant authority | `tests/unit/test_auth_service.py` |
| AUTH-002 | Tenant/KB role policy; auth + repository | Active tenant and effective KB role are checked before scoped operations | `tests/unit/test_auth_policy.py` |
| AUTH-003 | Document ACL/current state; PostgreSQL read owner | Search/cache candidates are confirmed for current publication and ACL before exposure | `tests/unit/test_retrieval_current_state.py`, functional retrieval path |
| AUTH-004 | Public identifier authorization; API handlers | Every supplied KB/document/group/filter/prefix ID is re-authorized; denied writes create no work | `tests/unit/test_api_authorization_inventory.py` |
| ING-001 | Upload identity/object location; API | Tenant/KB/object prefix is server-generated after authorization | upload and cross-tenant hardening tests |
| ING-002 | Validation/normalization; ingestion worker | Bytes are validated and normalized before chunks or embeddings | `tests/unit/test_document_ingestion.py` |
| ING-003 | Publication; PostgreSQL lifecycle owner | Only chunks of the active published version are retrievable; failed/cancelled work is not published | retrieval-current-state and search-projection tests |
| MODEL-001 | Operation DTOs; `ModelClient`/Gateway | Chat, embedding, rerank and token counting remain distinct typed contracts | `tests/unit/test_model_control.py` |
| MODEL-002 | Endpoint adaptation; Gateway drivers | Provider-specific knowledge terminates at Gateway/admin discovery, not runtime business modules | `tests/unit/test_architecture_boundaries.py` |
| MODEL-003 | Active revision identity; Gateway control plane | Alias call uses the immutable active snapshot and returns safe effective identity; endpoint placement is irrelevant | Gateway unit and model-runtime functional tests |
| DR-001 | Research scope/lifecycle; research repository | Same-tenant KB scope is persisted once and reused by every episode/tool | Deep Research unit and persistence tests |
| DR-002 | Research concurrency; research repository | Only a valid lease holder advances stage; transitions use compare-and-set | Deep Research lease/CAS tests |
| DR-003 | Research visibility; projection builder | Durable evidence is rechecked for current publication/ACL before context or public report | Deep Research visibility and revocation tests |
| UI-001 | Browser/API protocol; UI API client | Cookie/CSRF/SSE contracts and typed safe errors are preserved; UI guards are not authorization | protocol Vitest and selected Playwright paths |

## Boundary Rules

### Authority

`auth_service → ActorContext → handler policy → repository/query scope`

No client DTO, UI control or search filter may skip this chain.

### Ingestion

`authorized upload → durable job → validation/normalization → staged chunks → search projection → DB publication`

Publication and projection repair remain idempotent and observable.

### Retrieval

`server scope → derived candidates → DB current-state/ACL confirmation → evidence/citation`

Citation labels and cached cursors are projections, not durable identity or
authority.

### Models

`business operation → ModelClient alias → Gateway operation → endpoint adapter`

Any healthy compatible endpoint is valid. Provider SDKs, credentials and
payload details stay outside business modules.

### Research

`authorized durable scope → lease/CAS episode → allowlisted tool → evidence/claim → ACL-trimmed projection`

Planner output and document text cannot create scope or executable authority.

## Change Checklist

For a changed contract:

1. name the owner and public/internal boundary;
2. update the typed schema and compatibility behavior if applicable;
3. cover success plus important denial/failure/concurrency paths;
4. run the narrow end-to-end path that crosses the changed boundary;
5. update this map only when ownership or the invariant changes.

## Current Operational Gap

Two historical search-projection reconciliation records are repeatedly degraded
without reaching a terminal result. This is an implementation/operations gap in
the ING-003/AUTH-003 repair path, not permission to bypass DB confirmation.
Track current resolution in [STATUS.md](../STATUS.md).
