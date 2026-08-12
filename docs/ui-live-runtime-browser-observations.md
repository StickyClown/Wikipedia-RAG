# Live UI browser observations

Date: 2026-08-10
Target: `http://localhost:5173` (already running Compose)
Method: interactive inspection in the authenticated in-app browser session.

## Safety boundary

This pass did not delete any knowledge base, document, run, source, model
configuration, volume or index. It also did not start Wikipedia import,
document ingestion, source synchronization, a new chat query, a Deep Research
run, or a model validation/activation. Those actions either create durable
data, contact a model/provider, or could change the active retrieval index.

## Confirmed visible behaviour

| Area | Action | Observed result |
| --- | --- | --- |
| Public shell | Open UI | Initial page briefly displayed Russian sign-in shell and readiness `проверка`; the existing browser session then restored as `admin`. |
| Locale | Click `EN` | UI switched to English; product tagline became `Local Russian Wikipedia RAG MVP`, status became `ready`, and `EN` was pressed. |
| Locale | Click `RU` | UI switched back to Russian; `Вход` heading and pressed `RU` state were observed before the authenticated session restored. |
| Workspace | Click Chat/Search/Research/Knowledge Base/Models tabs | Each target tab set `aria-selected=true` and rendered its corresponding panel. |
| Research | Click an existing completed run | Opened a completed `Silver Quarry` run and displayed its detail/coverage surface without starting a new run. |
| Knowledge Base | Click existing source `Health` | The panel returned a visible safe error: `ZIM_NOT_FOUND` / `ZIM file is not available`. |
| Models | Click `Refresh` | Model control data refreshed and the Models panel remained visible. |

## Current visible runtime state

- API readiness was displayed as `ready`.
- The authenticated user displayed as `admin`.
- The Chat scope contains one selected KB (`Russian Wikipedia`) and a large
  list of historical upload, smoke, research, and benchmark KBs.
- Chat: `Ask` is enabled; `Debug` is disabled until a query run exists.
- Search: `Search` is disabled with an empty query.
- Research: `Quick run`, `Create plan`, and `Refresh` are enabled; historical
  run list contains completed, failed and cancelled runs.
- Knowledge Base: existing Kiwix source is `active`, but its health output says
  no ZIM file is available.
- Models initially showed `Validate draft` enabled; `Activate` was disabled.
  In the later follow-up state `Validate draft` was also disabled. Both
  `bootstrap-mock` and `bootstrap-openrouter` show `credentials missing ·
  enabled`; `Active revision: not configured` is visible.

## Controls intentionally not invoked

| Controls | Reason |
| --- | --- |
| `Logout`, Local login, OIDC | Preserve the existing browser session; no credentials were supplied for a new authentication flow. |
| Chat `Ask`, Chat `Reload app` | Ask may call the active provider and create a query run; reload would discard the current browser UI state. |
| Search submit | Disabled until a query is entered; no query was submitted to avoid creating additional runtime activity. |
| Research `Quick run`, `Create plan`, Pause/Resume/Cancel, exports | Create or mutate durable Deep Research runs and may call the active provider. |
| KB `Create`, `Start import`, file upload | Create persistent data or initiate ingestion/index publication; explicitly outside this pass. |
| Source `Add source`, `Sync`, `Full sync`, `Disable` | Create or modify source configuration/content and may reconcile indexed documents. |
| Models `Validate draft`, `Activate`, Add connection/model, Test connection | Change or validate model-control state and may contact providers. |

## Follow-up candidates

1. Mount or configure the expected ZIM before testing the import path; current
   source health is explicitly blocked by `ZIM_NOT_FOUND`.
2. Configure credentials and activate a validated model revision before
   provider-backed Chat or Deep Research UI checks.
3. Use isolated test KBs and a controlled model profile for mutating E2E
   workflows; do not use the historical shared KB inventory above.

## Follow-up pass: controls requested by the user

The browser session was re-authenticated using the documented local
development credentials from the project configuration. The password is not
recorded here.

| Control/action | Result |
| --- | --- |
| Chat `Ask` | Started retrieval and displayed live `RETRIEVAL`/`answer_generation` status. It remained in generation with a large remaining deadline; no `Answer` was observed during the browser interaction window. |
| Chat `Debug` | Became enabled during the run; click opened `Debug · Retrieval` with a visible `Timeline` and retrieval stages. |
| Chat `Stop` | Click produced the visible alert `Answer generation stopped.` |
| Search submit | Query `Россия` returned visible grouped Wikipedia results with scores and `Open in viewer` buttons. |
| Search `Open in viewer` | Click produced no visible viewer or alert in the observed state; this is recorded as an unresolved UI outcome. |
| Research `Create plan` | Click left the topic in the form and surfaced `Failed to fetch`; no plan appeared in the plan list. |
| KB `Create` | Created a persistent KB named `UI Browser Action Probe 2026-08-10T19:24:28.238Z`; it was not deleted. No index import or publication was started. |
| External sources `Add source` | Opened the source form with Connector, Name, Refresh seconds, Advanced configuration and Permissions fields. The form was not submitted. |
| Models `Test connection` (both existing connections) | Both clicks completed without a visible alert or status change; the cards still showed `credentials missing · enabled`. |
| Models `Validate draft` / `Activate` | `Validate draft` was disabled at the final observation; `Activate` remained disabled. Neither was invoked. |
| Models `Add connection` / `Add model` | Blank submissions produced no visible state change; existing forms remained visible. No model or connection was added. |
| Chat `Reload app` | Reload restored the workspace and kept Logout visible, confirming session persistence. |
| Logout → Local | Logout returned to the auth panel; Local login restored workspace tabs successfully. |

The following remain intentionally blocked by the index-safety constraint:
`Start import`, source `Sync`, source `Full sync`, file upload, and any action
that could publish or reconcile indexed content. OIDC was not started because
it redirects to an external identity flow and no OIDC test account was
provided.

## Root causes and implemented correction (2026-08-10)

The follow-up source review correlated the browser/API observations with the
runtime traces. The failed Research plan was not a browser transport defect:
the UI sent `retrieval_profile: "auto"` as though it were a configured
profile, and server lookup raised `KeyError`. Chat retrieval completed, but
the Model Gateway returned a retryable 502 during generation; stopping or
disconnecting the SSE stream could leave the child task unjoined and the
`query_runs` row in `running`. At review time there were 27 such stale normal
or extended chat rows. A Russian-Wikipedia plus upload-KB scope had no common
retrieval profile and `/api/v1/retrieval-profiles` incorrectly surfaced this
as a 500. The viewer APIs returned 200; the component was rendered below the
results without viewport scrolling or focus. Model connection tests persisted
safe status but the UI discarded it.

The implemented source correction normalizes `auto` on both sides of the
contract, resolves and persists a real profile across the complete Research
scope, includes primary KB in that scope, and returns safe 409/422 errors.
The catalog now returns `resolved_default: null` and
`scope_error_code=RETRIEVAL_PROFILE_INCOMPATIBLE` when no shared profile
exists; UI actions are blocked until the scope changes. Chat cancellation now
cancels and joins the active task, persists one terminal status through a
shielded finalizer, and startup recovers only stale normal/extended chat runs
as `STALE_QUERY_RUN_RECOVERED` without deleting data. Gateway errors carry
safe retry metadata and runtime alias failure is reflected in readiness during
its cooldown. The Viewer scrolls/focuses its heading and has pre-structure
loading/error feedback; model-test cards show safe result/time.

Focused source validation passed (39 backend tests; 3 UI protocol tests; UI
lint, typecheck and production build). No provider-backed live completion is
claimed by this document after the source change. `ZIM_NOT_FOUND` remains a
deployment prerequisite: mount/configure the expected ZIM and active index;
do not substitute a file or index merely to make the UI green.

### Rebuilt-runtime follow-up

The affected Compose services were rebuilt without deleting volumes; API
`/ready` returned `ok`. The isolated `upload_mock` Playwright run uncovered
three test-runtime issues rather than a regression in Ask/Research: a hidden
native-select option was asserted as visible, a broad `published` text locator
was ambiguous, and teardown fetched the API cross-origin rather than through
the UI proxy. The helper now waits for one option, uses an exact publication
status, and tears down through the same-origin proxy. The full mock E2E and
provider-backed smoke are therefore intentionally still not reported as
passed. The failed fixture cleanup also exposed an empty-KB FK ordering issue;
the supported deletion route now removes its grants before the KB row, with a
unit test.

### Final mock acceptance follow-up

The E2E helper now collapses the retrieval-scope control after selecting its one fixture KB, preventing a long KB list from overlaying Ask. Under a temporary `upload_mock` runtime, the focused Playwright scenario passed in 13.8 seconds: Ask rendered an answer, cited source with the fixture marker, and the Debug timeline. Its test-owned KB was removed through the owner endpoint. The endpoint performs bounded upload-only cleanup, including normal Chat child records; Research history remains a safe 409. Legacy MinIO `MissingContentMD5` falls back to per-object deletion.

The complete Research/upload E2E matrix remains unrun after this final change. Provider-backed Russian-Wikipedia Ask was then verified on 2026-08-11: the first repeat exposed `MODEL_OUTPUT_TRUNCATED` from the default 768-token answer cap; after raising the production budget, the same question reached `run.completed` with answer text and citations. Gateway structured requests now disable implicit OpenRouter reasoning, and nested FastAPI Gateway errors are unwrapped into their safe `code`/`retryable` fields. Parser and Gateway diagnostics log only safe reason text/path; provider bodies, prompts and document contents remain excluded. A bounded provider-backed Deep Research smoke also reached `completed` with evidence and a final report.

### Generation budget hardening (2026-08-11)

The production budget policy is now explicit across Ask and Deep Research:
Ask `4096`, verifier `4096`, planner/synthesis `16000`. `16000` is intentional:
with an `80000` context window and reserves `0.70 + 0.20 + 0.10`, `16384` would
not fit the declared envelope. Gateway clamps both normal and streaming calls
to the available envelope and returns safe budget metadata. Structured
truncation gets one bounded retry up to the stage cap; invalid schema and auth
rejections are non-retryable. A rebuilt provider Ask again reached
`run.completed`; a final repeat after tightening the insufficient-evidence
prompt also reached `run.completed`. The bounded provider-backed Deep Research smoke also reached
`completed` with evidence, claims and a final report. The full Research/upload
 E2E matrix remains pending.

### In-app Browser Ask/Research confirmation (2026-08-11)

The UI container was rebuilt (`docker compose up -d --build ui`, exit 0) and
the API `/ready` endpoint returned `status=ok`. In a fresh authenticated
in-app-browser tab, exactly one retrieval KB (`Russian Wikipedia`) was selected.

- Ask `Что такое Россия?` produced a real `POST /api/v1/chat` with a 200 SSE
  response. The visible UI moved through `Loading…`/retrieval, then left the
  busy state, rendered an `Answer` with citations `[S1]`–`[S12]`, and enabled
  `Debug`. The Debug view showed the retrieval timeline and `query_complete`;
  the observed query-run id ended in `5e698f31-09c0-0346-b23f-8a943b6646ea`.
- Deep Research `Кратко: что такое Россия и какова её столица?` created a new
  run (list suffix `826e7e8a`) and visibly progressed through `received`,
  `retrieving`, `evaluating`, and `synthesize` to `completed · quality_gate`.
  The detail view showed 15 evidence-memory items, 9 episodes, and the report
  terminal marker `all_questions_processed`.

Both workflows therefore execute end-to-end in the rebuilt browser UI. This
run also exposed a separate retrieval-quality issue: Russian-Wikipedia search
returned unrelated asteroid/domain/short-article fragments, so the Research
quality gate remained `partial` with no confirmed findings. That is not a
transport or terminalization failure, but it remains a product-quality blocker
for claiming a semantically correct Russian-Wikipedia answer.
