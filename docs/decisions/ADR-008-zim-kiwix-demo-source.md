# ADR-008 — ZIM/Kiwix demo source

Status: accepted for ExecPlan 10

## Context

ADR-007 selected Wikimedia XML multistream because that was the only local Wikipedia asset available at the time. The new demo MVP requirement and `docs/production_rag_architecture.md` require a real Russian Wikipedia ZIM served by Kiwix and ingested through libzim.

## Decision

Use ZIM/libzim as the default Wikipedia source for the completed local demo MVP. Keep the existing Wikimedia XML adapter and commands as a supported regression/development path.

## Consequences

- Docker Compose runs Kiwix over `./zim` and the worker reads the same directory read-only.
- ZIM documents preserve archive id, filename, Kiwix book identifier, exact entry path, redirect target and Kiwix source URL. By default the identifier is the ZIM filename stem, matching `kiwix-serve`; an operator may override it with `KIWIX_BOOK_NAME`.
- Kiwix source URLs are built from the exact libzim entry path, never from title reconstruction.
- Redirects are retained as alias/provenance but do not count toward the small import limit.
- XML-specific ADR-007 remains historical context for the existing XML adapter, but no longer defines the default demo source.
