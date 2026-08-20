# Data and Storage

## Ownership Matrix

| Store | Authoritative data | Rebuildable |
| --- | --- | --- |
| PostgreSQL | Identity, workspace grants, KB/source/document lifecycle, jobs, chunks, publication, query events, active model revisions and Deep Research state | No |
| S3-compatible object storage | Original uploads, normalized documents and parser artifacts, scoped by KB/document identity | Originals: no; derived artifacts: conditionally |
| OpenSearch | KB-scoped BM25 and vector documents | Yes |
| Redis/Valkey | Search windows and facet snapshots with bounded TTL | Yes |
| ZIM/Kiwix | Operator-managed Wikipedia source snapshot | Imported projections are rebuildable only from the preserved snapshot |
| `artifacts/` | Eval and validation evidence | Reruns create new evidence; important reports should be retained separately |
| Browser memory | Selected files, current screen and transient progress | Yes |

PostgreSQL plus object storage is the backup/restore boundary. Database metadata
and objects must be restored consistently. OpenSearch and Redis are derived.

## Document Lifecycle

1. API authorizes the actor and creates an upload session with a server-owned
   KB/document object prefix.
2. The browser uploads bytes to a presigned object URL.
3. Completion records the document/version and queues durable work.
4. A worker validates, parses, normalizes, chunks and embeds the document.
5. Chunks are staged, projected to search and published only after validation.
6. PostgreSQL remains the current-state authority. Retrieval rechecks active
   version, publication and ACL before exposing candidates.
7. Failed or cancelled work never publishes searchable chunks.
8. Delete hides content from retrieval first; bounded deferred purge removes
   objects and derived records.

## Identity and Provenance

- Source identity is server-scoped by KB, namespace and external ID.
- Content/source-version changes create a new document version; an unchanged
  version is idempotent.
- Object keys, checksums, publication state and ACL fields are server-owned.
- Evidence projections retain document/chunk/source/index contract identity so
  citations can be resolved and re-authorized.
- Deep Research stores durable evidence and claims separately from model-facing
  and public projections.

## Derived Search Safety

OpenSearch and Redis may be stale. Candidates from either layer are batch
confirmed against PostgreSQL for current version, published state and current
document access. Staleness may cause a safe false absence, never broader access.

Reconciliation rows make projection drift durable and observable. Repair is
idempotent and lease-fenced; non-terminal degraded records are operational
blockers when a controlled baseline requires a clean projection.

## Sensitive Data

Normal logs, public APIs and validation reports exclude:

- passwords, session/CSRF tokens, signing keys and provider credentials;
- raw prompts, provider request/response bodies and parser stderr;
- raw private document contents and model tool queries;
- object-storage keys and database URLs.

Public identifiers are returned only after current workspace KB/document
authorization. Retention and purge preserve lifecycle auditability without
keeping derived search exposure.
