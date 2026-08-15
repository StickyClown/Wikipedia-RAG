# Security and Tenancy

## Authority

- `ActorContext` is built from a server-side session; request fields never
  create tenant, role or group authority.
- Every tenant/KB/document operation re-authorizes supplied identifiers at its
  public boundary.
- Platform, tenant and KB roles are evaluated server-side. Document ACL is an
  additional restriction, not a replacement for KB access.
- UI visibility and disabled controls are usability features only; backend
  authorization is mandatory.

## Sessions and Requests

- Session cookies are opaque, HttpOnly and server validated.
- Session and CSRF tokens are stored hashed; state-changing cookie requests
  require CSRF.
- OIDC uses authorization-code/PKCE state stored server-side.
- Development auth bypasses and default credentials are never production
  authorization mechanisms.

## Document and Retrieval Access

- Upload object prefixes are generated after actor and KB authorization.
- Client filters for KB, document, group, source or object location are
  validated and authorized before retrieval or mutation.
- Search engines and caches only generate candidates. PostgreSQL confirms
  current publication and document access before exposure.
- ACL changes create durable projection work; stale projection may hide data but
  cannot grant broader access.
- Presigned uploads authorize a bounded object write; they do not expose storage
  credentials.

## Research Access

- A research run stores a same-tenant, server-authorized KB scope.
- Durable evidence does not imply permanent visibility. Context, report,
  citations and mixed-evidence claims are rebuilt from evidence visible to the
  current actor.
- Planner/tool inputs cannot select arbitrary tenants, KBs or object keys.
- Retrieved document instructions are treated as untrusted evidence text.

## Service Boundaries

- Business code calls models through Model Gateway; provider credentials and
  payloads terminate at the Gateway/driver boundary.
- Parser services receive bounded bytes/text only. They receive no tenant
  authority, storage credentials, arbitrary object URLs or model secrets.
- PostgreSQL and object storage are authoritative. Search and cache compromise
  must not bypass current-state confirmation.

## Redaction

Normal logs, public responses and validation reports exclude credentials,
tokens, raw prompts, provider bodies, private document contents, parser stderr,
object keys and database URLs. Safe diagnostics may expose request/trace ID,
component, alias, operation, revision, bounded counts and normalized error code.

## Required Verification

Changes to an authorization boundary cover allowed, insufficient-role and
cross-tenant paths and assert denied writes create no jobs or mutations.
Retrieval/research changes cover stale projection and post-persistence ACL
revocation where applicable.
