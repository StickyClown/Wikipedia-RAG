# Security checklist

## Repository and secrets

- `.env`, API keys, cookies, tokens, model credentials and production certificates are ignored.
- Example configs contain placeholders only.
- Logs and traces redact authorization headers and secrets.

## Multi-tenancy

- Authorization tests cover every endpoint and search mode.
- Tenant filters are constructed inside trusted code.
- Retrieval debugger and exported traces cannot expose other tenants.
- Cache keys include tenant, access scope and index version.

## Untrusted uploads

- MIME is detected by content.
- File, uncompressed-size, archive-entry and nesting limits exist.
- Filenames are normalized; path traversal is rejected.
- Parsers run without Docker socket, host mounts or outbound network by default.
- CPU, memory and wall-time limits exist.
- Embedded scripts/macros are not executed.
- HTML is sanitized before preview.
- Optional malware scan decision is documented.

## Infrastructure

- PostgreSQL, OpenSearch, MinIO, Redis and model servers are not publicly exposed in production.
- Internal credentials differ from defaults.
- TLS and authentication are enabled for non-local deployment.
- Backups are encrypted and restore access is restricted.

## Supply chain

- Lockfiles are committed.
- Production Docker images are pinned.
- Dependency vulnerability scanning runs in CI.
- Licenses of model and parser artifacts are recorded.

## Agent/runtime safety

- Tool allowlist is explicit.
- External writes are absent from Extended Search unless separately approved.
- Step and cost budgets are enforced server-side.
- Prompt/document instructions cannot override tenant/security policy.
