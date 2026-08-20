from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, cast

import httpx
from fastapi import File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from wikipediarag.answerability import should_try_extended_search
from wikipediarag.answering import generate_answer
from wikipediarag.auth import (
    ActorContext,
    AuthenticationMethod,
    AuthorizationError,
    GroupType,
    PlatformRole,
    require_active_tenant,
)
from wikipediarag.auth_service import (
    AuthenticationError,
    auth_disabled_actor,
    authenticate_local_user,
    change_local_password,
    create_session,
    csrf_token_matches,
    ensure_bootstrap_admin,
    load_actor_for_session,
    load_bootstrap_admin_user,
    load_session_user,
    local_login_enabled,
    revoke_session,
    rotate_csrf_token,
    test_actor_context,
)
from wikipediarag.config import Settings, get_settings
from wikipediarag.db import connect, connect_autocommit, ensure_schema
from wikipediarag.deep_research import (
    build_public_research_report,
    build_research_questions,
    context_policy_for_profile,
    visible_research_evidence,
)
from wikipediarag.diagnostics import (
    build_answer_artifact,
    build_failure_artifact,
    build_search_plan,
    initial_route_decision,
    repair_route_decision,
)
from wikipediarag.document_ingestion import UploadValidationError, safe_upload_filename, sha256_hex
from wikipediarag.extended import run_extended_search, should_start_extended
from wikipediarag.ids import stable_hash
from wikipediarag.import_paths import ImportFileNameError, configured_or_requested_filename, resolve_import_filename
from wikipediarag.observability import content_policy, safe_error_code, safe_telemetry_payload
from wikipediarag.oidc_service import complete_oidc_callback, encrypt_server_tokens, oidc_login_enabled, start_oidc_flow
from wikipediarag.provenance import (
    direct_upload_source_id,
    public_provenance_from_metadata,
    source_document_id,
    source_document_version_id,
)
from wikipediarag.reliability import (
    OperationDeadline,
    OperationDeadlineExceeded,
    is_retryable_exception,
    safe_failure_from_exception,
)
from wikipediarag.repository import (
    DocumentVersionLifecycleError,
    approve_research_plan,
    cancel_query_run,
    claim_idempotency_record,
    complete_idempotency_record,
    complete_query_run,
    create_document_deletion_job,
    create_document_upload_records,
    create_ingestion_job,
    create_knowledge_source,
    create_query_run,
    create_reprocess_job,
    create_research_plan,
    create_research_resume_job,
    create_research_run,
    create_source_sync_job,
    create_upload_batch,
    create_upload_session,
    fail_query_run,
    fetch_document_context_chunks,
    get_knowledge_base,
    get_knowledge_source,
    get_research_plan,
    get_research_run,
    get_source_sync_run_public,
    get_upload_batch_status,
    get_upload_session,
    insert_audit_event,
    insert_retrieval_event,
    list_document_sections,
    list_document_versions_public,
    list_knowledge_sources_public,
    list_research_plans,
    list_research_runs,
    list_upload_batch_sessions_private,
    load_index_version_by_read_alias,
    load_research_detail_records,
    load_research_run_scopes,
    load_retrieval_events,
    recover_stale_chat_query_runs,
    request_cancel,
    request_research_cancel,
    request_research_pause,
    request_resume,
    search_document_chunks,
    search_projection_health,
    soft_delete_document,
    update_knowledge_source,
    update_query_run_usage,
    update_research_plan,
)
from wikipediarag.research_tool_registry import DEFAULT_RESEARCH_TOOL_MODE
from wikipediarag.retrieval import retrieve, retrieve_multi
from wikipediarag.retrieval_contract import (
    KnowledgeBaseNotReady,
    RetrievalProfileIncompatible,
    validate_active_retrieval_contract,
)
from wikipediarag.retrieval_profile import get_profile_catalog, get_retrieval_profile
from wikipediarag.retrieval_profile_resolver import normalize_retrieval_profile_request
from wikipediarag.retrieval_profile_resolver import resolve_retrieval_profile as _resolve_retrieval_profile
from wikipediarag.schemas import (
    AccessGrantListResponse,
    AccessGrantReplaceRequest,
    AccessGrantResponse,
    AccessGroupResponse,
    AuthOidcStartResponse,
    AuthSessionResponse,
    AuthUserResponse,
    ChatRequest,
    DebugSearchRequest,
    DocumentContextChunk,
    DocumentContextResponse,
    DocumentDeleteResponse,
    DocumentReprocessResponse,
    DocumentSearchRequest,
    DocumentSearchResponse,
    DocumentSearchResult,
    DocumentSection,
    DocumentStructureResponse,
    GroupCreate,
    GroupPatch,
    ImportRequest,
    KnowledgeBaseCreate,
    KnowledgeBasePatch,
    LocalLoginRequest,
    LocalPasswordChangeRequest,
    QueryRunEvaluationRequest,
    QueryRunFeedbackRequest,
    ResearchPlanActionResponse,
    ResearchPlanCreate,
    ResearchPlanDetail,
    ResearchPlanListResponse,
    ResearchPlanPatch,
    ResearchPlanQuestion,
    ResearchPlanStatus,
    ResearchRunActionResponse,
    ResearchRunCreate,
    ResearchRunDetail,
    ResearchRunListResponse,
    ResearchRunStatus,
    RetrievalProfileCatalogResponse,
    RetrievalProfileOption,
    RetrievalResult,
    SearchRequest,
    SearchResponse,
    SourceCreate,
    SourceHealthResponse,
    SourcePatch,
    SourceProvenance,
    SourceReferenceInput,
    SourceResponse,
    SourceSyncRequest,
    SourceSyncResponse,
    SourceSyncRunResponse,
    SseEvent,
    UploadBatchAccepted,
    UploadBatchCreate,
    UploadBatchItemAccepted,
    UploadBatchStatus,
    UploadCompleteResponse,
    UploadSessionAccepted,
    UploadSessionComplete,
    UploadSessionCreate,
    UserCreate,
    UserPatch,
    ZimImportRequest,
)
from wikipediarag.search_index import READ_ALIAS, delete_document_chunks
from wikipediarag.search_service import run_public_search
from wikipediarag.source_connectors import ConnectorError, connector_for_kind
from wikipediarag.storage import create_presigned_put_url, delete_objects, head_object, put_bytes
from wikipediarag.workspace_access import (
    AccessGrant,
    PrincipalType,
    ResourcePermission,
    ResourceType,
)
from wikipediarag.workspace_access import (
    PlatformRole as WorkspacePlatformRole,
)
from wikipediarag.workspace_grants import InvalidGrantError, WorkspaceGrantRepository
from wikipediarag.workspace_sql import text


async def resolve_retrieval_profile(conn: Any, **kwargs: Any) -> Any:
    """Resolve against handler-bound repository functions.

    Keeping the callbacks here makes the resolver auditable and allows API
    tests to substitute the repository boundary without bypassing contract
    validation in production.
    """
    return await _resolve_retrieval_profile(
        conn,
        get_knowledge_base_fn=get_knowledge_base,
        load_index_fn=load_index_version_by_read_alias,
        **kwargs,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Normalize explicit HTTPException details into the public safe error envelope."""
    if isinstance(exc.detail, dict) and isinstance(exc.detail.get("error"), dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(
            code=_http_error_code(exc.status_code),
            message=str(exc.detail),
            request_id=_request_id(request),
        ),
    )


async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return sanitized validation errors without echoing unsafe request payloads."""
    return JSONResponse(
        status_code=422,
        content=_error_payload(
            code="REQUEST_VALIDATION_FAILED",
            message="request validation failed",
            request_id=_request_id(request),
            details={"errors": _safe_validation_errors(exc.errors())},
        ),
    )


async def authentication_exception_handler(request: Request, exc: AuthenticationError) -> JSONResponse:
    """Map authentication failures to the public auth error contract."""
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(
            code=exc.code,
            message=exc.message,
            request_id=_request_id(request),
        ),
    )


async def authorization_exception_handler(request: Request, exc: AuthorizationError) -> JSONResponse:
    """Map authorization failures to the public authz error contract."""
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(
            code=exc.code,
            message=exc.message,
            request_id=_request_id(request),
        ),
    )


async def startup() -> None:
    """Initialize database schema and local bootstrap admin state on API startup."""
    await ensure_schema()
    settings = get_settings()
    async with connect() as conn:
        await ensure_bootstrap_admin(conn, settings)
        await recover_stale_chat_query_runs(
            conn,
            max_age_seconds=max(
                1,
                int(settings.chat_run_deadline_seconds + settings.operation_heartbeat_seconds * 2),
            ),
        )


async def health() -> dict[str, str]:
    """Report process liveness without checking downstream dependencies."""
    return {"status": "ok"}


async def ready() -> dict[str, Any]:
    """Report dependency readiness without exposing dependency internals."""
    settings = get_settings()
    components: dict[str, str] = {}
    projection_details: dict[str, Any] = {
        "pending": 0,
        "oldest_age_seconds": 0,
        "last_error_code": None,
        "reconciliation_pending": 0,
        "reconciliation_degraded": 0,
        "reconciliation_oldest_age_seconds": 0,
        "reconciliation_error_code": None,
    }
    try:
        async with connect() as conn:
            await conn.execute(text("SELECT 1"))
            worker = await conn.execute(
                text(
                    """
                    SELECT EXISTS(
                      SELECT 1 FROM worker_instances
                      WHERE lane = 'deep_research'
                        AND last_heartbeat_at >= now() - make_interval(secs => :max_age_seconds)
                    ) AND EXISTS(
                      SELECT 1 FROM worker_instances
                      WHERE lane LIKE '%document_upload%'
                        AND last_heartbeat_at >= now() - make_interval(secs => :max_age_seconds)
                    )
                    """
                ),
                {"max_age_seconds": max(30, settings.worker_job_heartbeat_seconds * 2)},
            )
            projection = await search_projection_health(conn)
            projection_details = {
                "pending": int(projection.get("pending") or 0),
                "oldest_age_seconds": int(projection.get("oldest_age_seconds") or 0),
                "last_error_code": projection.get("last_error_code"),
                "reconciliation_pending": int(projection.get("reconciliation_pending") or 0),
                "reconciliation_degraded": int(projection.get("reconciliation_degraded") or 0),
                "reconciliation_oldest_age_seconds": int(projection.get("reconciliation_oldest_age_seconds") or 0),
                "reconciliation_error_code": projection.get("reconciliation_error_code"),
            }
        components["postgres"] = "ok"
        components["worker"] = "ok" if bool(worker.scalar()) else "stale"
        components["search_projection"] = (
            "degraded"
            if (
                projection.get("last_error_code")
                or int(projection.get("oldest_age_seconds") or 0) > settings.search_projection_ready_max_age_seconds
            )
            else "ok"
        )
    except Exception:
        components["postgres"] = "failed"
        components["worker"] = "failed"
        components["search_projection"] = "failed"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{settings.model_gateway_url.rstrip('/')}/ready")
            gateway_ready = response.status_code == 200 and response.json().get("status") == "ok"
            components["model_gateway"] = "ok" if gateway_ready else "failed"
    except Exception:
        components["model_gateway"] = "failed"
    checks: dict[str, str] = {
        "opensearch": f"{settings.opensearch_url.rstrip('/')}/",
        "minio": f"{settings.minio_endpoint.rstrip('/')}/minio/health/live",
    }
    if settings.document_parser_services_required:
        checks["xberg"] = f"{settings.xberg_url.rstrip('/')}/health"
        checks["docling"] = f"{settings.docling_url.rstrip('/')}/health"
        checks["metadata_service"] = f"{settings.metadata_service_url.rstrip('/')}/health"

    async def check_http(component: str, url: str) -> tuple[str, str]:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(url)
            return component, "ok" if response.status_code < 500 else "failed"
        except Exception:
            return component, "failed"

    for component, value in await asyncio.gather(*(check_http(name, url) for name, url in checks.items())):
        components[component] = value
    status = "ok" if all(value == "ok" for value in components.values()) else "degraded"
    return {"status": status, "components": components, "search_projection": projection_details}


async def local_login(
    payload: LocalLoginRequest,
    request: Request,
    response: Response,
) -> AuthSessionResponse:
    """Authenticate a local user and create an opaque application session cookie."""
    settings = get_settings()
    if not local_login_enabled(settings):
        raise AuthenticationError("LOCAL_LOGIN_DISABLED", "local login is disabled", status_code=403)
    async with connect() as conn:
        user = await authenticate_local_user(conn, username=payload.username, password=payload.password)
        created = await create_session(
            conn,
            user_id=user.user_id,
            authentication_method=AuthenticationMethod.local,
            settings=settings,
            remember_me=payload.remember_me,
        )
        await _audit(
            conn,
            request=request,
            actor=None,
            action="auth.local_login",
            target_type="user",
            target_id=user.user_id,
            outcome="success",
        )
    max_age = settings.remember_me_max_seconds if payload.remember_me else settings.session_max_seconds
    _set_session_cookie(response, created.session_token, settings, max_age=max_age)
    return AuthSessionResponse(
        authenticated=True,
        user=_auth_user_response(user),
        authentication_method=AuthenticationMethod.local.value,
        session_id=created.session_id,
        csrf_token=None,
        expires_at=created.expires_at,
    )


async def oidc_start() -> AuthOidcStartResponse:
    """Start the OIDC Authorization Code with PKCE login flow."""
    settings = get_settings()
    if not oidc_login_enabled(settings):
        raise AuthenticationError("OIDC_LOGIN_DISABLED", "OIDC login is disabled", status_code=403)
    async with connect() as conn:
        started = await start_oidc_flow(conn, settings=settings)
    return AuthOidcStartResponse(authorization_url=started.authorization_url, expires_at=started.expires_at)


async def oidc_callback(
    code: str,
    state: str,
    request: Request,
    response: Response,
) -> AuthSessionResponse:
    """Complete OIDC login and create the application session cookie."""
    settings = get_settings()
    if not oidc_login_enabled(settings):
        raise AuthenticationError("OIDC_LOGIN_DISABLED", "OIDC login is disabled", status_code=403)
    async with connect() as conn:
        login = await complete_oidc_callback(conn, settings=settings, code=code, state=state)
        await _audit(
            conn,
            request=request,
            actor=None,
            action="auth.oidc_login",
            target_type="user",
            target_id=login.user_id,
            outcome="success",
        )
    _set_session_cookie(response, login.session_token, settings, max_age=settings.session_max_seconds)
    return AuthSessionResponse(
        authenticated=True,
        user=AuthUserResponse(
            id=login.user_id,
            username=login.username,
            display_name=login.display_name,
            platform_role=login.platform_role.value,
            password_change_required=login.password_change_required,
        ),
        authentication_method=AuthenticationMethod.oidc.value,
        session_id=login.session_id,
        csrf_token=None,
        expires_at=login.expires_at,
    )


async def change_password(
    payload: LocalPasswordChangeRequest,
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict[str, str]:
    """Change the current user local password after CSRF validation."""
    actor = await _require_actor(request)
    await _require_csrf(actor, x_csrf_token)
    async with connect() as conn:
        await change_local_password(
            conn,
            user_id=actor.user_id,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="auth.local_password_changed",
            target_type="user",
            target_id=actor.user_id,
            outcome="success",
        )
    return {"status": "password_changed"}


async def logout(
    request: Request,
    response: Response,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict[str, str]:
    """Revoke the current session and clear the browser session cookie."""
    settings = get_settings()
    if settings.auth_disabled:
        _delete_session_cookie(response, settings)
        return {"status": "logged_out"}
    actor = await _require_actor(request)
    await _require_csrf(actor, x_csrf_token)
    async with connect() as conn:
        await revoke_session(conn, session_id=actor.session_id)
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="auth.logout",
            target_type="session",
            target_id=actor.session_id,
            outcome="success",
        )
    _delete_session_cookie(response, settings)
    return {"status": "logged_out"}


async def get_session(request: Request) -> AuthSessionResponse:
    """Return the current authenticated session and rotate the CSRF token when needed."""
    settings = get_settings()
    actor = await _load_actor(request)
    if actor is None:
        return AuthSessionResponse(authenticated=False)
    if settings.auth_disabled:
        async with connect() as conn:
            user = await load_bootstrap_admin_user(conn, settings)
        return AuthSessionResponse(
            authenticated=True,
            user=(
                _auth_user_response(user)
                if user is not None
                else AuthUserResponse(
                    id=actor.user_id,
                    username=settings.bootstrap_admin_username,
                    display_name=settings.bootstrap_admin_username,
                    platform_role=PlatformRole.platform_admin.value,
                    password_change_required=False,
                )
            ),
            authentication_method=actor.authentication_method.value,
            session_id=actor.session_id,
            csrf_token=None,
            expires_at=None,
        )
    async with connect() as conn:
        user = await load_session_user(conn, user_id=actor.user_id)
        csrf_token = await rotate_csrf_token(conn, session_id=actor.session_id)
    if user is None:
        return AuthSessionResponse(authenticated=False)
    return AuthSessionResponse(
        authenticated=True,
        user=_auth_user_response(user),
        authentication_method=actor.authentication_method.value,
        session_id=actor.session_id,
        csrf_token=csrf_token,
        expires_at=None,
    )


async def admin_list_users(request: Request) -> list[dict[str, Any]]:
    """List platform users for platform administrators."""
    actor = await _require_actor(request)
    _require_platform_admin(actor)
    async with connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT id, username, email, display_name, platform_role, is_disabled, created_at, updated_at
                FROM users
                ORDER BY created_at DESC
                """
            )
        )
        return [cast(dict[str, Any], _jsonable(dict(row))) for row in result.mappings()]


async def admin_create_user(payload: UserCreate, request: Request) -> dict[str, Any]:
    """Create a platform user record from administrator input."""
    actor = await _require_actor(request)
    _require_platform_admin(actor)
    platform_role = PlatformRole(payload.platform_role)
    user_id = str(uuid.uuid4())
    async with connect() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users(id, username, email, display_name, platform_role, is_disabled)
                VALUES (:id, :username, :email, :display_name, :platform_role, :is_disabled)
                """
            ),
            {
                "id": user_id,
                "username": payload.username,
                "email": payload.email,
                "display_name": payload.display_name or payload.username,
                "platform_role": platform_role.value,
                "is_disabled": payload.is_disabled,
            },
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="admin.user_created",
            target_type="user",
            target_id=user_id,
            outcome="success",
        )
    return {"id": user_id, "username": payload.username, "platform_role": platform_role.value}


async def admin_patch_user(user_id: str, payload: UserPatch, request: Request) -> dict[str, str]:
    """Update administrator-managed user attributes without changing unspecified fields."""
    actor = await _require_actor(request)
    _require_platform_admin(actor)
    assignments = ["updated_at = now()"]
    params: dict[str, Any] = {"id": user_id}
    if payload.email is not None:
        assignments.append("email = :email")
        params["email"] = payload.email
    if payload.display_name is not None:
        assignments.append("display_name = :display_name")
        params["display_name"] = payload.display_name
    if payload.platform_role is not None:
        assignments.append("platform_role = :platform_role")
        params["platform_role"] = PlatformRole(payload.platform_role).value
    if payload.is_disabled is not None:
        assignments.append("is_disabled = :is_disabled")
        params["is_disabled"] = payload.is_disabled
    async with connect() as conn:
        await conn.execute(text(f"UPDATE users SET {', '.join(assignments)} WHERE id = :id"), params)  # noqa: S608
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="admin.user_updated",
            target_type="user",
            target_id=user_id,
            outcome="success",
        )
    return {"status": "updated"}


async def list_groups(request: Request) -> list[dict[str, Any]]:
    """List global workspace groups for platform administrators."""
    actor = await _require_actor(request)
    _require_platform_admin(actor)
    async with connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT g.id, g.name, g.description, g.group_type, g.external_id, g.created_at, g.updated_at,
                       count(gm.user_id)::int AS member_count,
                       COALESCE(json_agg(gm.user_id) FILTER (WHERE gm.user_id IS NOT NULL), '[]') AS member_user_ids
                FROM groups g
                LEFT JOIN group_memberships gm ON gm.group_id = g.id
                GROUP BY g.id
                ORDER BY g.name
                """
            )
        )
        return [cast(dict[str, Any], _jsonable(dict(row))) for row in result.mappings()]


async def list_access_groups(kb_id: str, request: Request) -> list[AccessGroupResponse]:
    """List workspace groups that a KB sharer may select for a grant."""
    actor = await _require_actor(request)
    async with connect() as conn:
        resource = await WorkspaceGrantRepository(conn).load_knowledge_base(kb_id)
        if resource is None:
            raise HTTPException(status_code=404, detail="knowledge base not found")
        _read, _write, share, _delete = await WorkspaceGrantRepository(conn).authorize(
            user_id=actor.user_id,
            platform_role=WorkspacePlatformRole(actor.platform_role.value),
            resource=resource,
        )
        if not share:
            raise HTTPException(status_code=404, detail="knowledge base not found")
        result = await conn.execute(
            text(
                """
                SELECT id, name, group_type, external_id
                FROM groups
                ORDER BY name
                """
            )
        )
        return [AccessGroupResponse.model_validate(_jsonable(dict(row))) for row in result.mappings()]


async def create_group(payload: GroupCreate, request: Request) -> dict[str, str]:
    """Create a global local group. OIDC groups are provider-managed only."""
    actor = await _require_actor(request)
    _require_platform_admin(actor)
    group_id = str(uuid.uuid4())
    async with connect() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO groups(id, name, description, group_type)
                VALUES (:id, :name, :description, 'LOCAL')
                """
            ),
            {
                "id": group_id,
                "name": payload.name,
                "description": payload.description,
            },
        )
        await _replace_workspace_local_group_members(conn, group_id=group_id, member_user_ids=payload.member_user_ids)
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="group.created",
            target_type="group",
            target_id=group_id,
            outcome="success",
        )
    return {"id": group_id, "name": payload.name}


async def patch_group(group_id: str, payload: GroupPatch, request: Request) -> dict[str, str]:
    """Update group metadata and local group membership where allowed."""
    actor = await _require_actor(request)
    _require_platform_admin(actor)
    async with connect() as conn:
        group = await _load_workspace_group(conn, group_id=group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        if payload.name is not None:
            await conn.execute(
                text("UPDATE groups SET name = :name, updated_at = now() WHERE id = :id"),
                {"id": group_id, "name": payload.name},
            )
        if payload.description is not None:
            await conn.execute(
                text("UPDATE groups SET description = :description, updated_at = now() WHERE id = :id"),
                {"id": group_id, "description": payload.description},
            )
        if payload.member_user_ids is not None:
            if GroupType(group["group_type"]) != GroupType.local:
                raise HTTPException(status_code=409, detail="OIDC group membership is externally managed")
            await _replace_workspace_local_group_members(
                conn, group_id=group_id, member_user_ids=payload.member_user_ids
            )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="group.updated",
            target_type="group",
            target_id=group_id,
            outcome="success",
        )
    return {"status": "updated"}


async def delete_group(group_id: str, request: Request) -> dict[str, str]:
    """Delete an unused global group; group grants are never silently removed."""
    actor = await _require_actor(request)
    _require_platform_admin(actor)
    async with connect() as conn:
        group = await _load_workspace_group(conn, group_id=group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        references = await conn.execute(
            text("SELECT count(*) FROM resource_grants WHERE principal_type = 'GROUP' AND principal_id = :id::uuid"),
            {"id": group_id},
        )
        count = int(references.scalar_one())
        if count:
            raise HTTPException(status_code=409, detail={"code": "GROUP_IN_USE", "reference_count": min(count, 1000)})
        await conn.execute(text("DELETE FROM group_memberships WHERE group_id = :id"), {"id": group_id})
        await conn.execute(text("DELETE FROM groups WHERE id = :id"), {"id": group_id})
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="group.deleted",
            target_type="group",
            target_id=group_id,
            outcome="success",
        )
    return {"status": "deleted"}


async def get_knowledge_bases(request: Request) -> list[dict[str, Any]]:
    """List full KBs plus minimal shells for directly shared documents."""
    actor = await _require_actor(request)
    async with connect() as conn:
        visible = await WorkspaceGrantRepository(conn).list_visible_knowledge_bases(
            user_id=actor.user_id,
            platform_role=WorkspacePlatformRole(actor.platform_role.value),
        )
        result = await conn.execute(
            text("SELECT id, name, active_index FROM knowledge_bases WHERE id = ANY(:ids)"),
            {"ids": [resource.resource_id for resource, _, _, _ in visible]},
        )
        details = {str(row["id"]): dict(row) for row in result.mappings()}
    return [
        {
            "id": resource.resource_id,
            "name": str(details[resource.resource_id]["name"]),
            "active_index": str(details[resource.resource_id].get("active_index") or ""),
            "access_scope": access_scope,
            "write_access": write_access,
            "share_access": share_access,
            "owned_by_current_user": resource.owner_user_id == actor.user_id,
        }
        for resource, access_scope, write_access, share_access in visible
    ]


async def retrieval_profiles(
    request: Request,
    knowledge_base_ids: Annotated[list[str] | None, Query()] = None,
) -> RetrievalProfileCatalogResponse:
    """Return ACL-filtered profile compatibility for a complete retrieval scope."""

    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    kb_ids = _kb_scope_ids(knowledge_base_ids or [], settings.default_kb_id)
    async with connect() as conn:
        for kb_id in kb_ids:
            await _require_workspace_kb_read(conn, actor, kb_id)
        rows: list[dict[str, Any]] = []
        for kb_id in kb_ids:
            kb = await get_knowledge_base(conn, tenant_id, kb_id)
            if kb is None or not kb.get("active_index"):
                raise _kb_not_ready_http(
                    KnowledgeBaseNotReady("knowledge base has no active retrieval contract"),
                    actor.request_id,
                )
            row = await load_index_version_by_read_alias(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                read_alias=str(kb["active_index"]),
            )
            if row is None:
                raise _kb_not_ready_http(
                    KnowledgeBaseNotReady("active retrieval contract is unavailable"),
                    actor.request_id,
                )
            rows.append(dict(row))
        options: list[RetrievalProfileOption] = []
        for name in sorted(get_profile_catalog(settings).profiles):
            try:
                await resolve_retrieval_profile(
                    conn,
                    tenant_id=tenant_id,
                    knowledge_base_ids=kb_ids,
                    requested=name,
                    settings=settings,
                )
            except RetrievalProfileIncompatible:
                options.append(
                    RetrievalProfileOption(
                        name=name,
                        compatible=False,
                        reason_code="RETRIEVAL_PROFILE_INCOMPATIBLE",
                    )
                )
            except KnowledgeBaseNotReady:
                options.append(RetrievalProfileOption(name=name, compatible=False, reason_code="KB_NOT_READY"))
            else:
                options.append(RetrievalProfileOption(name=name, compatible=True))
        try:
            resolved = await resolve_retrieval_profile(
                conn,
                tenant_id=tenant_id,
                knowledge_base_ids=kb_ids,
                requested=None,
                settings=settings,
            )
        except RetrievalProfileIncompatible:
            resolved = None
    scope_hash = stable_hash(
        [
            *kb_ids,
            *sorted(f"{row.get('read_alias')}:{row.get('id')}:{row.get('embedding_alias')}" for row in rows),
        ],
        64,
    )
    return RetrievalProfileCatalogResponse(
        resolved_default=resolved.name if resolved is not None else None,
        scope_contract_hash=f"sha256:{scope_hash}",
        profiles=options,
        scope_error_code=("RETRIEVAL_PROFILE_INCOMPATIBLE" if resolved is None else None),
    )


async def create_knowledge_base(payload: KnowledgeBaseCreate, request: Request) -> dict[str, str]:
    """Create a workspace KB owned by the authenticated creator."""
    actor = await _require_actor(request)
    kb_id = str(uuid.uuid4())
    async with connect() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO knowledge_bases(id, name, owner_user_id)
                VALUES (:id, :name, :owner_user_id)
                """
            ),
            {"id": kb_id, "name": payload.name, "owner_user_id": actor.user_id},
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="knowledge_base.created",
            target_type="knowledge_base",
            target_id=kb_id,
            outcome="success",
        )
    return {"id": kb_id, "name": payload.name}


async def get_knowledge_base_endpoint(kb_id: str, request: Request) -> dict[str, Any]:
    """Read a full KB or safe partial shell under current workspace grants."""
    actor = await _require_actor(request)
    async with connect() as conn:
        repository = WorkspaceGrantRepository(conn)
        visible = await repository.list_visible_knowledge_bases(
            user_id=actor.user_id,
            platform_role=WorkspacePlatformRole(actor.platform_role.value),
        )
        selected = next((item for item in visible if item[0].resource_id == kb_id), None)
        if selected is None:
            raise HTTPException(status_code=404, detail="knowledge base not found")
        resource, access_scope, write_access, share_access = selected
        kb_result = await conn.execute(
            text("SELECT id, name, active_index, created_at, updated_at FROM knowledge_bases WHERE id = :id"),
            {"id": kb_id},
        )
        kb_row = kb_result.mappings().first()
    if kb_row is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if access_scope == "partial":
        return {
            "id": kb_id,
            "name": str(kb_row["name"]),
            "access_scope": "partial",
            "write_access": False,
            "share_access": False,
            "owned_by_current_user": False,
        }
    result = cast(dict[str, Any], _jsonable(dict(kb_row)))
    result.update(
        {
            "access_scope": "full",
            "write_access": write_access,
            "share_access": share_access,
            "owned_by_current_user": resource.owner_user_id == actor.user_id,
        }
    )
    return result


async def patch_knowledge_base(kb_id: str, payload: KnowledgeBasePatch, request: Request) -> dict[str, str]:
    """Rename a knowledge base after manager-role enforcement."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    if payload.name is None:
        return {"status": "unchanged"}
    async with connect() as conn:
        await _require_workspace_kb_write(conn, actor, kb_id)
        await conn.execute(
            text(
                """
                UPDATE knowledge_bases
                SET name = :name, updated_at = now()
                WHERE id = :id AND tenant_id = :tenant_id
                """
            ),
            {"id": kb_id, "tenant_id": tenant_id, "name": payload.name},
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="knowledge_base.updated",
            target_type="knowledge_base",
            target_id=kb_id,
            outcome="success",
        )
    return {"status": "updated"}


async def delete_knowledge_base(kb_id: str, request: Request) -> dict[str, str]:
    """Delete a bounded upload-only KB after owner-role enforcement."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    settings = get_settings()
    document_ids: list[str] = []
    artifact_keys: list[str] = []
    read_alias = READ_ALIAS
    async with connect() as conn:
        await _require_workspace_delete(WorkspaceGrantRepository(conn), actor, ResourceType.knowledge_base, kb_id)
        kb = await get_knowledge_base(conn, tenant_id, kb_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="knowledge base not found")
        read_alias = str(kb.get("active_index") or READ_ALIAS)
        document_result = await conn.execute(
            text(
                "SELECT id FROM documents WHERE tenant_id = :tenant_id AND knowledge_base_id = :id ORDER BY id LIMIT 26"
            ),
            {"id": kb_id, "tenant_id": tenant_id},
        )
        document_ids = [str(row["id"]) for row in document_result.mappings()]
        if len(document_ids) > 25:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "KB_DELETE_ASYNC_REQUIRED",
                        "message": "knowledge base is too large to delete synchronously",
                    }
                },
            )
        history_result = await conn.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM knowledge_sources "
                "WHERE tenant_id = :tenant_id AND knowledge_base_id = :id AND kind <> 'direct_upload') "
                "OR EXISTS(SELECT 1 FROM query_runs "
                "WHERE tenant_id = :tenant_id AND knowledge_base_id = :id "
                "AND mode NOT IN ('normal', 'extended')) "
                "OR EXISTS(SELECT 1 FROM research_episodes episode "
                "JOIN query_runs query_run ON query_run.id = episode.query_run_id "
                "WHERE query_run.tenant_id = :tenant_id AND query_run.knowledge_base_id = :id) "
                "OR EXISTS(SELECT 1 FROM research_tool_calls tool_call "
                "JOIN query_runs query_run ON query_run.id = tool_call.query_run_id "
                "WHERE query_run.tenant_id = :tenant_id AND query_run.knowledge_base_id = :id) "
                "OR EXISTS(SELECT 1 FROM research_runs "
                "WHERE tenant_id = :tenant_id AND knowledge_base_id = :id)"
            ),
            {"id": kb_id, "tenant_id": tenant_id},
        )
        if bool(history_result.scalar()):
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "KB_DELETE_ASYNC_REQUIRED", "message": "knowledge base has durable history"}},
            )
        artifact_result = await conn.execute(
            text("SELECT object_key FROM document_artifacts WHERE tenant_id = :tenant_id AND knowledge_base_id = :id"),
            {"id": kb_id, "tenant_id": tenant_id},
        )
        artifact_keys = [str(row["object_key"]) for row in artifact_result.mappings()]
        cleanup_statements = (
            "DELETE FROM retrieval_events WHERE tenant_id = :tenant_id AND query_run_id IN "
            "(SELECT id FROM query_runs WHERE tenant_id = :tenant_id AND knowledge_base_id = :id)",
            "DELETE FROM idempotency_records WHERE tenant_id = :tenant_id AND resource_id IN "
            "(SELECT id::text FROM query_runs WHERE tenant_id = :tenant_id AND knowledge_base_id = :id)",
            "DELETE FROM agent_runs WHERE tenant_id = :tenant_id AND query_run_id IN "
            "(SELECT id FROM query_runs WHERE tenant_id = :tenant_id AND knowledge_base_id = :id)",
            "DELETE FROM query_runs WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            # Projection events reference both the KB and its documents.  They are
            # derived state and must be removed before the canonical rows below.
            "DELETE FROM search_projection_events WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM document_sections WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM document_artifacts WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM ingestion_job_items WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM source_document_states WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM source_sync_runs WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM chunks WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM document_versions WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM documents WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM upload_sessions WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM upload_batches WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM ingestion_jobs WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM index_versions WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM legacy_id_mappings WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM knowledge_sources WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
            "DELETE FROM knowledge_base_grants WHERE knowledge_base_id = :id AND tenant_id = :tenant_id",
        )
        for statement in cleanup_statements:
            await conn.execute(
                text(statement),
                {"id": kb_id, "tenant_id": tenant_id},
            )
        await conn.execute(
            text("DELETE FROM knowledge_bases WHERE id = :id AND tenant_id = :tenant_id"),
            {"id": kb_id, "tenant_id": tenant_id},
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="knowledge_base.deleted",
            target_type="knowledge_base",
            target_id=kb_id,
            outcome="success",
        )
    for document_id in document_ids:
        await asyncio.to_thread(
            delete_document_chunks,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            document_id=document_id,
            settings=settings,
            read_alias=read_alias,
        )
    if artifact_keys:
        await asyncio.to_thread(delete_objects, artifact_keys, settings)
    return {"status": "deleted"}


def _workspace_grant_response(grant: AccessGrant) -> AccessGrantResponse:
    if grant.id is None:
        raise RuntimeError("persisted access grant must have an id")
    return AccessGrantResponse(
        id=grant.id,
        principal_type=grant.principal_type.value,
        principal_id=grant.principal_id,
        permission=grant.permission.value,
    )


async def _workspace_resource_or_404(
    repository: WorkspaceGrantRepository, resource_type: ResourceType, resource_id: str
) -> Any:
    resource = (
        await repository.load_knowledge_base(resource_id)
        if resource_type == ResourceType.knowledge_base
        else await repository.load_document(resource_id)
    )
    if resource is None:
        raise HTTPException(status_code=404, detail="resource not found")
    return resource


async def _require_workspace_share(
    repository: WorkspaceGrantRepository, actor: ActorContext, resource_type: ResourceType, resource_id: str
) -> Any:
    resource = await _workspace_resource_or_404(repository, resource_type, resource_id)
    _, _, share, _ = await repository.authorize(
        user_id=actor.user_id,
        platform_role=WorkspacePlatformRole(actor.platform_role.value),
        resource=resource,
    )
    if not share:
        raise HTTPException(status_code=404, detail="resource not found")
    return resource


async def _require_workspace_delete(
    repository: WorkspaceGrantRepository, actor: ActorContext, resource_type: ResourceType, resource_id: str
) -> Any:
    resource = await _workspace_resource_or_404(repository, resource_type, resource_id)
    _read, _write, _share, delete = await repository.authorize(
        user_id=actor.user_id,
        platform_role=WorkspacePlatformRole(actor.platform_role.value),
        resource=resource,
    )
    if not delete:
        raise HTTPException(status_code=404, detail="resource not found")
    return resource


async def _require_workspace_kb_write(conn: Any, actor: ActorContext, kb_id: str) -> str:
    """Authorize workspace KB writes and resolve the temporary storage key."""
    return await _require_workspace_kb_permission(conn, actor, kb_id, write_required=True)


async def _require_workspace_kb_read(conn: Any, actor: ActorContext, kb_id: str) -> str:
    """Authorize workspace KB reads and resolve the temporary storage key."""
    return await _require_workspace_kb_permission(conn, actor, kb_id, write_required=False)


async def _require_workspace_kb_permission(conn: Any, actor: ActorContext, kb_id: str, *, write_required: bool) -> str:
    """Keep tenant IDs an internal legacy storage key, never request authority."""
    repository = WorkspaceGrantRepository(conn)
    resource = await repository.load_knowledge_base(kb_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    _read, write_access, _share, _delete = await repository.authorize(
        user_id=actor.user_id,
        platform_role=WorkspacePlatformRole(actor.platform_role.value),
        resource=resource,
    )
    if not (write_access if write_required else _read):
        raise HTTPException(status_code=404, detail="knowledge base not found")
    legacy_row = await conn.execute(text("SELECT tenant_id FROM knowledge_bases WHERE id = :id"), {"id": kb_id})
    row = legacy_row.mappings().first()
    if row is None or row["tenant_id"] is None:
        raise HTTPException(status_code=409, detail="knowledge base migration is incomplete")
    return str(row["tenant_id"])


async def _require_full_workspace_kb_scope(conn: Any, *, actor: ActorContext, kb_ids: Sequence[str]) -> str:
    """Authorize a retrieval scope before resolving its transitional storage key.

    Chat and research currently need one legacy storage partition for their
    durable query rows.  That partition is derived only after the workspace
    resolver has accepted every requested KB; it is never read from a session.
    """
    repository = WorkspaceGrantRepository(conn)
    visible = await repository.list_visible_knowledge_bases(
        user_id=actor.user_id,
        platform_role=WorkspacePlatformRole(actor.platform_role.value),
    )
    scopes = {resource.resource_id: scope for resource, scope, _write, _share in visible}
    if not kb_ids or any(scopes.get(kb_id) != "full" for kb_id in kb_ids):
        raise HTTPException(status_code=404, detail="knowledge base not found")
    result = await conn.execute(
        text("SELECT tenant_id FROM knowledge_bases WHERE id::text = ANY(:ids)"), {"ids": list(kb_ids)}
    )
    tenant_ids = {str(row["tenant_id"]) for row in result.mappings() if row["tenant_id"] is not None}
    if len(tenant_ids) != 1:
        raise HTTPException(status_code=409, detail="workspace retrieval migration is incomplete")
    return next(iter(tenant_ids))


async def list_workspace_access_grants(
    resource_type: ResourceType, resource_id: str, request: Request
) -> AccessGrantListResponse:
    actor = await _require_actor(request)
    async with connect() as conn:
        repository = WorkspaceGrantRepository(conn)
        resource = await _require_workspace_share(repository, actor, resource_type, resource_id)
        grants = await repository.load_grants(resource_type, resource_id)
    return AccessGrantListResponse(
        access_grants=[_workspace_grant_response(grant) for grant in grants],
        inherits_kb_access=resource.inherits_kb_access if resource_type == ResourceType.document else None,
    )


async def replace_workspace_access_grants(
    resource_type: ResourceType,
    resource_id: str,
    payload: AccessGrantReplaceRequest,
    request: Request,
) -> AccessGrantListResponse:
    if resource_type == ResourceType.knowledge_base and payload.inherits_kb_access is not None:
        raise HTTPException(status_code=422, detail="inherits_kb_access applies only to documents")
    actor = await _require_actor(request)
    requested = [
        AccessGrant(
            PrincipalType(grant.principal_type),
            grant.principal_id,
            ResourcePermission(grant.permission),
        )
        for grant in payload.access_grants
    ]
    async with connect() as conn:
        repository = WorkspaceGrantRepository(conn)
        resource = await _require_workspace_share(repository, actor, resource_type, resource_id)
        try:
            grants = await repository.replace_grants(
                resource_type=resource_type, resource_id=resource_id, grants=requested
            )
        except InvalidGrantError as exc:
            raise HTTPException(status_code=422, detail="invalid grant principal") from exc
        if resource_type == ResourceType.document and payload.inherits_kb_access is not None:
            await conn.execute(
                text("UPDATE documents SET inherits_kb_access = :inherits WHERE id = :id"),
                {"inherits": payload.inherits_kb_access, "id": resource_id},
            )
            resource = await _workspace_resource_or_404(repository, resource_type, resource_id)
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="workspace_resource.grants_replaced",
            target_type=resource_type.value,
            target_id=resource_id,
            outcome="success",
        )
    return AccessGrantListResponse(
        access_grants=[_workspace_grant_response(grant) for grant in grants],
        inherits_kb_access=resource.inherits_kb_access if resource_type == ResourceType.document else None,
    )


async def list_knowledge_base_access_grants(kb_id: str, request: Request) -> AccessGrantListResponse:
    return await list_workspace_access_grants(ResourceType.knowledge_base, kb_id, request)


async def replace_knowledge_base_access_grants(
    kb_id: str, payload: AccessGrantReplaceRequest, request: Request
) -> AccessGrantListResponse:
    return await replace_workspace_access_grants(ResourceType.knowledge_base, kb_id, payload, request)


async def list_document_access_grants(document_id: str, request: Request) -> AccessGrantListResponse:
    return await list_workspace_access_grants(ResourceType.document, document_id, request)


async def replace_document_access_grants(
    document_id: str, payload: AccessGrantReplaceRequest, request: Request
) -> AccessGrantListResponse:
    return await replace_workspace_access_grants(ResourceType.document, document_id, payload, request)


async def list_sources(kb_id: str, request: Request) -> dict[str, Any]:
    """List external knowledge sources without exposing stored credentials."""
    actor = await _require_actor(request)
    async with connect() as conn:
        tenant_id = await _require_workspace_kb_read(conn, actor, kb_id)
        rows = await list_knowledge_sources_public(conn, tenant_id=tenant_id, knowledge_base_id=kb_id)
    sources = [SourceResponse.model_validate(_source_public_payload(row)).model_dump(mode="json") for row in rows]
    return {"sources": sources}


async def create_source(kb_id: str, payload: SourceCreate, request: Request) -> SourceResponse:
    """Create an external source with credentials stored only in encrypted server state."""
    settings = get_settings()
    actor = await _require_actor(request)
    _reject_secrets_in_config(payload.config)
    async with connect() as conn:
        tenant_id = await _require_workspace_kb_write(conn, actor, kb_id)
        metadata = {**payload.metadata, "source_contract": "external_source_v1"}
        source_id = await create_knowledge_source(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            kind=payload.kind,
            name=payload.name,
            config=payload.config,
            encrypted_credentials=encrypt_server_tokens(settings, payload.credentials) if payload.credentials else {},
            metadata=metadata,
            refresh_interval_seconds=payload.refresh_interval_seconds,
        )
        row = await get_knowledge_source(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, source_id=str(source_id))
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="source.create",
            target_type="knowledge_source",
            target_id=str(source_id),
            outcome="success",
        )
    if row is None:
        raise HTTPException(status_code=500, detail="source was not created")
    return SourceResponse.model_validate(_source_public_payload(row))


async def get_source(kb_id: str, source_id: str, request: Request) -> SourceResponse:
    """Read a source configuration after viewer-role enforcement."""
    actor = await _require_actor(request)
    async with connect() as conn:
        tenant_id = await _require_workspace_kb_read(conn, actor, kb_id)
        row = await get_knowledge_source(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, source_id=source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    return SourceResponse.model_validate(_source_public_payload(row))


async def patch_source(kb_id: str, source_id: str, payload: SourcePatch, request: Request) -> SourceResponse:
    """Update source settings while keeping credentials out of public responses."""
    settings = get_settings()
    actor = await _require_actor(request)
    if payload.config is not None:
        _reject_secrets_in_config(payload.config)
    async with connect() as conn:
        tenant_id = await _require_workspace_kb_write(conn, actor, kb_id)
        existing = await get_knowledge_source(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, source_id=source_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="source not found")
        await update_knowledge_source(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            source_id=source_id,
            name=payload.name,
            status=payload.status,
            config=payload.config,
            encrypted_credentials=encrypt_server_tokens(settings, payload.credentials)
            if payload.credentials is not None
            else None,
            metadata=payload.metadata,
            refresh_interval_seconds=payload.refresh_interval_seconds,
            refresh_interval_supplied="refresh_interval_seconds" in payload.model_fields_set,
        )
        row = await get_knowledge_source(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, source_id=source_id)
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="source.update",
            target_type="knowledge_source",
            target_id=source_id,
            outcome="success",
        )
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    return SourceResponse.model_validate(_source_public_payload(row))


async def healthcheck_source(kb_id: str, source_id: str, request: Request) -> SourceHealthResponse:
    """Run a bounded connector healthcheck using decrypted server-side credentials."""
    settings = get_settings()
    actor = await _require_actor(request)
    async with connect() as conn:
        tenant_id = await _require_workspace_kb_write(conn, actor, kb_id)
        row = await get_knowledge_source(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            source_id=source_id,
            include_credentials=True,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    try:
        connector = connector_for_kind(
            str(row["kind"]),
            dict(row.get("config") or {}),
            _decrypt_api_credentials(settings, dict(row.get("encrypted_credentials") or {})),
        )
        result = await connector.healthcheck()
    except ConnectorError as exc:
        raise HTTPException(status_code=502, detail={"error": {"code": exc.code, "message": exc.safe_message}}) from exc
    return SourceHealthResponse(source_id=source_id, status=result.status, details=result.details)


async def sync_source(
    kb_id: str,
    source_id: str,
    payload: SourceSyncRequest,
    request: Request,
) -> SourceSyncResponse:
    """Queue a full or incremental source sync job for the worker."""
    settings = get_settings()
    actor = await _require_actor(request)
    async with connect() as conn:
        tenant_id = await _require_workspace_kb_write(conn, actor, kb_id)
        source = await get_knowledge_source(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, source_id=source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
    idempotency_record, owns_idempotency_record = await _claim_operation_idempotency(
        request=request,
        actor=actor,
        tenant_id=tenant_id,
        route=f"POST:/api/v1/knowledge-bases/{kb_id}/sources/{source_id}:sync",
        payload={"knowledge_base_id": kb_id, "source_id": source_id, **payload.model_dump(mode="json")},
        settings=settings,
    )
    if not owns_idempotency_record:
        safe_response = dict((idempotency_record or {}).get("safe_response") or {})
        if not safe_response:
            raise HTTPException(status_code=409, detail="idempotent source sync record is missing response")
        return SourceSyncResponse.model_validate(safe_response)
    async with connect() as conn:
        source = await get_knowledge_source(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, source_id=source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        job_id, run_id = await create_source_sync_job(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            source_id=source_id,
            mode=payload.mode,
            cursor_before=dict(source.get("sync_cursor") or {}),
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="source.sync",
            target_type="knowledge_source",
            target_id=source_id,
            outcome="success",
        )
    response = SourceSyncResponse(source_id=source_id, run_id=str(run_id), job_id=str(job_id), status="received")
    if idempotency_record is not None:
        async with connect() as conn:
            await complete_idempotency_record(
                conn,
                record_id=str(idempotency_record["id"]),
                resource_id=str(job_id),
                response_status=202,
                safe_response=response.model_dump(mode="json"),
            )
    return response


async def get_source_sync_run(run_id: str, request: Request) -> SourceSyncRunResponse:
    """Read source sync-run status after KB viewer authorization."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        row = await get_source_sync_run_public(conn, tenant_id=tenant_id, run_id=run_id)
        if row is not None:
            await _require_workspace_kb_read(conn, actor, str(row["knowledge_base_id"]))
    if row is None:
        raise HTTPException(status_code=404, detail="source sync run not found")
    return SourceSyncRunResponse.model_validate(_sync_run_public_payload(row))


async def create_wikipedia_import(payload: ImportRequest, request: Request) -> dict[str, str]:
    """Queue a bounded XML Wikipedia import job for the default knowledge base."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    try:
        xml_filename = configured_or_requested_filename(payload.xml_path, settings.wiki_xml_path)
        index_filename = configured_or_requested_filename(payload.index_path, settings.wiki_index_path)
    except ImportFileNameError as exc:
        raise _import_file_name_error() from exc
    config = {
        "limit": payload.limit,
        "xml_filename": xml_filename,
        "index_filename": index_filename,
        "snapshot_id": payload.snapshot_id or settings.wiki_snapshot_id,
        "retrieval_profile": settings.retrieval_profile,
    }
    async with connect() as conn:
        await _require_workspace_kb_write(conn, actor, settings.default_kb_id)
    idempotency_record, owns_idempotency_record = await _claim_operation_idempotency(
        request=request,
        actor=actor,
        tenant_id=tenant_id,
        route="POST:/api/v1/imports/wikipedia",
        payload=payload.model_dump(mode="json"),
        settings=settings,
    )
    if not owns_idempotency_record:
        safe_response = dict((idempotency_record or {}).get("safe_response") or {})
        if not safe_response:
            raise HTTPException(status_code=409, detail="idempotent import record is missing response")
        return {"job_id": str(safe_response["job_id"])}
    async with connect() as conn:
        job_id = await create_ingestion_job(
            conn,
            tenant_id,
            settings.default_kb_id,
            "wikipedia_xml",
            config,
        )
    response = {"job_id": str(job_id)}
    if idempotency_record is not None:
        async with connect() as conn:
            await complete_idempotency_record(
                conn,
                record_id=str(idempotency_record["id"]),
                resource_id=str(job_id),
                response_status=202,
                safe_response=response,
            )
    return response


async def create_zim_import(payload: ZimImportRequest, request: Request) -> dict[str, str]:
    """Queue a bounded ZIM Wikipedia import job for the default knowledge base."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    if payload.zim_path and payload.zim_filename and payload.zim_path != payload.zim_filename:
        raise _import_file_name_error()
    requested_filename = payload.zim_filename or payload.zim_path
    try:
        zim_filename = (
            resolve_import_filename(settings.zim_dir, requested_filename).name
            if requested_filename
            else (
                resolve_import_filename(settings.zim_dir, settings.zim_filename).name if settings.zim_filename else None
            )
        )
    except ImportFileNameError as exc:
        raise _import_file_name_error() from exc
    config = {
        "limit": payload.limit or 10000,
        "zim_filename": zim_filename,
        "snapshot_id": payload.snapshot_id,
        "kiwix_public_base_url": settings.kiwix_public_base_url,
        "kiwix_book_name": settings.kiwix_book_name,
        "retrieval_profile": settings.retrieval_profile,
    }
    async with connect() as conn:
        await _require_workspace_kb_write(conn, actor, settings.default_kb_id)
    idempotency_record, owns_idempotency_record = await _claim_operation_idempotency(
        request=request,
        actor=actor,
        tenant_id=tenant_id,
        route="POST:/api/v1/imports/zim",
        payload=payload.model_dump(mode="json"),
        settings=settings,
    )
    if not owns_idempotency_record:
        safe_response = dict((idempotency_record or {}).get("safe_response") or {})
        if not safe_response:
            raise HTTPException(status_code=409, detail="idempotent import record is missing response")
        return {"job_id": str(safe_response["job_id"])}
    async with connect() as conn:
        job_id = await create_ingestion_job(
            conn,
            tenant_id,
            settings.default_kb_id,
            "wikipedia_zim",
            config,
        )
    response = {"job_id": str(job_id)}
    if idempotency_record is not None:
        async with connect() as conn:
            await complete_idempotency_record(
                conn,
                record_id=str(idempotency_record["id"]),
                resource_id=str(job_id),
                response_status=202,
                safe_response=response,
            )
    return response


async def get_ingestion_job(job_id: str, request: Request) -> dict[str, Any]:
    """Read tenant-scoped ingestion job state."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM ingestion_jobs WHERE id = :id AND tenant_id = :tenant_id"),
            {"id": job_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return cast(dict[str, Any], _jsonable(dict(row)))


async def ingestion_job_events(job_id: str, request: Request) -> StreamingResponse:
    """Stream ingestion job progress until a terminal state is reached."""

    async def event_stream() -> AsyncIterator[str]:
        sequence = 0
        while True:
            sequence += 1
            job = await get_ingestion_job(job_id, request)
            yield _sse("job.progress", {"sequence": sequence, "job": job})
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(1)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def cancel_ingestion_job(job_id: str, request: Request) -> dict[str, str]:
    """Request cancellation for an authorized ingestion job."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        job = await _load_job_for_actor(conn, tenant_id=tenant_id, job_id=job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        await _require_workspace_kb_write(conn, actor, str(job["knowledge_base_id"]))
        await request_cancel(conn, job_id)
    return {"status": "cancel_requested"}


async def resume_ingestion_job(job_id: str, request: Request) -> dict[str, str]:
    """Request resume for an authorized ingestion job."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        job = await _load_job_for_actor(conn, tenant_id=tenant_id, job_id=job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        await _require_workspace_kb_write(conn, actor, str(job["knowledge_base_id"]))
        await request_resume(conn, job_id)
    return {"status": "resume_requested"}


_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")


def _optional_idempotency_key(request: Request) -> str | None:
    key = request.headers.get("idempotency-key")
    if key is None:
        return None
    normalized = key.strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(normalized):
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_IDEMPOTENCY_KEY", "message": "invalid Idempotency-Key"}},
        )
    return normalized


async def _claim_operation_idempotency(
    *,
    request: Request,
    actor: ActorContext,
    tenant_id: str,
    route: str,
    payload: dict[str, Any],
    settings: Settings,
    in_progress_code: str = "OPERATION_IN_PROGRESS",
    idempotency_key: str | None = None,
    replay_failed: bool = False,
) -> tuple[dict[str, Any] | None, bool]:
    key = idempotency_key or _optional_idempotency_key(request)
    if key is None:
        return None, True
    serialized_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    request_hash = stable_hash([route, serialized_payload], 64)
    async with connect() as conn:
        record, owner = await claim_idempotency_record(
            conn,
            tenant_id=tenant_id,
            actor_user_id=actor.user_id,
            route=route,
            idempotency_key=key,
            request_hash=request_hash,
            ttl_seconds=settings.idempotency_record_ttl_seconds,
        )
    if owner:
        return record, True
    if str(record.get("request_hash")) != request_hash:
        raise HTTPException(
            status_code=409,
            detail=_error_payload(
                code="IDEMPOTENCY_KEY_REUSED",
                message="Idempotency-Key was already used for a different request",
                request_id=actor.request_id,
            ),
        )
    if str(record.get("status")) == "completed":
        return record, False
    if str(record.get("status")) == "failed" and not replay_failed:
        raise HTTPException(
            status_code=409,
            detail=_error_payload(
                code="IDEMPOTENCY_OPERATION_FAILED",
                message="the previous operation with this Idempotency-Key failed",
                request_id=actor.request_id,
                details={"operation_id": str(record.get("resource_id") or "")},
            ),
        )
    raise HTTPException(
        status_code=409,
        detail=_error_payload(
            code=in_progress_code,
            message="an operation with this Idempotency-Key is already in progress",
            request_id=actor.request_id,
            details={"operation_id": str(record.get("resource_id") or "")},
        ),
    )


async def _accepted_batch_from_sessions(
    *,
    tenant_id: str,
    batch_id: str,
    knowledge_base_id: str,
    settings: Settings,
) -> UploadBatchAccepted:
    async with connect() as conn:
        sessions = await list_upload_batch_sessions_private(conn, tenant_id=tenant_id, batch_id=batch_id)
    items: list[UploadBatchItemAccepted] = []
    for session in sessions:
        upload_url = await asyncio.to_thread(
            create_presigned_put_url,
            str(session["object_key"]),
            content_type=str(session["content_type"]),
            expires_seconds=settings.upload_session_ttl_seconds,
            settings=settings,
        )
        items.append(
            UploadBatchItemAccepted(
                upload_session_id=str(session["id"]),
                upload_url=upload_url,
                expires_at=session["expires_at"],
                required_headers={"Content-Type": str(session["content_type"])},
                filename=str(session["filename"]),
                content_type=str(session["content_type"]),
                size_bytes=int(session["size_bytes"]),
                checksum_sha256=str(session["checksum_sha256"]),
            )
        )
    return UploadBatchAccepted(
        batch_id=batch_id,
        knowledge_base_id=knowledge_base_id,
        status="received",
        total_items=len(items),
        items=items,
    )


_UPLOAD_PROVENANCE_RESERVED_METADATA = frozenset(
    {
        "source_ref",
        "source_reference",
        "source_provenance",
        "source_document_id",
        "source_chunk_id",
        "document_id",
        "document_version_id",
        "tenant_id",
        "knowledge_base_id",
        "object_key",
        "checksum_sha256",
        "filename",
        "content_type",
        "size_bytes",
    }
)


def _parse_source_reference_json(raw: str) -> SourceReferenceInput | None:
    if not raw.strip():
        return None
    try:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("source_ref_json must be a JSON object")
        return SourceReferenceInput.model_validate(decoded)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_SOURCE_REFERENCE", "message": str(exc)}},
        ) from None


def _upload_session_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    source_ref: SourceReferenceInput | None = None,
    multipart: bool = False,
) -> dict[str, Any]:
    """Keep client display metadata separate from server-owned provenance."""

    values = dict(metadata or {})
    reserved = sorted(key for key in values if key.casefold() in _UPLOAD_PROVENANCE_RESERVED_METADATA)
    if reserved:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "RESERVED_UPLOAD_METADATA",
                    "message": f"metadata contains server-owned field: {reserved[0]}",
                }
            },
        )
    if source_ref is not None:
        values["source_reference"] = source_ref.model_dump(mode="json", exclude_none=True)
    if multipart:
        values["upload_transport"] = "multipart"
    return values


def _upload_document_identity(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    checksum_sha256: str,
    filename: str,
    parser_profile: str,
    source_ref: SourceReferenceInput | None,
) -> tuple[str, str, dict[str, Any] | None]:
    """Derive upload identity.  Legacy callers keep the prior dedup semantics."""

    if source_ref is None:
        document_id = "doc:" + stable_hash([tenant_id, knowledge_base_id, checksum_sha256, filename], 24)
        return (
            document_id,
            "docv:" + stable_hash([document_id, checksum_sha256, parser_profile, "normalized_document_v1"], 32),
            None,
        )
    reference = source_ref.model_dump(mode="json", exclude_none=True)
    reference.setdefault("source_version", f"sha256:{checksum_sha256}")
    source_id = direct_upload_source_id(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        namespace=str(reference["namespace"]),
    )
    document_id = source_document_id(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        source_id=source_id,
        external_id=str(reference["external_id"]),
    )
    return (
        document_id,
        source_document_version_id(
            document_id=document_id,
            source_version=str(reference["source_version"]),
            content_sha256=checksum_sha256,
            parser_profile=parser_profile,
        ),
        reference,
    )


def _merge_completion_metadata(
    session_metadata: Mapping[str, Any] | None,
    completion_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Completion can add display metadata but cannot replace upload identity."""

    additions = _upload_session_metadata(completion_metadata)
    values = dict(session_metadata or {})
    values.update(additions)
    return values


async def upload_document_multipart(
    kb_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    parser_profile: Annotated[str, Form()] = "standard",
    metadata_json: Annotated[str, Form()] = "{}",
    source_ref_json: Annotated[str, Form()] = "",
) -> UploadCompleteResponse:
    """Accept a small multipart document upload and enqueue asynchronous ingestion."""
    settings = get_settings()
    actor = await _require_actor(request)
    try:
        filename = safe_upload_filename(file.filename or "document")
    except UploadValidationError as exc:
        raise HTTPException(status_code=422, detail={"error": {"code": exc.code, "message": exc.safe_message}}) from exc
    try:
        metadata = json.loads(metadata_json) if metadata_json else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_METADATA_JSON", "message": "metadata_json must be valid JSON object"}},
        ) from exc
    if not isinstance(metadata, dict):
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_METADATA_JSON", "message": "metadata_json must be valid JSON object"}},
        )
    source_ref = _parse_source_reference_json(source_ref_json)
    data = await file.read()
    if len(data) > settings.upload_max_bytes:
        raise HTTPException(status_code=413, detail="uploaded file exceeds configured upload size limit")
    checksum = sha256_hex(data)
    content_type = file.content_type or "application/octet-stream"
    async with connect() as conn:
        tenant_id = await _require_workspace_kb_write(conn, actor, kb_id)
    object_key = f"uploads/{tenant_id}/{kb_id}/api/{stable_hash([filename, checksum], 16)}/{checksum}"
    idempotency_record, owns_idempotency_record = await _claim_operation_idempotency(
        request=request,
        actor=actor,
        tenant_id=tenant_id,
        route=f"POST:/api/v1/knowledge-bases/{kb_id}/documents",
        payload={
            "knowledge_base_id": kb_id,
            "filename": filename,
            "checksum_sha256": checksum,
            "parser_profile": parser_profile,
            "metadata": metadata,
            "source_ref": source_ref.model_dump(mode="json") if source_ref else None,
        },
        settings=settings,
    )
    if not owns_idempotency_record:
        safe_response = dict((idempotency_record or {}).get("safe_response") or {})
        if not safe_response:
            raise HTTPException(status_code=409, detail="idempotent upload record is missing response")
        return UploadCompleteResponse.model_validate(safe_response)
    await asyncio.to_thread(put_bytes, object_key, data, content_type=content_type, settings=settings)
    async with connect() as conn:
        session_id, _expires_at = await create_upload_session(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            owner_user_id=actor.user_id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            checksum_sha256=checksum,
            object_key=object_key,
            parser_profile=parser_profile,
            metadata=_upload_session_metadata(metadata, source_ref=source_ref, multipart=True),
            ttl_seconds=settings.upload_session_ttl_seconds,
        )
        upload_session = await get_upload_session(conn, tenant_id=tenant_id, upload_session_id=str(session_id))
        if upload_session is None:
            raise HTTPException(status_code=500, detail="upload session was not created")
        document_id, document_version_id, source_reference = _upload_document_identity(
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            checksum_sha256=checksum,
            filename=filename,
            parser_profile=parser_profile,
            source_ref=source_ref,
        )
        try:
            job_id, job_status = await create_document_upload_records(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                upload_session=upload_session,
                document_id=document_id,
                document_version_id=document_version_id,
                content_hash=checksum,
                metadata=_upload_session_metadata(metadata, source_ref=source_ref, multipart=True),
                source_reference=source_reference,
            )
        except DocumentVersionLifecycleError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": exc.code, "message": "document version requires explicit reprocess"}},
            ) from None
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="document.upload_multipart",
            target_type="document",
            target_id=document_id,
            outcome="success",
        )
    response = UploadCompleteResponse(
        document_id=document_id,
        document_version_id=document_version_id,
        job_id=str(job_id),
        status=job_status,
    )
    if idempotency_record is not None:
        async with connect() as conn:
            await complete_idempotency_record(
                conn,
                record_id=str(idempotency_record["id"]),
                resource_id=str(job_id),
                response_status=202,
                safe_response=response.model_dump(mode="json"),
            )
    return response


async def create_upload_session_endpoint(payload: UploadSessionCreate, request: Request) -> UploadSessionAccepted:
    """Create a presigned object-storage upload session without exposing object keys."""
    settings = get_settings()
    actor = await _require_actor(request)
    try:
        filename = safe_upload_filename(payload.filename)
    except UploadValidationError as exc:
        raise HTTPException(status_code=422, detail={"error": {"code": exc.code, "message": exc.safe_message}}) from exc
    kb_id = payload.knowledge_base_id or settings.default_kb_id
    async with connect() as conn:
        tenant_id = await _require_workspace_kb_write(conn, actor, kb_id)
    idempotency_record, owns_idempotency_record = await _claim_operation_idempotency(
        request=request,
        actor=actor,
        tenant_id=tenant_id,
        route="POST:/api/v1/uploads/sessions",
        payload=payload.model_dump(mode="json"),
        settings=settings,
    )
    if not owns_idempotency_record:
        safe_response = dict((idempotency_record or {}).get("safe_response") or {})
        replay_session_id = str(safe_response.get("upload_session_id") or "")
        async with connect() as conn:
            previous_session = await get_upload_session(conn, tenant_id=tenant_id, upload_session_id=replay_session_id)
        if previous_session is None:
            raise HTTPException(status_code=409, detail="idempotent upload session is unavailable")
        upload_url = await asyncio.to_thread(
            create_presigned_put_url,
            str(previous_session["object_key"]),
            content_type=str(previous_session["content_type"]),
            expires_seconds=settings.upload_session_ttl_seconds,
            settings=settings,
        )
        return UploadSessionAccepted(
            upload_session_id=str(previous_session["id"]),
            upload_url=upload_url,
            expires_at=previous_session["expires_at"],
            required_headers={"Content-Type": str(previous_session["content_type"])},
        )
    object_key = (
        f"uploads/{tenant_id}/{kb_id}/{stable_hash([filename, payload.checksum_sha256], 16)}/{payload.checksum_sha256}"
    )
    async with connect() as conn:
        session_id, expires_at = await create_upload_session(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            owner_user_id=actor.user_id,
            filename=filename,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
            checksum_sha256=payload.checksum_sha256.lower(),
            object_key=object_key,
            parser_profile=payload.parser_profile,
            metadata=_upload_session_metadata(payload.metadata, source_ref=payload.source_ref),
            ttl_seconds=settings.upload_session_ttl_seconds,
        )
    upload_url = await asyncio.to_thread(
        create_presigned_put_url,
        object_key,
        content_type=payload.content_type,
        expires_seconds=settings.upload_session_ttl_seconds,
        settings=settings,
    )
    response = UploadSessionAccepted(
        upload_session_id=str(session_id),
        upload_url=upload_url,
        expires_at=expires_at,
        required_headers={"Content-Type": payload.content_type},
    )
    if idempotency_record is not None:
        async with connect() as conn:
            await complete_idempotency_record(
                conn,
                record_id=str(idempotency_record["id"]),
                resource_id=str(session_id),
                response_status=202,
                safe_response={"upload_session_id": str(session_id)},
            )
    return response


async def create_upload_batch_endpoint(payload: UploadBatchCreate, request: Request) -> UploadBatchAccepted:
    """Create presigned upload sessions for a batch of validated files."""
    settings = get_settings()
    actor = await _require_actor(request)
    kb_id = payload.knowledge_base_id or settings.default_kb_id
    sanitized_items: list[tuple[int, str]] = []
    seen_items: set[tuple[str, str]] = set()
    for index, item in enumerate(payload.items):
        try:
            filename = safe_upload_filename(item.filename)
        except UploadValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": exc.code, "message": exc.safe_message, "details": {"item_index": index}}},
            ) from exc
        duplicate_key = (filename.casefold(), item.checksum_sha256.lower())
        if duplicate_key in seen_items:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "DUPLICATE_BATCH_ITEM",
                        "message": "batch contains duplicate filename/checksum item",
                        "details": {"item_index": index},
                    }
                },
            )
        seen_items.add(duplicate_key)
        sanitized_items.append((index, filename))
    async with connect() as conn:
        tenant_id = await _require_workspace_kb_write(conn, actor, kb_id)

    idempotency_record, owns_idempotency_record = await _claim_operation_idempotency(
        request=request,
        actor=actor,
        tenant_id=tenant_id,
        route="POST:/api/v1/uploads/batches",
        payload=payload.model_dump(mode="json"),
        settings=settings,
    )
    if not owns_idempotency_record:
        safe_response = dict((idempotency_record or {}).get("safe_response") or {})
        replay_batch_id = str(safe_response.get("batch_id") or "")
        replay_kb_id = str(safe_response.get("knowledge_base_id") or kb_id)
        if not replay_batch_id:
            raise HTTPException(status_code=409, detail="idempotent batch record is missing batch id")
        return await _accepted_batch_from_sessions(
            tenant_id=tenant_id,
            batch_id=replay_batch_id,
            knowledge_base_id=replay_kb_id,
            settings=settings,
        )

    async with connect() as conn:
        batch_id = await create_upload_batch(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            owner_user_id=actor.user_id,
            total_items=len(payload.items),
            metadata={**payload.metadata, "upload_contract": "upload_batches_v1"},
        )
        accepted_items: list[UploadBatchItemAccepted] = []
        for index, filename in sanitized_items:
            item = payload.items[index]
            checksum = item.checksum_sha256.lower()
            object_key = (
                f"uploads/{tenant_id}/{kb_id}/batches/{batch_id}/{index:04d}-"
                f"{stable_hash([filename, checksum], 16)}/{checksum}"
            )
            session_id, expires_at = await create_upload_session(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                owner_user_id=actor.user_id,
                batch_id=str(batch_id),
                filename=filename,
                content_type=item.content_type,
                size_bytes=item.size_bytes,
                checksum_sha256=checksum,
                object_key=object_key,
                parser_profile=item.parser_profile,
                metadata=_upload_session_metadata(item.metadata, source_ref=item.source_ref),
                ttl_seconds=settings.upload_session_ttl_seconds,
            )
            upload_url = await asyncio.to_thread(
                create_presigned_put_url,
                object_key,
                content_type=item.content_type,
                expires_seconds=settings.upload_session_ttl_seconds,
                settings=settings,
            )
            accepted_items.append(
                UploadBatchItemAccepted(
                    upload_session_id=str(session_id),
                    upload_url=upload_url,
                    expires_at=expires_at,
                    required_headers={"Content-Type": item.content_type},
                    filename=filename,
                    content_type=item.content_type,
                    size_bytes=item.size_bytes,
                    checksum_sha256=checksum,
                )
            )
    response = UploadBatchAccepted(
        batch_id=str(batch_id),
        knowledge_base_id=kb_id,
        status="received",
        total_items=len(accepted_items),
        items=accepted_items,
    )
    if idempotency_record is not None:
        async with connect() as conn:
            await complete_idempotency_record(
                conn,
                record_id=str(idempotency_record["id"]),
                resource_id=str(batch_id),
                response_status=202,
                safe_response={"batch_id": str(batch_id), "knowledge_base_id": kb_id},
            )
    return response


async def get_upload_batch_endpoint(batch_id: str, request: Request) -> UploadBatchStatus:
    """Read safe upload batch progress for an authorized editor."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        status = await get_upload_batch_status(conn, tenant_id=tenant_id, batch_id=batch_id)
        if status is not None:
            await _require_workspace_kb_write(conn, actor, str(status["knowledge_base_id"]))
    if status is None:
        raise HTTPException(status_code=404, detail="upload batch not found")
    return UploadBatchStatus.model_validate(status)


async def complete_upload_session_endpoint(
    upload_session_id: str,
    request: Request,
    payload: UploadSessionComplete | None = None,
) -> UploadCompleteResponse:
    """Validate an uploaded object and enqueue document ingestion records."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        session = await get_upload_session(
            conn,
            tenant_id=tenant_id,
            upload_session_id=upload_session_id,
        )
        if session is not None:
            await _require_workspace_kb_write(conn, actor, str(session["knowledge_base_id"]))
    if session is None:
        raise HTTPException(status_code=404, detail="upload session not found")
    idempotency_record, owns_idempotency_record = await _claim_operation_idempotency(
        request=request,
        actor=actor,
        tenant_id=tenant_id,
        route=f"POST:/api/v1/uploads/sessions/{upload_session_id}:complete",
        payload={
            "upload_session_id": upload_session_id,
            "metadata": payload.model_dump(mode="json") if payload else {},
        },
        settings=settings,
    )
    if not owns_idempotency_record:
        safe_response = dict((idempotency_record or {}).get("safe_response") or {})
        if not safe_response:
            raise HTTPException(status_code=409, detail="idempotent completion record is missing response")
        return UploadCompleteResponse.model_validate(safe_response)
    if str(session["status"]) not in {"created", "uploaded"}:
        raise HTTPException(status_code=409, detail="upload session is not completable")
    expires_at = session["expires_at"]
    if isinstance(expires_at, datetime) and expires_at < datetime.now(UTC):
        raise HTTPException(status_code=409, detail="upload session expired")
    try:
        head = await asyncio.to_thread(head_object, str(session["object_key"]), settings)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="uploaded object is not available") from exc
    if int(head["content_length"]) != int(session["size_bytes"]):
        raise HTTPException(status_code=409, detail="uploaded object size mismatch")
    session_metadata = dict(session.get("metadata") or {})
    source_reference = session_metadata.pop("source_reference", None)
    source_ref = SourceReferenceInput.model_validate(source_reference) if isinstance(source_reference, dict) else None
    document_id, document_version_id, normalized_source_reference = _upload_document_identity(
        tenant_id=tenant_id,
        knowledge_base_id=str(session["knowledge_base_id"]),
        checksum_sha256=str(session["checksum_sha256"]),
        filename=str(session["filename"]),
        parser_profile=str(session["parser_profile"]),
        source_ref=source_ref,
    )
    session_metadata = _merge_completion_metadata(session_metadata, payload.metadata if payload else None)
    async with connect() as conn:
        try:
            job_id, job_status = await create_document_upload_records(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=str(session["knowledge_base_id"]),
                upload_session={**session, "metadata": session_metadata},
                document_id=document_id,
                document_version_id=document_version_id,
                content_hash=str(session["checksum_sha256"]),
                metadata=session_metadata,
                source_reference=normalized_source_reference,
            )
        except DocumentVersionLifecycleError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": exc.code, "message": "document version requires explicit reprocess"}},
            ) from None
    response = UploadCompleteResponse(
        document_id=document_id,
        document_version_id=document_version_id,
        job_id=str(job_id),
        status=job_status,
    )
    if idempotency_record is not None:
        async with connect() as conn:
            await complete_idempotency_record(
                conn,
                record_id=str(idempotency_record["id"]),
                resource_id=str(job_id),
                response_status=202,
                safe_response=response.model_dump(mode="json"),
            )
    return response


async def get_document(document_id: str, request: Request) -> dict[str, Any]:
    """Read a document only after current workspace grant authorization."""
    actor = await _require_actor(request)
    async with connect() as conn:
        document, resource, write_access, share_access = await _load_workspace_viewer_document(
            conn, actor=actor, document_id=document_id
        )
    payload = _document_public_payload(document)
    payload.update(
        {
            "inherits_kb_access": resource.inherits_kb_access,
            "write_access": write_access,
            "share_access": share_access,
            "owned_by_current_user": resource.owner_user_id == actor.user_id,
        }
    )
    return cast(dict[str, Any], _jsonable(payload))


async def get_document_versions(document_id: str, request: Request) -> dict[str, Any]:
    """List public document versions after viewer-role enforcement."""
    actor = await _require_actor(request)
    async with connect() as conn:
        document, resource, write_access, share_access = await _load_workspace_viewer_document(
            conn, actor=actor, document_id=document_id
        )
        tenant_id = str(document["tenant_id"])
        versions = await list_document_versions_public(conn, tenant_id, document_id)
    return {"document_id": document_id, "versions": _jsonable(versions)}


async def get_document_structure(document_id: str, request: Request) -> DocumentStructureResponse:
    """Return the published document section tree for the viewer."""
    actor = await _require_actor(request)
    async with connect() as conn:
        document, resource, write_access, share_access = await _load_workspace_viewer_document(
            conn, actor=actor, document_id=document_id
        )
        tenant_id = str(document["tenant_id"])
        version_id = str(document["current_version_id"]) if document.get("current_version_id") else None
        sections = await list_document_sections(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=str(document["knowledge_base_id"]),
            document_id=document_id,
            document_version_id=version_id,
        )
    metadata = dict(document.get("metadata") or {})
    source_url = metadata.get("source_url")
    return DocumentStructureResponse(
        document_id=document_id,
        document_version_id=version_id,
        knowledge_base_id=str(document["knowledge_base_id"]),
        title=str(document.get("title") or ""),
        source_type=str(document.get("source_type") or ""),
        source_url=str(source_url) if source_url else None,
        sections=[_document_section(row) for row in sections],
        public_metadata=dict(document.get("public_metadata") or {}),
        inherits_kb_access=resource.inherits_kb_access,
        write_access=write_access,
        share_access=share_access,
        owned_by_current_user=resource.owner_user_id == actor.user_id,
        provenance=SourceProvenance.model_validate(
            public_provenance_from_metadata(
                dict(document.get("public_metadata") or {}),
                document_id=document_id,
                document_version_id=version_id or "",
                source_uri=str(document.get("source_uri") or ""),
                source_url=str(source_url or ""),
            )
        ),
    )


async def get_document_context(
    document_id: str,
    request: Request,
    chunk_id: str | None = Query(default=None, max_length=240),
    section_id: str | None = Query(default=None, max_length=240),
    before: int = Query(default=2, ge=0, le=10),
    after: int = Query(default=2, ge=0, le=10),
    limit: int = Query(default=80, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=500),
) -> DocumentContextResponse:
    """Return neighboring chunks around a document chunk or section."""
    actor = await _require_actor(request)
    async with connect() as conn:
        document, _resource, _write, _share = await _load_workspace_viewer_document(
            conn, actor=actor, document_id=document_id
        )
        tenant_id = str(document["tenant_id"])
        version_id = str(document["current_version_id"]) if document.get("current_version_id") else None
        section_path: list[str] | None = None
        if section_id:
            sections = await list_document_sections(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=str(document["knowledge_base_id"]),
                document_id=document_id,
                document_version_id=version_id,
            )
            for section in sections:
                if str(section.get("section_id") or "") == section_id:
                    section_path = [str(item) for item in section.get("path") or []]
                    break
            if section_path is None:
                raise HTTPException(status_code=404, detail="section not found")
        rows = await fetch_document_context_chunks(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=str(document["knowledge_base_id"]),
            document_id=document_id,
            document_version_id=version_id,
            chunk_id=chunk_id,
            section_path=section_path,
            before=before,
            after=after,
            limit=limit,
            offset=offset,
        )
    if chunk_id and not rows:
        raise HTTPException(status_code=404, detail="chunk not found")
    return DocumentContextResponse(
        document_id=document_id,
        document_version_id=version_id,
        anchor_chunk_id=chunk_id,
        section_id=section_id,
        chunks=[_document_context_chunk(row, anchor_chunk_id=chunk_id) for row in rows],
        limit=limit,
        offset=offset,
    )


async def search_document(document_id: str, payload: DocumentSearchRequest, request: Request) -> DocumentSearchResponse:
    """Search within a single authorized document."""
    actor = await _require_actor(request)
    async with connect() as conn:
        document, _resource, _write, _share = await _load_workspace_viewer_document(
            conn, actor=actor, document_id=document_id
        )
        tenant_id = str(document["tenant_id"])
        version_id = str(document["current_version_id"]) if document.get("current_version_id") else None
        rows = await search_document_chunks(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=str(document["knowledge_base_id"]),
            document_id=document_id,
            document_version_id=version_id,
            query=payload.query,
            limit=payload.limit,
            offset=payload.offset,
        )
    has_more = len(rows) > payload.limit
    return DocumentSearchResponse(
        document_id=document_id,
        document_version_id=version_id,
        results=[_document_search_result(row, query=payload.query) for row in rows[: payload.limit]],
        limit=payload.limit,
        offset=payload.offset,
        has_more=has_more,
    )


async def delete_document(document_id: str, request: Request) -> DocumentDeleteResponse:
    """Soft-delete a document, remove searchable chunks, and schedule deferred purge."""
    settings = get_settings()
    actor = await _require_actor(request)
    purge_after = datetime.now(UTC) + timedelta(days=max(0, settings.document_soft_delete_retention_days))
    async with connect() as conn:
        repository = WorkspaceGrantRepository(conn)
        resource = await repository.load_document(document_id)
        if resource is None:
            raise HTTPException(status_code=404, detail="document not found")
        _, _, _, delete_access = await repository.authorize(
            user_id=actor.user_id,
            platform_role=WorkspacePlatformRole(actor.platform_role.value),
            resource=resource,
        )
        if not delete_access:
            raise HTTPException(status_code=404, detail="document not found")
        document_result = await conn.execute(
            text(
                "SELECT tenant_id, knowledge_base_id, lifecycle_state, deleted_at, purge_after "
                "FROM documents WHERE id = :id"
            ),
            {"id": document_id},
        )
        row = document_result.mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="document not found")
        document = dict(row)
        tenant_id = str(document["tenant_id"])
        kb_id = str(document["knowledge_base_id"])
        kb_result = await conn.execute(text("SELECT active_index FROM knowledge_bases WHERE id = :id"), {"id": kb_id})
        kb = kb_result.mappings().first()
        read_alias = str(kb["active_index"] or READ_ALIAS) if kb else READ_ALIAS
        if document.get("lifecycle_state") == "deleted":
            return DocumentDeleteResponse(
                document_id=document_id,
                job_id=None,
                lifecycle_state="deleted",
                purge_after=cast(datetime | None, document.get("purge_after")),
            )
        await soft_delete_document(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            document_id=document_id,
            deleted_by_user_id=actor.user_id,
            purge_after=purge_after,
            deletion_reason="user_requested",
        )
        job_id = await create_document_deletion_job(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            document_id=document_id,
            purge_after=purge_after,
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="document.delete_requested",
            target_type="document",
            target_id=document_id,
            outcome="success",
        )
    await asyncio.to_thread(
        delete_document_chunks,
        tenant_id=tenant_id,
        knowledge_base_id=kb_id,
        document_id=document_id,
        settings=settings,
        read_alias=read_alias,
    )
    return DocumentDeleteResponse(
        document_id=document_id,
        job_id=str(job_id),
        lifecycle_state="deleting",
        purge_after=purge_after,
    )


async def reprocess_document(document_id: str, request: Request) -> DocumentReprocessResponse:
    """Queue reprocessing for the current published document version."""
    settings = get_settings()
    actor = await _require_actor(request)
    async with connect() as conn:
        document, _resource, write_access, _share_access = await _load_workspace_viewer_document(
            conn, actor=actor, document_id=document_id
        )
        if not document.get("current_version_id") or not write_access:
            raise HTTPException(status_code=404, detail="document not found")
        tenant_result = await conn.execute(text("SELECT tenant_id FROM documents WHERE id = :id"), {"id": document_id})
        tenant_row = tenant_result.mappings().first()
        if tenant_row is None:
            raise HTTPException(status_code=404, detail="document not found")
        tenant_id = str(tenant_row["tenant_id"])
    idempotency_record, owns_idempotency_record = await _claim_operation_idempotency(
        request=request,
        actor=actor,
        tenant_id=tenant_id,
        route=f"POST:/api/v1/documents/{document_id}:reprocess",
        payload={"document_id": document_id, "document_version_id": str(document["current_version_id"])},
        settings=settings,
    )
    if not owns_idempotency_record:
        safe_response = dict((idempotency_record or {}).get("safe_response") or {})
        if not safe_response:
            raise HTTPException(status_code=409, detail="idempotent reprocess record is missing response")
        return DocumentReprocessResponse.model_validate(safe_response)
    async with connect() as conn:
        job_id = await create_reprocess_job(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=str(document["knowledge_base_id"]),
            document_id=document_id,
            document_version_id=str(document["current_version_id"]),
        )
    response = DocumentReprocessResponse(
        document_id=document_id,
        document_version_id=str(document["current_version_id"]),
        job_id=str(job_id),
        status="received",
    )
    if idempotency_record is not None:
        async with connect() as conn:
            await complete_idempotency_record(
                conn,
                record_id=str(idempotency_record["id"]),
                resource_id=str(job_id),
                response_status=202,
                safe_response=response.model_dump(mode="json"),
            )
    return response


async def search(payload: SearchRequest, request: Request) -> SearchResponse:
    """Run search over KBs currently readable through workspace grants."""
    settings = get_settings()
    actor = await _require_actor(request)
    kb_ids = _kb_scope_ids(payload.knowledge_base_ids, settings.default_kb_id)
    try:
        async with connect() as conn:
            workspace_repository = WorkspaceGrantRepository(conn)
            visible = await workspace_repository.list_visible_knowledge_bases(
                user_id=actor.user_id,
                platform_role=WorkspacePlatformRole(actor.platform_role.value),
            )
            readable_kbs = {resource.resource_id for resource, scope, _write, _share in visible if scope == "full"}
            if not set(kb_ids).issubset(readable_kbs):
                raise HTTPException(status_code=404, detail="knowledge base not found")
            tenant_rows = await conn.execute(
                text("SELECT id, tenant_id FROM knowledge_bases WHERE id::text = ANY(:ids)"), {"ids": kb_ids}
            )
            tenant_ids = {str(row["tenant_id"]) for row in tenant_rows.mappings() if row["tenant_id"] is not None}
            if len(tenant_ids) != 1:
                raise HTTPException(status_code=409, detail="workspace retrieval migration is incomplete")
            tenant_id = next(iter(tenant_ids))
            resolved_profile = await resolve_retrieval_profile(
                conn,
                tenant_id=tenant_id,
                knowledge_base_ids=kb_ids,
                requested=payload.ranking_profile,
                settings=settings,
            )
            payload = payload.model_copy(update={"ranking_profile": resolved_profile.name})
            return await run_public_search(
                conn,
                payload,
                tenant_id=tenant_id,
                knowledge_base_ids=kb_ids,
                settings=settings,
                actor_user_id=actor.user_id,
                actor_platform_role=WorkspacePlatformRole(actor.platform_role.value),
            )
    except KnowledgeBaseNotReady as exc:
        raise _kb_not_ready_http(exc, actor.request_id) from exc
    raise RuntimeError("search response was not returned")


async def stream_chat_response(payload: ChatRequest, request: Request) -> StreamingResponse:
    """Run retrieval, answer generation, diagnostics persistence, and SSE streaming for chat."""
    settings = get_settings()
    actor = await _require_actor(request)
    kb_ids = _kb_scope_ids(payload.knowledge_base_ids, settings.default_kb_id)
    async with connect() as conn:
        tenant_id = await _require_full_workspace_kb_scope(conn, actor=actor, kb_ids=kb_ids)
    request_id = str(uuid.uuid4())
    effective_message = payload.message
    conversation_id = payload.conversation_id or str(uuid.uuid4())
    chat_overrides = deepcopy(payload.retrieval_overrides)
    chat_overrides.setdefault("answer", {})["ambiguity_mode"] = payload.ambiguity_mode
    if payload.conversation_id:
        async with connect() as conn:
            previous_run = await _load_conversation_run(
                conn,
                tenant_id=tenant_id,
                user_id=actor.user_id,
                conversation_id=payload.conversation_id,
            )
        if previous_run is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        assert previous_run is not None
        previous_usage = dict(previous_run.get("usage") or {})
        previous_interpretations = list(previous_usage.get("interpretations") or [])
        selected_label = ""
        if payload.selected_interpretation_id:
            for item in previous_interpretations:
                if str(item.get("interpretation_id") or "") == payload.selected_interpretation_id:
                    selected_label = str(item.get("label") or "")
                    break
            if not selected_label:
                raise HTTPException(status_code=400, detail="interpretation not found in conversation")
        original_question = str(previous_run.get("input_text") or "")
        effective_message = f"{original_question}\nУточнение пользователя: {payload.message}"
        if selected_label:
            effective_message += f"\nВыбранное значение: {selected_label}"
    trace_id = stable_hash([request_id, effective_message], 32)
    deadline = OperationDeadline.after(settings.chat_run_deadline_seconds)
    requested_profile = normalize_retrieval_profile_request(payload.retrieval_profile)
    try:
        active_profile = get_retrieval_profile(
            requested_profile or settings.retrieval_profile,
            settings,
            chat_overrides,
        )
    except (KeyError, ValueError) as exc:
        raise _unknown_retrieval_profile_http(exc, request_id) from exc
    if payload.conversation_id:
        assert previous_run is not None
        previous_scope = {
            str(item)
            for item in list(dict(previous_run.get("usage") or {}).get("knowledge_base_ids") or [])
            if str(item)
        }
        if previous_scope and not previous_scope.issubset(set(kb_ids)):
            raise HTTPException(status_code=404, detail="conversation is outside the requested knowledge-base scope")
    primary_kb_id = kb_ids[0]
    classifier_suggested_extended = should_start_extended(effective_message)
    route_decision = initial_route_decision(
        mode=payload.mode.value,
        extended_policy=active_profile.postprocess.extended_search,
        classifier_suggested_extended=classifier_suggested_extended,
    )
    search_plan = build_search_plan(
        query=effective_message,
        mode=payload.mode.value,
        route=route_decision["route"],
        route_reason=route_decision["reason"],
        knowledge_base_id=primary_kb_id,
        knowledge_base_ids=kb_ids,
        trace_id=trace_id,
        profile=active_profile,
    )
    try:
        async with connect() as conn:
            if requested_profile is None:
                active_profile = await resolve_retrieval_profile(
                    conn,
                    tenant_id=tenant_id,
                    knowledge_base_ids=kb_ids,
                    requested=None,
                    overrides=chat_overrides,
                    settings=settings,
                )
                route_decision = initial_route_decision(
                    mode=payload.mode.value,
                    extended_policy=active_profile.postprocess.extended_search,
                    classifier_suggested_extended=classifier_suggested_extended,
                )
                search_plan = build_search_plan(
                    query=effective_message,
                    mode=payload.mode.value,
                    route=route_decision["route"],
                    route_reason=route_decision["reason"],
                    knowledge_base_id=primary_kb_id,
                    knowledge_base_ids=kb_ids,
                    trace_id=trace_id,
                    profile=active_profile,
                )
            if requested_profile is not None:
                active_profile = await resolve_retrieval_profile(
                    conn,
                    tenant_id=tenant_id,
                    knowledge_base_ids=kb_ids,
                    requested=requested_profile,
                    overrides=chat_overrides,
                    settings=settings,
                )
    except KnowledgeBaseNotReady as exc:
        raise _kb_not_ready_http(exc, request_id) from exc
    except RetrievalProfileIncompatible as exc:
        raise _retrieval_profile_incompatible_http(exc, request_id) from exc
    except (KeyError, ValueError) as exc:
        raise _unknown_retrieval_profile_http(exc, request_id) from exc
    search_filters: dict[str, Any] = {}
    async with connect() as conn:
        initial_usage = _initial_query_run_usage(
            mode=payload.mode.value,
            profile=active_profile,
            retrieval_overrides=chat_overrides,
            knowledge_base_ids=kb_ids,
            route_decision=route_decision,
            trace_id=trace_id,
            settings=settings,
            conversation_id=conversation_id,
            ambiguity_mode=payload.ambiguity_mode,
        )
    idempotency_record, owns_idempotency_record = await _claim_operation_idempotency(
        request=request,
        actor=actor,
        tenant_id=tenant_id,
        route="POST:/api/v1/chat",
        payload=payload.model_dump(mode="json"),
        settings=settings,
        in_progress_code="QUERY_IN_PROGRESS",
        replay_failed=True,
        idempotency_key=(
            stable_hash(["chat_client_request", payload.client_request_id], 64) if payload.client_request_id else None
        ),
    )
    if not owns_idempotency_record:
        safe_response = dict((idempotency_record or {}).get("safe_response") or {})
        existing_id = str(safe_response.get("query_run_id") or "")
        if not existing_id:
            raise HTTPException(status_code=409, detail="idempotent chat record is missing query run id")
        async with connect() as conn:
            existing_run = await _load_query_run_for_actor(conn, tenant_id=tenant_id, query_run_id=existing_id)
        if existing_run is None:
            raise HTTPException(status_code=409, detail="idempotent chat query run is unavailable")
        return _replay_query_run_stream(request_id=request_id, query_run=existing_run)
    async with connect() as conn:
        query_run_id = await create_query_run(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=primary_kb_id,
            user_id=actor.user_id,
            request_id=request_id,
            client_request_id=payload.client_request_id,
            mode=payload.mode.value,
            input_text=effective_message,
            trace_id=trace_id,
            usage=initial_usage,
        )

    async def event_stream() -> AsyncIterator[str]:
        """Coordinate the event_stream workflow while preserving tenant and API contracts."""
        run_started = time.perf_counter()
        sequence = 1
        current_stage = "question_received"
        last_successful_stage = "question_received"
        retrieval: Any | None = None
        actual_search_plan = search_plan
        active_stage_task: asyncio.Task[Any] | None = None
        yield _event(
            SseEvent(
                event="run.started",
                request_id=request_id,
                query_run_id=str(query_run_id),
                sequence=sequence,
                data={
                    "trace_id": trace_id,
                    "search_plan": actual_search_plan,
                    "stage": current_stage,
                    "attempt": 1,
                    "elapsed_ms": 0,
                    "deadline_remaining_ms": deadline.remaining_ms(),
                },
            )
        )
        try:

            def stage_notice(event_name: str, stage: str, elapsed_ms: int = 0) -> str:
                return _event(
                    SseEvent(
                        event=event_name,
                        request_id=request_id,
                        query_run_id=str(query_run_id),
                        sequence=sequence,
                        data={
                            "stage": stage,
                            "elapsed_ms": elapsed_ms,
                            "attempt": 1,
                            "deadline_remaining_ms": deadline.remaining_ms(),
                        },
                    )
                )

            async def wait_for_stage_task(task: asyncio.Task[Any], *, stage: str, started: float) -> AsyncIterator[str]:
                """Emit heartbeats while one bounded stage is running."""

                nonlocal sequence
                try:
                    while not task.done():
                        timeout = deadline.timeout_seconds(settings.operation_heartbeat_seconds, stage=stage)
                        await asyncio.wait({task}, timeout=timeout)
                        if task.done():
                            break
                        if await request.is_disconnected():
                            raise asyncio.CancelledError
                        deadline.ensure_remaining(stage=stage)
                        sequence += 1
                        yield stage_notice("stage.heartbeat", stage, _elapsed_ms(started))
                except (asyncio.CancelledError, OperationDeadlineExceeded):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise

            async with connect_autocommit() as conn:
                current_stage = "path_selected"
                await insert_retrieval_event(
                    conn,
                    tenant_id=tenant_id,
                    query_run_id=str(query_run_id),
                    trace_id=trace_id,
                    event_type="query_stage",
                    stage="path_selected",
                    payload=_path_selection_event(
                        mode=payload.mode.value,
                        route_decision=route_decision,
                        knowledge_base_ids=kb_ids,
                        profile=active_profile,
                        search_plan=actual_search_plan,
                        retrieval_overrides=chat_overrides,
                    ),
                )
                use_harness_first = route_decision["route"] == "extended_first"
                last_successful_stage = "path_selected"
                if use_harness_first:
                    current_stage = "extended_search"
                    sequence += 1
                    yield stage_notice("stage.started", current_stage)
                    stage_started = time.perf_counter()
                    if await request.is_disconnected():
                        raise asyncio.CancelledError
                    retrieval_task = asyncio.create_task(
                        run_extended_search(
                            conn,
                            effective_message,
                            tenant_id=tenant_id,
                            knowledge_base_id=primary_kb_id,
                            query_run_id=str(query_run_id),
                            trace_id=trace_id,
                            settings=settings,
                            profile=active_profile,
                            profile_overrides=chat_overrides,
                            search_filters=search_filters,
                            knowledge_base_ids=kb_ids,
                            deadline=deadline,
                        )
                    )
                    active_stage_task = retrieval_task
                    async for heartbeat in wait_for_stage_task(
                        retrieval_task,
                        stage=current_stage,
                        started=stage_started,
                    ):
                        yield heartbeat
                    retrieval = await retrieval_task
                    active_stage_task = None
                    sequence += 1
                    yield stage_notice("stage.completed", current_stage, _elapsed_ms(stage_started))
                    last_successful_stage = "extended_search"
                else:
                    current_stage = "retrieval"
                    sequence += 1
                    yield stage_notice("stage.started", current_stage)
                    stage_started = time.perf_counter()
                    if await request.is_disconnected():
                        raise asyncio.CancelledError
                    if len(kb_ids) > 1:
                        retrieval_task = asyncio.create_task(
                            retrieve_multi(
                                conn,
                                effective_message,
                                tenant_id=tenant_id,
                                knowledge_base_ids=kb_ids,
                                query_run_id=str(query_run_id),
                                trace_id=trace_id,
                                settings=settings,
                                profile=active_profile,
                                profile_overrides=chat_overrides,
                                search_filters=search_filters,
                                deadline=deadline,
                            )
                        )
                    else:
                        retrieval_task = asyncio.create_task(
                            retrieve(
                                conn,
                                effective_message,
                                tenant_id=tenant_id,
                                knowledge_base_id=primary_kb_id,
                                query_run_id=str(query_run_id),
                                trace_id=trace_id,
                                settings=settings,
                                profile=active_profile,
                                search_filters=search_filters,
                                deadline=deadline,
                            )
                        )
                    active_stage_task = retrieval_task
                    async for heartbeat in wait_for_stage_task(
                        retrieval_task,
                        stage=current_stage,
                        started=stage_started,
                    ):
                        yield heartbeat
                    retrieval = await retrieval_task
                    active_stage_task = None
                    last_successful_stage = "retrieval"
                    sequence += 1
                    yield stage_notice("stage.completed", current_stage, _elapsed_ms(stage_started))
                    if (
                        retrieval.answerability
                        and should_try_extended_search(retrieval.answerability)
                        and active_profile.postprocess.extended_search
                        in {
                            "always",
                            "conditional",
                        }
                    ):
                        repair_decision = repair_route_decision(retrieval.answerability)
                        actual_search_plan = build_search_plan(
                            query=effective_message,
                            mode=payload.mode.value,
                            route=repair_decision["route"],
                            route_reason=repair_decision["reason"],
                            knowledge_base_id=primary_kb_id,
                            knowledge_base_ids=kb_ids,
                            trace_id=trace_id,
                            profile=active_profile,
                        )
                        current_stage = "extended_search"
                        sequence += 1
                        yield stage_notice("stage.started", current_stage)
                        stage_started = time.perf_counter()
                        await update_query_run_usage(
                            conn,
                            query_run_id=str(query_run_id),
                            usage={"extended_search_repair": repair_decision},
                        )
                        await insert_retrieval_event(
                            conn,
                            tenant_id=tenant_id,
                            query_run_id=str(query_run_id),
                            trace_id=trace_id,
                            event_type="query_stage",
                            stage="path_selected",
                            payload=_path_selection_event(
                                mode=payload.mode.value,
                                route_decision=repair_decision,
                                knowledge_base_ids=kb_ids,
                                profile=active_profile,
                                search_plan=actual_search_plan,
                                retrieval_overrides=chat_overrides,
                            ),
                        )
                        retrieval_task = asyncio.create_task(
                            run_extended_search(
                                conn,
                                effective_message,
                                tenant_id=tenant_id,
                                knowledge_base_id=primary_kb_id,
                                query_run_id=str(query_run_id),
                                trace_id=trace_id,
                                settings=settings,
                                profile=active_profile,
                                profile_overrides=chat_overrides,
                                search_filters=search_filters,
                                knowledge_base_ids=kb_ids,
                                seed_result=retrieval,
                                deadline=deadline,
                            )
                        )
                        active_stage_task = retrieval_task
                        async for heartbeat in wait_for_stage_task(
                            retrieval_task,
                            stage=current_stage,
                            started=stage_started,
                        ):
                            yield heartbeat
                        retrieval = await retrieval_task
                        active_stage_task = None
                        sequence += 1
                        yield stage_notice("stage.completed", current_stage, _elapsed_ms(stage_started))
                        last_successful_stage = "extended_search"
            current_stage = "answer_generation"
            sequence += 1
            yield stage_notice("stage.started", current_stage)
            stage_started = time.perf_counter()
            if await request.is_disconnected():
                raise asyncio.CancelledError
            generation_task = asyncio.create_task(
                generate_answer(
                    effective_message,
                    retrieval,
                    settings,
                    active_profile,
                    deadline=deadline,
                    correlation_id=str(query_run_id),
                )
            )
            active_stage_task = generation_task
            while True:
                try:
                    answer, validation = await asyncio.wait_for(
                        asyncio.shield(generation_task),
                        timeout=deadline.timeout_seconds(
                            settings.operation_heartbeat_seconds,
                            stage=current_stage,
                        ),
                    )
                    break
                except TimeoutError:
                    if generation_task.done():
                        answer, validation = await generation_task
                        break
                    if await request.is_disconnected():
                        generation_task.cancel()
                        await asyncio.gather(generation_task, return_exceptions=True)
                        raise asyncio.CancelledError from None
                    deadline.ensure_remaining(stage=current_stage)
                    sequence += 1
                    yield stage_notice("stage.heartbeat", current_stage, _elapsed_ms(stage_started))
                except OperationDeadlineExceeded:
                    generation_task.cancel()
                    await asyncio.gather(generation_task, return_exceptions=True)
                    active_stage_task = None
                    raise
            active_stage_task = None
            sequence += 1
            yield stage_notice("stage.completed", current_stage, _elapsed_ms(stage_started))
            last_successful_stage = "answer_generation"
            timings_ms = _combined_timings(retrieval.model_dump(), validation)
            answer_artifact = build_answer_artifact(
                query_run_id=str(query_run_id),
                knowledge_base_id=primary_kb_id,
                search_plan=actual_search_plan,
                retrieval=retrieval,
                validation=cast(dict[str, Any], validation),
                timings_ms=timings_ms,
                answer_present=bool(answer),
            )
            sequence += 1
            yield _event(
                SseEvent(
                    event="message.delta",
                    request_id=request_id,
                    query_run_id=str(query_run_id),
                    sequence=sequence,
                    data={
                        "text": answer,
                        "evidence": [item.model_dump() for item in retrieval.evidence],
                        "conversation_id": conversation_id,
                        "ambiguity_mode": payload.ambiguity_mode,
                        "answer_mode": validation.get("answer_mode", "single"),
                        "interpretations": validation.get("interpretations", []),
                        "clarification_question": validation.get("clarification_question"),
                        "status": validation.get("status"),
                        "model_output_contract_abstained": bool(validation.get("model_output_contract_abstained")),
                        "model_output_contract_reason": validation.get("model_output_contract_reason"),
                    },
                )
            )
            sequence += 1
            yield _event(
                SseEvent(
                    event="usage.updated",
                    request_id=request_id,
                    query_run_id=str(query_run_id),
                    sequence=sequence,
                    data={
                        "retrieval": retrieval.model_dump(),
                        "citation_validation": validation,
                        "timings_ms": timings_ms,
                        "search_plan": actual_search_plan,
                        "root_cause": answer_artifact["root_cause"],
                        "answer_artifact": answer_artifact,
                        "conversation_id": conversation_id,
                        "ambiguity_mode": payload.ambiguity_mode,
                    },
                )
            )
            async with connect() as conn:
                current_stage = "query_run_complete"
                await _insert_answer_events(
                    conn,
                    tenant_id=tenant_id,
                    query_run_id=str(query_run_id),
                    trace_id=trace_id,
                    retrieval=retrieval,
                    answer=answer,
                    validation=cast(dict[str, Any], validation),
                    timings_ms=timings_ms,
                    answer_artifact=answer_artifact,
                )
                await complete_query_run(
                    conn,
                    query_run_id=str(query_run_id),
                    answer=answer,
                    usage={
                        "citations": validation.get("citations", []),
                        "generation_usage": validation.get("usage", {}),
                        "provider": validation.get("provider"),
                        "provider_cost": validation.get("provider_cost"),
                        "model_alias": validation.get("model_alias"),
                        "model_gateway": validation.get("model_gateway", {}),
                        "timings_ms": timings_ms,
                        "index_contract_id": retrieval.index_contract_id,
                        "run_contract_id": retrieval.run_contract_id,
                        "knowledge_base_ids": kb_ids,
                        "search_plan": actual_search_plan,
                        "root_cause": answer_artifact["root_cause"],
                        "conversation_id": conversation_id,
                        "ambiguity_mode": payload.ambiguity_mode,
                        "answer_mode": validation.get("answer_mode", "single"),
                        "interpretations": validation.get("interpretations", []),
                        "clarification_question": validation.get("clarification_question"),
                        "status": validation.get("status"),
                        "model_output_contract_abstained": bool(validation.get("model_output_contract_abstained")),
                        "model_output_contract_reason": validation.get("model_output_contract_reason"),
                        "answer_artifact": answer_artifact,
                    },
                    model_alias=str(validation.get("model_alias") or ""),
                    provider_request_id=str(validation.get("provider_request_id") or ""),
                )
                if idempotency_record is not None:
                    await complete_idempotency_record(
                        conn,
                        record_id=str(idempotency_record["id"]),
                        resource_id=str(query_run_id),
                        response_status=200,
                        safe_response={"query_run_id": str(query_run_id), "terminal_status": "completed"},
                    )
                last_successful_stage = "query_run_complete"
            sequence += 1
            yield _event(
                SseEvent(
                    event="run.completed",
                    request_id=request_id,
                    query_run_id=str(query_run_id),
                    sequence=sequence,
                    data={
                        "answer": answer,
                        "root_cause": answer_artifact["root_cause"],
                        "stage": "completed",
                        "attempt": 1,
                        "elapsed_ms": _elapsed_ms(run_started),
                        "deadline_remaining_ms": deadline.remaining_ms(),
                    },
                )
            )
        except asyncio.CancelledError:
            task_failure: Exception | None = None
            if active_stage_task is not None:
                if active_stage_task.done() and not active_stage_task.cancelled():
                    try:
                        active_stage_task.result()
                    except Exception as exc:  # noqa: BLE001 - persist the already-terminal safe failure.
                        task_failure = exc
                elif not active_stage_task.done():
                    active_stage_task.cancel()

            async def persist_terminal_state() -> None:
                async with connect() as conn:
                    if task_failure is not None:
                        await insert_retrieval_event(
                            conn,
                            tenant_id=tenant_id,
                            query_run_id=str(query_run_id),
                            trace_id=trace_id,
                            event_type="answer_stage" if current_stage == "answer_generation" else "query_stage",
                            stage=current_stage,
                            payload=_failure_stage_event(
                                task_failure,
                                stage=current_stage,
                                last_successful_stage=last_successful_stage,
                                retrieval=retrieval,
                            ),
                        )
                        await fail_query_run(
                            conn,
                            query_run_id=str(query_run_id),
                            error_code=safe_error_code(task_failure),
                        )
                    else:
                        await cancel_query_run(
                            conn,
                            query_run_id=str(query_run_id),
                            error_code="CLIENT_DISCONNECTED",
                        )
                    if idempotency_record is not None:
                        await complete_idempotency_record(
                            conn,
                            record_id=str(idempotency_record["id"]),
                            resource_id=str(query_run_id),
                            response_status=500 if task_failure is not None else 499,
                            safe_response={
                                "query_run_id": str(query_run_id),
                                "terminal_status": "failed" if task_failure is not None else "cancelled",
                                **({"error_code": safe_error_code(task_failure)} if task_failure is not None else {}),
                            },
                        )

            try:
                await asyncio.shield(persist_terminal_state())
            except asyncio.CancelledError:
                # The shielded persistence task continues after ASGI has
                # cancelled the stream; re-raise the original disconnect.
                pass
            raise
        except Exception as exc:
            async with connect() as conn:
                await insert_retrieval_event(
                    conn,
                    tenant_id=tenant_id,
                    query_run_id=str(query_run_id),
                    trace_id=trace_id,
                    event_type="answer_stage" if current_stage == "answer_generation" else "query_stage",
                    stage=current_stage,
                    payload=_failure_stage_event(
                        exc,
                        stage=current_stage,
                        last_successful_stage=last_successful_stage,
                        retrieval=retrieval,
                    ),
                )
                await fail_query_run(conn, query_run_id=str(query_run_id), error_code=safe_error_code(exc))
                if idempotency_record is not None:
                    await complete_idempotency_record(
                        conn,
                        record_id=str(idempotency_record["id"]),
                        resource_id=str(query_run_id),
                        response_status=500,
                        safe_response={
                            "query_run_id": str(query_run_id),
                            "terminal_status": "failed",
                            "error_code": safe_error_code(exc),
                        },
                    )
            sequence += 1
            failure = _safe_failure_payload(
                exc,
                stage=current_stage,
                last_successful_stage=last_successful_stage,
                trace_id=trace_id,
                request_id=request_id,
                elapsed_ms=_elapsed_ms(run_started),
                retrieval=retrieval,
                query_run_id=str(query_run_id),
                knowledge_base_id=primary_kb_id,
                search_plan=actual_search_plan,
            )
            yield _event(
                SseEvent(
                    event="run.failed",
                    request_id=request_id,
                    query_run_id=str(query_run_id),
                    sequence=sequence,
                    data=failure,
                )
            )
        finally:
            if active_stage_task is not None:
                if not active_stage_task.done():
                    active_stage_task.cancel()
                await asyncio.gather(active_stage_task, return_exceptions=True)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _replay_query_run_stream(*, request_id: str, query_run: dict[str, Any]) -> StreamingResponse:
    """Replay a terminal chat run without repeating retrieval or model generation."""
    query_run_id = str(query_run["id"])
    trace_id = str(query_run.get("trace_id") or "")
    terminal_status = str(query_run.get("status") or "failed")
    stored_usage = dict(query_run.get("usage") or {})

    async def event_stream() -> AsyncIterator[str]:
        sequence = 1
        yield _event(
            SseEvent(
                event="run.started",
                request_id=request_id,
                query_run_id=query_run_id,
                sequence=sequence,
                data={"trace_id": trace_id, "stage": "replayed", "elapsed_ms": 0, "replayed": True},
            )
        )
        sequence += 1
        if terminal_status == "completed":
            answer = str(query_run.get("answer") or "")
            yield _event(
                SseEvent(
                    event="message.delta",
                    request_id=request_id,
                    query_run_id=query_run_id,
                    sequence=sequence,
                    data={
                        "text": answer,
                        "stage": "answer_generation",
                        "elapsed_ms": 0,
                        "replayed": True,
                        "conversation_id": stored_usage.get("conversation_id"),
                        "ambiguity_mode": stored_usage.get("ambiguity_mode", "auto"),
                        "answer_mode": stored_usage.get("answer_mode", "single"),
                        "interpretations": stored_usage.get("interpretations", []),
                        "clarification_question": stored_usage.get("clarification_question"),
                    },
                )
            )
            sequence += 1
            yield _event(
                SseEvent(
                    event="run.completed",
                    request_id=request_id,
                    query_run_id=query_run_id,
                    sequence=sequence,
                    data={
                        "answer": answer,
                        "stage": "completed",
                        "elapsed_ms": 0,
                        "replayed": True,
                        "conversation_id": stored_usage.get("conversation_id"),
                        "ambiguity_mode": stored_usage.get("ambiguity_mode", "auto"),
                        "answer_mode": stored_usage.get("answer_mode", "single"),
                        "interpretations": stored_usage.get("interpretations", []),
                        "clarification_question": stored_usage.get("clarification_question"),
                    },
                )
            )
            return
        event_name = "run.cancelled" if terminal_status == "cancelled" else "run.failed"
        yield _event(
            SseEvent(
                event=event_name,
                request_id=request_id,
                query_run_id=query_run_id,
                sequence=sequence,
                data={
                    "code": str(query_run.get("error_code") or terminal_status.upper()),
                    "stage": "replayed_terminal",
                    "elapsed_ms": 0,
                    "retryable": False,
                    "replayed": True,
                },
            )
        )

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def query_run_retrieval(query_run_id: str, request: Request) -> dict[str, Any]:
    """Return stored retrieval events for an editor-authorized query run."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        run = await _load_query_run_for_actor(conn, tenant_id=tenant_id, query_run_id=query_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="query run not found")
        for kb_id in _query_run_kb_scope(run):
            await _require_workspace_kb_write(conn, actor, kb_id)
        events = await load_retrieval_events(conn, tenant_id, query_run_id)
    return {"query_run_id": query_run_id, "run": _query_run_summary(run), "events": _jsonable(events)}


async def query_run_feedback(query_run_id: str, payload: QueryRunFeedbackRequest, request: Request) -> dict[str, Any]:
    """Record sanitized user feedback for an editor-authorized query run."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    settings = get_settings()
    async with connect() as conn:
        run = await _load_query_run_for_actor(conn, tenant_id=tenant_id, query_run_id=query_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="query run not found")
        for kb_id in _query_run_kb_scope(run):
            await _require_workspace_kb_write(conn, actor, kb_id)
        event_payload = {
            "stage": "feedback",
            "rating": payload.rating,
            "feedback": safe_telemetry_payload(payload.model_dump(mode="json"), settings=settings),
        }
        await insert_retrieval_event(
            conn,
            tenant_id=tenant_id,
            query_run_id=query_run_id,
            trace_id=str(run["trace_id"]),
            event_type="feedback",
            stage="feedback",
            payload=event_payload,
        )
    return {"query_run_id": query_run_id, "status": "recorded"}


async def query_run_evaluation(
    query_run_id: str, payload: QueryRunEvaluationRequest, request: Request
) -> dict[str, Any]:
    """Record sanitized evaluation metadata for an editor-authorized query run."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    settings = get_settings()
    async with connect() as conn:
        run = await _load_query_run_for_actor(conn, tenant_id=tenant_id, query_run_id=query_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="query run not found")
        for kb_id in _query_run_kb_scope(run):
            await _require_workspace_kb_write(conn, actor, kb_id)
        event_payload = {
            "stage": "evaluation",
            "evaluator": payload.evaluator,
            "scores": payload.scores,
            "reason_codes": payload.reason_codes,
            "evaluation": safe_telemetry_payload(payload.model_dump(mode="json"), settings=settings),
        }
        await insert_retrieval_event(
            conn,
            tenant_id=tenant_id,
            query_run_id=query_run_id,
            trace_id=str(run["trace_id"]),
            event_type="evaluation",
            stage="evaluation",
            payload=event_payload,
        )
    return {"query_run_id": query_run_id, "status": "recorded"}


def _plan_question_records(topic: str, questions: list[ResearchPlanQuestion] | None) -> list[dict[str, Any]]:
    if questions:
        ordered = sorted(questions, key=lambda item: item.ordinal)
        return [
            {"question": item.question, "ordinal": index, "kind": item.kind}
            for index, item in enumerate(ordered, start=1)
        ]
    return [
        {"question": question, "ordinal": index, "kind": "primary" if index == 1 else "decomposition"}
        for index, question in enumerate(build_research_questions(topic), start=1)
    ]


def _research_plan_scope_ids(knowledge_base_ids: Any, knowledge_base_id: str) -> list[str]:
    scope_ids = [str(item) for item in list(knowledge_base_ids or []) if str(item)]
    if knowledge_base_id not in scope_ids:
        scope_ids.insert(0, knowledge_base_id)
    return scope_ids[:3]


def _research_plan_summary(row: dict[str, Any]) -> dict[str, Any]:
    questions = list(row.get("questions") or [])
    return {
        "id": str(row["id"]),
        "knowledge_base_id": str(row["knowledge_base_id"]),
        "knowledge_base_ids": [str(item) for item in list(row.get("knowledge_base_ids") or []) if str(item)],
        "user_id": _optional_uuid(row, "user_id"),
        "topic": str(row.get("topic") or ""),
        "retrieval_profile": str(row.get("retrieval_profile") or ""),
        "tool_mode": str(row.get("tool_mode") or DEFAULT_RESEARCH_TOOL_MODE),
        "status": str(row.get("status") or ResearchPlanStatus.draft.value),
        "notes": str(row.get("notes") or ""),
        "question_count": len(questions),
        "approved_run_id": _optional_uuid(row, "approved_run_id"),
        "approved_at": row.get("approved_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _research_plan_detail(row: dict[str, Any]) -> dict[str, Any]:
    questions = []
    for index, item in enumerate(list(row.get("questions") or []), start=1):
        if not isinstance(item, dict):
            continue
        questions.append(
            {
                "question": str(item.get("question") or ""),
                "ordinal": int(item.get("ordinal") or index),
                "kind": str(item.get("kind") or "primary"),
            }
        )
    return {
        "plan": _research_plan_summary(row),
        "questions": questions,
        "retrieval_overrides": dict(row.get("retrieval_overrides") or {}),
        "context_policy": dict(row.get("context_policy") or {}),
    }


async def create_research_plan_endpoint(
    payload: ResearchPlanCreate,
    request: Request,
) -> ResearchPlanActionResponse:
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    settings = get_settings()
    kb_id = payload.knowledge_base_id or settings.default_kb_id
    kb_ids = _research_plan_scope_ids(payload.knowledge_base_ids, kb_id)
    try:
        async with connect() as conn:
            for scoped_kb_id in kb_ids:
                await _require_workspace_kb_read(conn, actor, scoped_kb_id)
            profile = await resolve_retrieval_profile(
                conn,
                tenant_id=tenant_id,
                knowledge_base_ids=kb_ids,
                requested=normalize_retrieval_profile_request(payload.retrieval_profile),
                overrides=payload.retrieval_overrides,
                settings=settings,
            )
            plan_id = await create_research_plan(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                knowledge_base_ids=kb_ids,
                user_id=actor.user_id,
                topic=payload.topic,
                retrieval_profile=profile.name,
                tool_mode=payload.tool_mode,
                retrieval_overrides=payload.retrieval_overrides,
                context_policy=context_policy_for_profile(profile, payload.context_policy_override),
                questions=_plan_question_records(payload.topic, payload.questions),
                notes=payload.notes,
            )
    except KnowledgeBaseNotReady as exc:
        raise _kb_not_ready_http(exc, actor.request_id) from exc
    except RetrievalProfileIncompatible as exc:
        raise _retrieval_profile_incompatible_http(exc, actor.request_id) from exc
    except (KeyError, ValueError) as exc:
        raise _unknown_retrieval_profile_http(exc, actor.request_id) from exc
    return ResearchPlanActionResponse(plan_id=str(plan_id), status=ResearchPlanStatus.draft)


async def list_research_plans_endpoint(
    request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> ResearchPlanListResponse:
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        rows = await list_research_plans(conn, tenant_id=tenant_id, limit=limit)
        repository = WorkspaceGrantRepository(conn)
        visible: list[dict[str, Any]] = []
        for row in rows:
            resource = await repository.load_knowledge_base(str(row["knowledge_base_id"]))
            if resource is None:
                continue
            read, _write, _share, _delete = await repository.authorize(
                user_id=actor.user_id,
                platform_role=WorkspacePlatformRole(actor.platform_role.value),
                resource=resource,
            )
            if read:
                visible.append(_research_plan_summary(row))
    return ResearchPlanListResponse.model_validate({"plans": _jsonable(visible)})


async def get_research_plan_endpoint(research_plan_id: str, request: Request) -> ResearchPlanDetail:
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        row = await get_research_plan(conn, tenant_id=tenant_id, research_plan_id=research_plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail="research plan not found")
        repository = WorkspaceGrantRepository(conn)
        resource = await repository.load_knowledge_base(str(row["knowledge_base_id"]))
        read = False
        if resource is not None:
            read, _write, _share, _delete = await repository.authorize(
                user_id=actor.user_id,
                platform_role=WorkspacePlatformRole(actor.platform_role.value),
                resource=resource,
            )
        if not read:
            raise HTTPException(status_code=404, detail="research plan not found")
    return ResearchPlanDetail.model_validate(_jsonable(_research_plan_detail(row)))


async def patch_research_plan_endpoint(
    research_plan_id: str,
    payload: ResearchPlanPatch,
    request: Request,
) -> ResearchPlanActionResponse:
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    settings = get_settings()
    async with connect() as conn:
        row = await get_research_plan(conn, tenant_id=tenant_id, research_plan_id=research_plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail="research plan not found")
        if str(row.get("status") or "") != ResearchPlanStatus.draft.value:
            raise HTTPException(status_code=409, detail="research plan is not editable")
        is_creator = str(row.get("user_id") or "") == actor.user_id
        if not is_creator:
            raise HTTPException(status_code=403, detail="research plan can only be edited by the creator")
        next_kb_id = str(payload.knowledge_base_id or row["knowledge_base_id"])
        next_scope_ids = _research_plan_scope_ids(
            payload.knowledge_base_ids or row.get("knowledge_base_ids"),
            next_kb_id,
        )
        for scoped_kb_id in next_scope_ids:
            await _require_workspace_kb_read(conn, actor, scoped_kb_id)
        profile = get_retrieval_profile(
            payload.retrieval_profile or str(row.get("retrieval_profile") or settings.retrieval_profile),
            settings,
            (
                payload.retrieval_overrides
                if payload.retrieval_overrides is not None
                else dict(row.get("retrieval_overrides") or {})
            ),
        )
        context_policy = (
            context_policy_for_profile(profile, payload.context_policy_override)
            if payload.context_policy_override is not None
            else dict(row.get("context_policy") or {})
        )
        topic = payload.topic or str(row.get("topic") or "")
        questions = (
            _plan_question_records(topic, payload.questions)
            if payload.questions is not None
            else list(row.get("questions") or [])
        )
        await update_research_plan(
            conn,
            research_plan_id=research_plan_id,
            topic=payload.topic,
            knowledge_base_id=payload.knowledge_base_id,
            knowledge_base_ids=(
                next_scope_ids
                if payload.knowledge_base_ids is not None or payload.knowledge_base_id is not None
                else None
            ),
            retrieval_profile=(
                profile.name
                if payload.retrieval_profile is not None or payload.retrieval_overrides is not None
                else None
            ),
            tool_mode=payload.tool_mode,
            retrieval_overrides=payload.retrieval_overrides,
            context_policy=context_policy,
            questions=questions,
            notes=payload.notes,
        )
    return ResearchPlanActionResponse(plan_id=research_plan_id, status=ResearchPlanStatus.draft)


async def approve_research_plan_endpoint(research_plan_id: str, request: Request) -> ResearchPlanActionResponse:
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    settings = get_settings()
    async with connect() as conn:
        row = await get_research_plan(conn, tenant_id=tenant_id, research_plan_id=research_plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail="research plan not found")
        if str(row.get("status") or "") != ResearchPlanStatus.draft.value:
            raise HTTPException(status_code=409, detail="research plan is already approved")
        kb_id = str(row["knowledge_base_id"])
        kb_ids = _research_plan_scope_ids(row.get("knowledge_base_ids"), kb_id)
        for scoped_kb_id in kb_ids:
            await _require_workspace_kb_read(conn, actor, scoped_kb_id)
        profile = get_retrieval_profile(
            str(row.get("retrieval_profile") or settings.retrieval_profile),
            settings,
            dict(row.get("retrieval_overrides") or {}),
        )
        for scoped_kb_id in kb_ids:
            await validate_active_retrieval_contract(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=scoped_kb_id,
                profile=profile,
                retrieval_overrides=dict(row.get("retrieval_overrides") or {}),
                settings=settings,
            )
        question_rows = [
            str(item.get("question") or "") for item in list(row.get("questions") or []) if isinstance(item, dict)
        ]
        run_id, job_id = await create_research_run(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            knowledge_base_ids=kb_ids,
            user_id=actor.user_id,
            topic=str(row.get("topic") or ""),
            retrieval_profile=profile.name,
            tool_mode=str(row.get("tool_mode") or DEFAULT_RESEARCH_TOOL_MODE),
            retrieval_overrides=dict(row.get("retrieval_overrides") or {}),
            context_policy=dict(row.get("context_policy") or {}),
            questions=question_rows or build_research_questions(str(row.get("topic") or "")),
            research_plan_id=research_plan_id,
        )
        await approve_research_plan(
            conn,
            tenant_id=tenant_id,
            research_plan_id=research_plan_id,
            approved_by_user_id=actor.user_id,
            run_id=str(run_id),
        )
    return ResearchPlanActionResponse(
        plan_id=research_plan_id,
        status=ResearchPlanStatus.approved,
        run_id=str(run_id),
    )


async def create_research_run_endpoint(payload: ResearchRunCreate, request: Request) -> ResearchRunActionResponse:
    """Create a durable Deep Research run and enqueue worker execution."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    settings = get_settings()
    request_id = _request_id(request)
    try:
        async with connect() as conn:
            plan = None
            if payload.research_plan_id:
                plan = await get_research_plan(conn, tenant_id=tenant_id, research_plan_id=payload.research_plan_id)
                if plan is None:
                    raise HTTPException(status_code=404, detail="research plan not found")
                if str(plan.get("status") or "") != ResearchPlanStatus.approved.value:
                    raise HTTPException(status_code=409, detail="research plan must be approved before run creation")
            kb_ids = (
                _research_plan_scope_ids(plan.get("knowledge_base_ids"), str(plan["knowledge_base_id"]))
                if plan is not None
                else _research_plan_scope_ids(
                    payload.knowledge_base_ids,
                    payload.knowledge_base_id or settings.default_kb_id,
                )
            )
            kb_id = kb_ids[0]
            retrieval_overrides = (
                dict(plan.get("retrieval_overrides") or {}) if plan is not None else payload.retrieval_overrides
            )
            requested_profile = (
                str(plan.get("retrieval_profile") or "") if plan is not None else payload.retrieval_profile
            ) or None
            requested_profile = normalize_retrieval_profile_request(requested_profile)
            profile = await resolve_retrieval_profile(
                conn,
                tenant_id=tenant_id,
                knowledge_base_ids=kb_ids,
                requested=requested_profile,
                overrides=retrieval_overrides,
                settings=settings,
            )
            tool_mode = (
                str(plan.get("tool_mode") or DEFAULT_RESEARCH_TOOL_MODE) if plan is not None else payload.tool_mode
            )
            topic = str(plan.get("topic") or payload.topic) if plan is not None else payload.topic
            context_policy = (
                dict(plan.get("context_policy") or {})
                if plan is not None
                else context_policy_for_profile(profile, payload.context_policy_override)
            )
            questions = (
                [
                    str(item.get("question") or "")
                    for item in list(plan.get("questions") or [])
                    if isinstance(item, dict)
                ]
                if plan is not None
                else build_research_questions(payload.topic)
            )
            for scoped_kb_id in kb_ids:
                await _require_workspace_kb_read(conn, actor, scoped_kb_id)
            idempotency_record, owns_idempotency_record = await _claim_operation_idempotency(
                request=request,
                actor=actor,
                tenant_id=tenant_id,
                route="POST:/api/v1/research-runs",
                payload=payload.model_dump(mode="json"),
                settings=settings,
                idempotency_key=(
                    stable_hash(["research_client_request", payload.client_request_id], 64)
                    if payload.client_request_id
                    else None
                ),
            )
            if not owns_idempotency_record:
                safe_response = dict((idempotency_record or {}).get("safe_response") or {})
                if not safe_response:
                    raise HTTPException(status_code=409, detail="idempotent research run record is missing response")
                return ResearchRunActionResponse.model_validate(safe_response)
            run_id, job_id = await create_research_run(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                knowledge_base_ids=kb_ids,
                user_id=actor.user_id,
                topic=topic,
                retrieval_profile=profile.name,
                tool_mode=tool_mode,
                retrieval_overrides=retrieval_overrides,
                context_policy=context_policy,
                questions=questions,
                research_plan_id=payload.research_plan_id,
            )
            await insert_audit_event(
                conn,
                actor=actor,
                tenant_id=tenant_id,
                action="research_run.created",
                target_type="research_run",
                target_id=str(run_id),
                outcome="success",
                metadata={
                    "knowledge_base_id": kb_id,
                    "knowledge_base_ids": kb_ids,
                    "job_id": str(job_id),
                    "tool_mode": tool_mode,
                    "research_plan_id": payload.research_plan_id,
                },
            )
            response = ResearchRunActionResponse(
                run_id=str(run_id),
                status=ResearchRunStatus.received,
                job_id=str(job_id),
            )
            if idempotency_record is not None:
                await complete_idempotency_record(
                    conn,
                    record_id=str(idempotency_record["id"]),
                    resource_id=str(job_id),
                    response_status=202,
                    safe_response=response.model_dump(mode="json"),
                )
    except KnowledgeBaseNotReady as exc:
        raise _kb_not_ready_http(exc, request_id) from exc
    except RetrievalProfileIncompatible as exc:
        raise _retrieval_profile_incompatible_http(exc, request_id) from exc
    except (KeyError, ValueError) as exc:
        raise _unknown_retrieval_profile_http(exc, request_id) from exc
    return response


async def list_research_runs_endpoint(
    request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> ResearchRunListResponse:
    """List research runs in the active tenant that are visible to the actor."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        rows = await list_research_runs(conn, tenant_id=tenant_id, limit=limit)
        repository = WorkspaceGrantRepository(conn)
        visible: list[dict[str, Any]] = []
        for row in rows:
            scope_rows = await load_research_run_scopes(
                conn,
                tenant_id=tenant_id,
                research_run_id=str(row["id"]),
            )
            resource = await repository.load_knowledge_base(str(row["knowledge_base_id"]))
            if resource is None:
                continue
            read, _write, _share, _delete = await repository.authorize(
                user_id=actor.user_id,
                platform_role=WorkspacePlatformRole(actor.platform_role.value),
                resource=resource,
            )
            if read:
                visible.append(_research_run_summary(row, knowledge_base_ids=_research_scope_ids(scope_rows)))
    return ResearchRunListResponse.model_validate({"runs": _jsonable(visible)})


async def get_research_run_endpoint(research_run_id: str, request: Request) -> ResearchRunDetail:
    """Return a safe current-ACL view of a durable Deep Research run."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        run = await _load_research_run_or_404(conn, tenant_id=tenant_id, research_run_id=research_run_id)
        await _authorize_research_run(conn, actor=actor, tenant_id=tenant_id, run=run, action="read")
        scope_rows = await load_research_run_scopes(conn, tenant_id=tenant_id, research_run_id=research_run_id)
        scope_ids = _research_scope_ids(scope_rows) or [str(run["knowledge_base_id"])]
        records = await load_research_detail_records(conn, tenant_id=tenant_id, research_run_id=research_run_id)
        records = {
            **records,
            "evidence": await _reauthorize_research_evidence(conn, records["evidence"], actor=actor),
        }
    detail = _research_detail(run, records=records, knowledge_base_ids=scope_ids)
    return ResearchRunDetail.model_validate(_jsonable(detail))


async def research_run_events(research_run_id: str, request: Request) -> dict[str, Any]:
    """Return compact lifecycle events for a research run without raw source text."""
    detail = await get_research_run_endpoint(research_run_id, request)
    return {
        "run_id": research_run_id,
        "episodes": [item.model_dump(mode="json") for item in detail.episodes],
        "tool_calls": [item.model_dump(mode="json") for item in detail.tool_calls],
        "decisions": [item.model_dump(mode="json") for item in detail.decisions],
        "relations": [item.model_dump(mode="json") for item in detail.relations],
        "coverage": [item.model_dump(mode="json") for item in detail.coverage],
        "reflections": [item.model_dump(mode="json") for item in detail.reflections],
    }


async def pause_research_run(research_run_id: str, request: Request) -> ResearchRunActionResponse:
    """Request pause; worker stops at the next episode boundary."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        run = await _load_research_run_or_404(conn, tenant_id=tenant_id, research_run_id=research_run_id)
        await _authorize_research_run(conn, actor=actor, tenant_id=tenant_id, run=run, action="control")
        if str(run["status"]) in {"completed", "failed", "cancelled"}:
            raise HTTPException(status_code=409, detail="research run is already terminal")
        await request_research_pause(conn, tenant_id=tenant_id, research_run_id=research_run_id)
    return ResearchRunActionResponse(
        run_id=research_run_id,
        status="pause_requested",
        job_id=_optional_uuid(run, "active_job_id"),
    )


async def resume_research_run(research_run_id: str, request: Request) -> ResearchRunActionResponse:
    """Resume a paused or failed research run by enqueueing a fresh worker job."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        run = await _load_research_run_or_404(conn, tenant_id=tenant_id, research_run_id=research_run_id)
        await _authorize_research_run(conn, actor=actor, tenant_id=tenant_id, run=run, action="control")
        if str(run["status"]) in {"completed", "cancelled"}:
            raise HTTPException(status_code=409, detail="research run cannot be resumed")
        scope_rows = await load_research_run_scopes(conn, tenant_id=tenant_id, research_run_id=research_run_id)
        job_id = await create_research_resume_job(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=str(run["knowledge_base_id"]),
            research_run_id=research_run_id,
            knowledge_base_ids=_research_scope_ids(scope_rows) or [str(run["knowledge_base_id"])],
            tool_mode=str(run.get("tool_mode") or DEFAULT_RESEARCH_TOOL_MODE),
        )
    return ResearchRunActionResponse(run_id=research_run_id, status=ResearchRunStatus.received, job_id=str(job_id))


async def cancel_research_run(research_run_id: str, request: Request) -> ResearchRunActionResponse:
    """Request cancellation for a durable research run."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        run = await _load_research_run_or_404(conn, tenant_id=tenant_id, research_run_id=research_run_id)
        await _authorize_research_run(conn, actor=actor, tenant_id=tenant_id, run=run, action="control")
        await request_research_cancel(conn, tenant_id=tenant_id, research_run_id=research_run_id)
    return ResearchRunActionResponse(
        run_id=research_run_id,
        status="cancel_requested",
        job_id=_optional_uuid(run, "active_job_id"),
    )


async def run_debug_search(payload: DebugSearchRequest, request: Request) -> dict[str, Any]:
    """Run editor-only retrieval debugging and persist its diagnostic query run."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    request_id = str(uuid.uuid4())
    trace_id = stable_hash([request_id, payload.message], 32)
    profile = get_retrieval_profile(payload.retrieval_profile, settings, payload.retrieval_overrides)
    kb_ids = _kb_scope_ids(payload.knowledge_base_ids, settings.default_kb_id)
    primary_kb_id = kb_ids[0]
    route_decision = {
        "route": "direct_retrieval",
        "reason": "search_debug_endpoint",
    }
    search_plan = build_search_plan(
        query=payload.message,
        mode="debug",
        route=route_decision["route"],
        route_reason=route_decision["reason"],
        knowledge_base_id=primary_kb_id,
        knowledge_base_ids=kb_ids,
        trace_id=trace_id,
        profile=profile,
        top_k=payload.top_k,
        include_generation=False,
    )
    query_run_id: str | None = None
    async with connect() as conn:
        storage_tenants = [await _require_workspace_kb_write(conn, actor, kb_id) for kb_id in kb_ids]
        if len(set(storage_tenants)) != 1:
            raise HTTPException(status_code=409, detail="knowledge bases are not yet in one workspace storage scope")
        tenant_id = storage_tenants[0]
        profile = await resolve_retrieval_profile(
            conn,
            tenant_id=tenant_id,
            knowledge_base_ids=kb_ids,
            requested=payload.retrieval_profile,
            overrides=payload.retrieval_overrides,
            settings=settings,
        )
        search_plan = build_search_plan(
            query=payload.message,
            mode="debug",
            route=route_decision["route"],
            route_reason=route_decision["reason"],
            knowledge_base_id=primary_kb_id,
            knowledge_base_ids=kb_ids,
            trace_id=trace_id,
            profile=profile,
            top_k=payload.top_k,
            include_generation=False,
        )
        created = await create_query_run(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=primary_kb_id,
            user_id=actor.user_id,
            request_id=request_id,
            client_request_id=None,
            mode="debug",
            input_text=payload.message,
            trace_id=trace_id,
            usage=_initial_query_run_usage(
                mode="debug",
                profile=profile,
                retrieval_overrides=payload.retrieval_overrides,
                knowledge_base_ids=kb_ids,
                route_decision=route_decision,
                trace_id=trace_id,
                settings=settings,
            ),
        )
        query_run_id = str(created)
        await insert_retrieval_event(
            conn,
            tenant_id=tenant_id,
            query_run_id=query_run_id,
            trace_id=trace_id,
            event_type="query_stage",
            stage="path_selected",
            payload=_path_selection_event(
                mode="debug",
                route_decision=route_decision,
                knowledge_base_ids=kb_ids,
                profile=profile,
                search_plan=search_plan,
                retrieval_overrides=payload.retrieval_overrides,
            ),
        )
        try:
            result = await retrieve_multi(
                conn,
                payload.message,
                tenant_id=tenant_id,
                knowledge_base_ids=kb_ids,
                query_run_id=query_run_id,
                trace_id=trace_id,
                settings=settings,
                top_k=payload.top_k,
                profile=profile,
                profile_overrides=payload.retrieval_overrides,
            )
        except KnowledgeBaseNotReady as exc:
            await fail_query_run(conn, query_run_id=query_run_id, error_code=exc.code)
            raise _kb_not_ready_http(exc, trace_id) from exc
        except Exception as exc:
            await fail_query_run(conn, query_run_id=query_run_id, error_code=safe_error_code(exc))
            raise
    output = result.model_dump()
    answer_artifact = build_answer_artifact(
        query_run_id=query_run_id,
        knowledge_base_id=primary_kb_id,
        search_plan=search_plan,
        retrieval=result,
        validation=None,
        timings_ms=_combined_timings(output, {}),
        answer_present=False,
    )
    output["search_plan"] = search_plan
    output["root_cause"] = answer_artifact["root_cause"]
    output["answer_artifact"] = answer_artifact
    output["query_run_id"] = query_run_id
    async with connect() as conn:
        await complete_query_run(
            conn,
            query_run_id=query_run_id,
            answer="",
            usage={
                "citations": [],
                "timings_ms": _combined_timings(output, {}),
                "index_contract_id": result.index_contract_id,
                "run_contract_id": result.run_contract_id,
                "knowledge_base_ids": kb_ids,
                "search_plan": search_plan,
                "root_cause": answer_artifact["root_cause"],
                "answer_artifact": answer_artifact,
            },
        )
    return output


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _event(event: SseEvent) -> str:
    return _sse(event.event, event.model_dump(mode="json"))


async def _load_actor(request: Request) -> ActorContext | None:
    """Resolve the server-owned actor from disabled-auth, test-auth, or session-cookie state."""
    settings = get_settings()
    request_id = _request_id(request)
    trace_id = request.headers.get("x-trace-id", request_id)[:128]
    if settings.auth_disabled:
        async with connect() as conn:
            user = await load_bootstrap_admin_user(conn, settings)
        user_id = user.user_id if user is not None else settings.default_user_id
        return auth_disabled_actor(settings, user_id=user_id, request_id=request_id, trace_id=trace_id)
    if settings.auth_mode == "test":
        return test_actor_context(settings, request_id=request_id, trace_id=trace_id)
    session_token = request.cookies.get(settings.session_cookie_name)
    if not session_token:
        return None
    async with connect() as conn:
        return await load_actor_for_session(
            conn,
            session_token=session_token,
            request_id=request_id,
            trace_id=trace_id,
        )


async def _require_actor(request: Request) -> ActorContext:
    """Require authentication, CSRF for mutating requests, and password-change gating."""
    settings = get_settings()
    actor = await _load_actor(request)
    if actor is None:
        raise AuthenticationError("UNAUTHENTICATED", "authentication required")
    if request.method in {"POST", "PATCH", "DELETE"}:
        await _require_csrf(actor, request.headers.get("X-CSRF-Token"))
    if not settings.auth_disabled and request.url.path not in {
        "/api/v1/auth/local/password",
        "/api/v1/auth/logout",
        "/api/v1/auth/session",
    }:
        async with connect() as conn:
            user = await load_session_user(conn, user_id=actor.user_id)
        if user is not None and user.password_change_required:
            raise AuthenticationError("PASSWORD_CHANGE_REQUIRED", "password change is required", status_code=403)
    return actor


def _require_platform_admin(actor: ActorContext) -> None:
    if actor.platform_role != PlatformRole.platform_admin:
        raise AuthorizationError("PLATFORM_ADMIN_REQUIRED", "platform administrator access is required")


async def _replace_workspace_local_group_members(conn: Any, *, group_id: str, member_user_ids: list[str]) -> None:
    unique_ids = sorted(set(member_user_ids))
    if unique_ids:
        result = await conn.execute(
            text("SELECT id::text AS id FROM users WHERE id::text = ANY(:ids)"), {"ids": unique_ids}
        )
        if {str(row["id"]) for row in result.mappings()} != set(unique_ids):
            raise HTTPException(status_code=422, detail="invalid group member")
    await conn.execute(
        text(
            "DELETE FROM group_memberships WHERE group_id = :group_id "
            "AND membership_source = 'LOCAL'"
        ),
        {"group_id": group_id},
    )
    for user_id in unique_ids:
        await conn.execute(
            text(
                """
                INSERT INTO group_memberships(group_id, user_id, membership_source)
                VALUES (:group_id, :user_id, 'LOCAL')
                ON CONFLICT (group_id, user_id, membership_source) DO NOTHING
                """
            ),
            {"group_id": group_id, "user_id": user_id},
        )
    await conn.execute(text("UPDATE workspace_authorization_state SET revision = revision + 1 WHERE id = true"))


async def _load_workspace_group(conn: Any, *, group_id: str) -> dict[str, Any] | None:
    result = await conn.execute(text("SELECT * FROM groups WHERE id = :id"), {"id": group_id})
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def _load_job_for_actor(conn: Any, *, tenant_id: str, job_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text("SELECT * FROM ingestion_jobs WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": job_id, "tenant_id": tenant_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def _load_query_run_for_actor(conn: Any, *, tenant_id: str, query_run_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text("SELECT * FROM query_runs WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": query_run_id, "tenant_id": tenant_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def _load_conversation_run(
    conn: Any, *, tenant_id: str, user_id: str, conversation_id: str
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            "SELECT * FROM query_runs WHERE tenant_id = :tenant_id AND user_id = :user_id "
            "AND usage->>'conversation_id' = :conversation_id ORDER BY created_at DESC LIMIT 1"
        ),
        {"tenant_id": tenant_id, "user_id": user_id, "conversation_id": conversation_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


def _kb_scope_ids(knowledge_base_ids: list[str], default_kb_id: str) -> list[str]:
    scope: list[str] = []
    for kb_id in knowledge_base_ids or [default_kb_id]:
        normalized = str(kb_id).strip()
        if normalized and normalized not in scope:
            scope.append(normalized)
    return scope or [default_kb_id]


def _import_file_name_error() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": {
                "code": "IMPORT_FILE_NAME_INVALID",
                "message": "import file must be an available filename in the configured directory",
            }
        },
    )


async def _require_search_scope_ready(conn: Any, *, tenant_id: str, kb_ids: list[str]) -> None:
    """Validate every requested KB has a registered active search index."""
    for kb_id in kb_ids:
        kb = await get_knowledge_base(conn, tenant_id, kb_id)
        if kb is None:
            raise KnowledgeBaseNotReady("knowledge base is not available", details={"knowledge_base_id": kb_id})
        read_alias = str(kb.get("active_index") or "")
        if not read_alias:
            raise KnowledgeBaseNotReady("knowledge base has no active index", details={"knowledge_base_id": kb_id})
        index_version = await load_index_version_by_read_alias(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            read_alias=read_alias,
        )
        if index_version is None:
            raise KnowledgeBaseNotReady(
                "active index has no registered index version",
                details={"knowledge_base_id": kb_id, "read_alias": read_alias},
            )


async def _load_workspace_viewer_document(
    conn: Any, *, actor: ActorContext, document_id: str
) -> tuple[dict[str, Any], Any, bool, bool]:
    """Use live grants/memberships for a public document read."""
    repository = WorkspaceGrantRepository(conn)
    resource = await repository.load_document(document_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="document not found")
    read, write, share, _ = await repository.authorize(
        user_id=actor.user_id,
        platform_role=WorkspacePlatformRole(actor.platform_role.value),
        resource=resource,
    )
    if not read:
        raise HTTPException(status_code=404, detail="document not found")
    result = await conn.execute(
        text(
            """
            SELECT d.id, d.tenant_id, d.knowledge_base_id, d.title, d.source_type,
                   COALESCE(d.metadata ->> 'source_kind', d.source_type) AS source_kind,
                   d.metadata, d.created_at, d.updated_at,
                   d.lifecycle_state, d.deleted_at, d.purge_after,
                   v.id AS current_version_id, v.status AS version_status, v.status AS status,
                   v.public_metadata ->> 'filename' AS filename, v.public_metadata,
                   v.parser_route, v.parser_name, v.parser_version, v.content_hash, v.normalized_hash,
                   v.uploaded_at, v.upload_completed_at, v.ingested_at, v.published_at
            FROM documents d
            LEFT JOIN LATERAL (
              SELECT * FROM document_versions WHERE document_id = d.id
              ORDER BY created_at DESC LIMIT 1
            ) v ON true
            WHERE d.id = :document_id AND d.lifecycle_state = 'active'
            """
        ),
        {"document_id": document_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")
    return dict(row), resource, write, share


def _source_public_payload(row: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(row.get("metadata") or {})
    return {
        "id": str(row["id"]),
        "knowledge_base_id": str(row["knowledge_base_id"]),
        "kind": str(row["kind"]),
        "name": str(row["name"]),
        "status": str(row["status"]),
        "config": dict(row.get("config") or {}),
        "metadata": metadata,
        "refresh_interval_seconds": row.get("refresh_interval_seconds"),
        "last_sync_run_id": str(row["last_sync_run_id"]) if row.get("last_sync_run_id") is not None else None,
        "last_sync_status": str(row["last_sync_status"]) if row.get("last_sync_status") is not None else None,
        "last_synced_at": row.get("last_synced_at"),
        "next_sync_at": row.get("next_sync_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _document_public_payload(row: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(row.get("metadata") or {})
    payload = dict(row)
    payload["provenance"] = public_provenance_from_metadata(
        dict(row.get("public_metadata") or {}),
        document_id=str(row.get("id") or ""),
        document_version_id=str(row.get("current_version_id") or ""),
        source_uri=str(row.get("source_uri") or ""),
        source_url=str(metadata.get("source_url") or ""),
    )
    return payload


def _sync_run_public_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "source_id": str(row["source_id"]),
        "knowledge_base_id": str(row["knowledge_base_id"]),
        "mode": str(row["mode"]),
        "status": str(row["status"]),
        "cursor_before": dict(row.get("cursor_before") or {}),
        "cursor_after": dict(row.get("cursor_after") or {}),
        "checkpoint": dict(row.get("checkpoint") or {}),
        "stats": dict(row.get("stats") or {}),
        "error_code": str(row["error_code"]) if row.get("error_code") is not None else None,
        "error_message": str(row["error_message"]) if row.get("error_message") is not None else None,
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _reject_secrets_in_config(config: dict[str, Any]) -> None:
    """Reject connector config payloads that place secret-like values outside credentials."""
    secret_tokens = ("secret", "password", "token", "cookie", "credential")

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key).casefold()
                if any(token in key_text for token in secret_tokens):
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error": {
                                "code": "SOURCE_CONFIG_SECRET_FIELD",
                                "message": "connector secrets must be passed in credentials, not config",
                                "details": {"path": f"{path}.{key}" if path else str(key)},
                            }
                        },
                    )
                walk(item, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(config, "")


def _decrypt_api_credentials(settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    from wikipediarag.oidc_service import decrypt_server_tokens

    return decrypt_server_tokens(settings, {str(key): str(value) for key, value in payload.items()})


def _document_section(row: dict[str, Any]) -> DocumentSection:
    return DocumentSection(
        section_id=str(row.get("section_id") or ""),
        parent_section_id=str(row["parent_section_id"]) if row.get("parent_section_id") else None,
        title=str(row.get("title") or ""),
        level=int(row.get("level") or 1),
        path=[str(item) for item in row.get("path") or []],
        ordinal=int(row.get("ordinal") or 1),
        locator=cast(dict[str, Any], row.get("locator") if isinstance(row.get("locator"), dict) else {}),
        first_chunk_id=str(row["first_chunk_id"]) if row.get("first_chunk_id") else None,
        last_chunk_id=str(row["last_chunk_id"]) if row.get("last_chunk_id") else None,
        metadata=cast(dict[str, Any], row.get("metadata") if isinstance(row.get("metadata"), dict) else {}),
    )


def _document_context_chunk(row: dict[str, Any], *, anchor_chunk_id: str | None) -> DocumentContextChunk:
    chunk_metadata = dict(row.get("chunk_metadata") or {})
    locator = row.get("locator")
    if not isinstance(locator, dict) or not locator:
        locator = chunk_metadata.get("locator")
    chunk_id = str(row.get("chunk_id") or "")
    return DocumentContextChunk(
        chunk_id=chunk_id,
        document_id=str(row.get("document_id") or ""),
        document_version_id=str(row["document_version_id"]) if row.get("document_version_id") else None,
        knowledge_base_id=str(row.get("knowledge_base_id") or ""),
        title=str(row.get("title") or ""),
        section_path=[str(item) for item in row.get("section_path") or []],
        content=str(row.get("content") or ""),
        source_url=str(row.get("source_url") or ""),
        locator=cast(dict[str, Any], locator if isinstance(locator, dict) else {}),
        prev_chunk_id=str(row["prev_chunk_id"]) if row.get("prev_chunk_id") else None,
        next_chunk_id=str(row["next_chunk_id"]) if row.get("next_chunk_id") else None,
        chunk_ordinal=int(row["chunk_ordinal"]) if row.get("chunk_ordinal") is not None else None,
        highlighted=bool(anchor_chunk_id and chunk_id == anchor_chunk_id),
        provenance=SourceProvenance.model_validate(
            public_provenance_from_metadata(
                chunk_metadata,
                document_id=str(row.get("document_id") or ""),
                document_version_id=str(row.get("document_version_id") or ""),
                source_url=str(row.get("source_url") or ""),
                chunk_id=chunk_id,
            )
        ),
    )


def _document_search_result(row: dict[str, Any], *, query: str) -> DocumentSearchResult:
    context = _document_context_chunk(row, anchor_chunk_id=None)
    return DocumentSearchResult(
        chunk_id=context.chunk_id,
        document_id=context.document_id,
        document_version_id=context.document_version_id,
        knowledge_base_id=context.knowledge_base_id,
        title=context.title,
        snippet=_search_snippet(context.content, query=query),
        section_path=context.section_path,
        source_url=context.source_url,
        locator=context.locator,
        prev_chunk_id=context.prev_chunk_id,
        next_chunk_id=context.next_chunk_id,
        score=float(row.get("score") or 0.0),
        ranks=dict(row.get("ranks") or {}),
        provenance=context.provenance,
    )


def _parse_search_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _search_snippet(content: str, *, query: str, max_chars: int = 360) -> str:
    normalized_content = " ".join(content.split())
    if len(normalized_content) <= max_chars:
        return normalized_content
    needle = " ".join(query.split()).casefold()
    haystack = normalized_content.casefold()
    index = haystack.find(needle) if needle else -1
    if index < 0:
        return normalized_content[: max_chars - 3].rstrip() + "..."
    start = max(0, index - 90)
    end = min(len(normalized_content), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(normalized_content) else ""
    return prefix + normalized_content[start:end].strip() + suffix


def _query_run_kb_scope(run: dict[str, Any]) -> list[str]:
    usage = run.get("usage")
    if isinstance(usage, dict):
        raw_scope = usage.get("knowledge_base_ids")
        if isinstance(raw_scope, list):
            scope = [str(kb_id) for kb_id in raw_scope if str(kb_id)]
            if scope:
                return scope
    kb_id = run.get("knowledge_base_id")
    return [str(kb_id)] if kb_id else []


def _query_run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(run.get("id") or ""),
        "tenant_id": str(run.get("tenant_id") or ""),
        "knowledge_base_id": str(run.get("knowledge_base_id") or ""),
        "user_id": str(run.get("user_id") or ""),
        "request_id": str(run.get("request_id") or ""),
        "client_request_id": run.get("client_request_id"),
        "mode": run.get("mode"),
        "status": run.get("status"),
        "input_text": run.get("input_text"),
        "answer": run.get("answer"),
        "model_alias": run.get("model_alias"),
        "provider_request_id": run.get("provider_request_id"),
        "error_code": run.get("error_code"),
        "usage": run.get("usage") if isinstance(run.get("usage"), dict) else {},
        "trace_id": run.get("trace_id"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "created_at": run.get("created_at"),
    }


def _research_scope_ids(scope_rows: list[dict[str, Any]]) -> list[str]:
    return [
        normalized
        for normalized in (str(row.get("knowledge_base_id") or "").strip() for row in scope_rows)
        if normalized
    ]


def _research_run_summary(run: dict[str, Any], *, knowledge_base_ids: list[str] | None = None) -> dict[str, Any]:
    scope_ids = list(dict.fromkeys(knowledge_base_ids or []))
    if not scope_ids:
        usage = run.get("usage")
        if isinstance(usage, dict):
            raw_scope = usage.get("knowledge_base_ids")
            if isinstance(raw_scope, list):
                scope_ids = [str(item) for item in raw_scope if str(item)]
    if not scope_ids and run.get("knowledge_base_id"):
        scope_ids = [str(run.get("knowledge_base_id"))]
    return {
        "id": str(run.get("id") or ""),
        "knowledge_base_id": str(run.get("knowledge_base_id") or ""),
        "knowledge_base_ids": scope_ids,
        "user_id": str(run.get("user_id") or "") or None,
        "topic": str(run.get("topic") or ""),
        "retrieval_profile": str(run.get("retrieval_profile") or ""),
        "tool_mode": str(run.get("tool_mode") or DEFAULT_RESEARCH_TOOL_MODE),
        "status": str(run.get("status") or "received"),
        "progress": run.get("progress") if isinstance(run.get("progress"), dict) else {},
        "stop_reason": run.get("stop_reason"),
        "error_code": run.get("error_code"),
        "active_job_id": _optional_uuid(run, "active_job_id"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "completed_at": run.get("completed_at"),
    }


async def _load_research_run_or_404(conn: Any, *, tenant_id: str, research_run_id: str) -> dict[str, Any]:
    run = await get_research_run(conn, tenant_id=tenant_id, research_run_id=research_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="research run not found")
    return run


async def _authorize_research_run(
    conn: Any,
    *,
    actor: ActorContext,
    tenant_id: str,
    run: dict[str, Any],
    action: str,
) -> None:
    del tenant_id
    kb_id = str(run["knowledge_base_id"])
    repository = WorkspaceGrantRepository(conn)
    resource = await repository.load_knowledge_base(kb_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="research run not found")
    read, write, share, delete = await repository.authorize(
        user_id=actor.user_id,
        platform_role=WorkspacePlatformRole(actor.platform_role.value),
        resource=resource,
    )
    is_creator = str(run.get("user_id") or "") == actor.user_id
    if action == "read":
        if read:
            return
    elif read and (is_creator or write or share or delete):
        return
    raise HTTPException(status_code=403, detail="research run access denied")


async def _reauthorize_research_evidence(
    conn: Any,
    evidence: list[dict[str, Any]],
    *,
    actor: ActorContext,
) -> list[dict[str, Any]]:
    """Persisted evidence cannot preserve access after document revocation."""
    allowed = await WorkspaceGrantRepository(conn).authorized_document_ids(
        user_id=actor.user_id,
        platform_role=WorkspacePlatformRole(actor.platform_role.value),
        document_ids=[str(row.get("document_id") or "") for row in evidence],
    )
    return [row for row in evidence if str(row.get("document_id") or "") in allowed]


def _research_detail(
    run: dict[str, Any],
    *,
    records: dict[str, list[dict[str, Any]]],
    knowledge_base_ids: list[str] | None = None,
) -> dict[str, Any]:
    evidence = visible_research_evidence(records["evidence"])
    visible_evidence_ids = {str(row.get("id")) for row in evidence}
    claims = []
    for row in records["claims"]:
        evidence_ids = [str(item) for item in row.get("evidence_ids") or []]
        if evidence_ids and all(item in visible_evidence_ids for item in evidence_ids):
            claims.append(row)
    visible_claim_ids = {str(row.get("id")) for row in claims}
    relations = [
        row
        for row in records.get("relations", [])
        if str(row.get("source_claim_id") or "") in visible_claim_ids
        and str(row.get("target_claim_id") or "") in visible_claim_ids
    ]
    coverage = [
        {
            **row,
            "linked_evidence_ids": [
                str(item) for item in row.get("linked_evidence_ids") or [] if str(item) in visible_evidence_ids
            ],
        }
        for row in records["coverage"]
    ]
    final_report = build_public_research_report(
        run,
        questions=records["questions"],
        coverage=coverage,
        evidence=evidence,
        claims=claims,
        reflections=records["reflections"],
    )
    return {
        "run": _research_run_summary(run, knowledge_base_ids=knowledge_base_ids),
        "questions": records["questions"],
        "coverage": coverage,
        "evidence": evidence,
        "claims": claims,
        "relations": relations,
        "reflections": records["reflections"],
        "episodes": records["episodes"],
        "tool_calls": records.get("tool_calls", []),
        "decisions": records.get("decisions", []),
        "final_report": final_report,
    }


def _optional_uuid(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    return str(value) if value is not None else None


async def _require_csrf(actor: ActorContext, csrf_token: str | None) -> None:
    settings = get_settings()
    if settings.auth_disabled:
        return
    if actor.authentication_method == AuthenticationMethod.test:
        return
    if not csrf_token:
        raise AuthenticationError("CSRF_TOKEN_REQUIRED", "CSRF token is required", status_code=403)
    async with connect() as conn:
        if not await csrf_token_matches(conn, session_id=actor.session_id, csrf_token=csrf_token):
            raise AuthenticationError("CSRF_TOKEN_INVALID", "CSRF token is invalid", status_code=403)


async def _default_active_tenant(conn: Any, user_id: str) -> str | None:
    result = await conn.execute(
        text(
            """
            SELECT tenant_id
            FROM tenant_memberships
            WHERE user_id = :user_id
            ORDER BY tenant_id
            LIMIT 1
            """
        ),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    return str(row["tenant_id"]) if row is not None else None


def _set_session_cookie(response: Response, session_token: str, settings: Any, *, max_age: int) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        session_token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        max_age=max_age,
        path="/",
    )


def _delete_session_cookie(response: Response, settings: Any) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        httponly=True,
    )


def _auth_user_response(user: Any) -> AuthUserResponse:
    return AuthUserResponse(
        id=user.user_id,
        username=user.username,
        display_name=user.display_name,
        platform_role=user.platform_role.value,
        password_change_required=user.password_change_required,
    )


async def _audit(
    conn: Any,
    *,
    request: Request,
    actor: ActorContext | None,
    action: str,
    target_type: str,
    target_id: str | None,
    outcome: str,
) -> None:
    await insert_audit_event(
        conn,
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        metadata={"request_path": str(request.url.path)},
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(_jsonable(data), ensure_ascii=False)}\n\n"


def _initial_query_run_usage(
    *,
    mode: str,
    profile: Any,
    retrieval_overrides: dict[str, Any],
    knowledge_base_ids: list[str],
    route_decision: dict[str, str],
    trace_id: str,
    settings: Any,
    conversation_id: str | None = None,
    ambiguity_mode: str = "auto",
) -> dict[str, Any]:
    """Build the safe initial query-run usage envelope used by chat and debug search."""
    return {
        "trace_id": trace_id,
        "conversation_id": conversation_id,
        "ambiguity_mode": ambiguity_mode,
        "mode": mode,
        "retrieval_profile": {
            "name": profile.name,
            "source": profile.source,
            "version": profile.version,
        },
        "retrieval_overrides": retrieval_overrides,
        "knowledge_base_ids": knowledge_base_ids,
        "route_decision": route_decision,
        "extended_search": _extended_search_status(
            route_decision=route_decision,
            knowledge_base_ids=knowledge_base_ids,
            extended_policy=profile.postprocess.extended_search,
        ),
        "content_policy": content_policy(settings),
    }


def _extended_search_status(
    *,
    route_decision: dict[str, str],
    knowledge_base_ids: list[str],
    extended_policy: str,
) -> dict[str, str]:
    route = route_decision.get("route", "")
    reason = route_decision.get("reason", "")
    if route in {"extended_first", "extended_repair"}:
        return {"decision": "started", "reason": reason}
    if extended_policy == "off":
        return {"decision": "skipped", "reason": "extended_search_policy_off"}
    return {"decision": "skipped", "reason": reason or "direct_path_selected"}


def _path_selection_event(
    *,
    mode: str,
    route_decision: dict[str, str],
    knowledge_base_ids: list[str],
    profile: Any,
    search_plan: dict[str, Any],
    retrieval_overrides: dict[str, Any],
) -> dict[str, Any]:
    return {
        "stage": "path_selected",
        "stable_stage": "path_selected",
        "mode": mode,
        "route": route_decision.get("route"),
        "reason": route_decision.get("reason"),
        "retrieval_profile": profile.name,
        "retrieval_overrides": retrieval_overrides,
        "knowledge_base_ids": knowledge_base_ids,
        "extended_search": _extended_search_status(
            route_decision=route_decision,
            knowledge_base_ids=knowledge_base_ids,
            extended_policy=profile.postprocess.extended_search,
        ),
        "search_plan": search_plan,
    }


async def _insert_answer_events(
    conn: Any,
    *,
    tenant_id: str,
    query_run_id: str,
    trace_id: str,
    retrieval: RetrievalResult,
    answer: str,
    validation: dict[str, Any],
    timings_ms: dict[str, int],
    answer_artifact: dict[str, Any],
) -> None:
    """Persist answer-generation and validation events for retrieval observability."""
    context_source_summary = _context_source_summary(retrieval, list(validation.get("citations") or []))
    await insert_retrieval_event(
        conn,
        tenant_id=tenant_id,
        query_run_id=query_run_id,
        trace_id=trace_id,
        event_type="answer_stage",
        stage="answer_generation",
        payload={
            "stage": "answer_generation",
            "stable_stage": "answer_generation",
            "answer": answer,
            "model_call": validation.get("model_gateway", {}),
            "status": validation.get("status"),
            "model_output_contract_abstained": bool(validation.get("model_output_contract_abstained")),
            "model_output_contract_reason": validation.get("model_output_contract_reason"),
            "context_source_summary": context_source_summary,
            "latency_ms": timings_ms.get("generation_total", 0),
        },
    )
    await insert_retrieval_event(
        conn,
        tenant_id=tenant_id,
        query_run_id=query_run_id,
        trace_id=trace_id,
        event_type="answer_stage",
        stage="citation_validation",
        payload={
            "stage": "citation_validation",
            "stable_stage": "citation_validation",
            "valid": validation.get("valid"),
            "status": validation.get("status"),
            "citations": list(validation.get("citations") or []),
            "unknown": list(validation.get("unknown") or []),
            "unsupported_claims": list(validation.get("unsupported_claims") or []),
            "phantom_claim_citations": list(validation.get("phantom_claim_citations") or []),
            "source_url_errors": list(validation.get("source_url_errors") or []),
            "latency_ms": timings_ms.get("citation_validation", 0),
        },
    )
    claim_verification = validation.get("claim_verification")
    if isinstance(claim_verification, dict):
        await insert_retrieval_event(
            conn,
            tenant_id=tenant_id,
            query_run_id=query_run_id,
            trace_id=trace_id,
            event_type="answer_stage",
            stage="claim_verification",
            payload={
                "stage": "claim_verification",
                "stable_stage": "claim_verification",
                "status": claim_verification.get("status"),
                "model_call": claim_verification.get("model_gateway", {}),
                "verdicts": claim_verification.get("verdicts", []),
                "unsupported_claims": claim_verification.get("unsupported_claims", []),
                "contradicted_claims": claim_verification.get("contradicted_claims", []),
                "latency_ms": timings_ms.get("claim_verification", 0),
            },
        )
    await insert_retrieval_event(
        conn,
        tenant_id=tenant_id,
        query_run_id=query_run_id,
        trace_id=trace_id,
        event_type="query_stage",
        stage="query_complete",
        payload={
            "stage": "query_complete",
            "stable_stage": "query_complete",
            "answer_present": bool(answer),
            "citations": list(validation.get("citations") or []),
            "context_source_summary": context_source_summary,
            "citation_validation": {
                "valid": validation.get("valid"),
                "status": validation.get("status"),
            },
            "root_cause": answer_artifact.get("root_cause", {}),
            "timings_ms": timings_ms,
        },
    )


def _combined_timings(retrieval: dict[str, Any], validation: dict[str, object]) -> dict[str, int]:
    timings: dict[str, int] = {}
    for event in retrieval.get("events", []):
        if not isinstance(event, dict):
            continue
        if event.get("stage") == "timings" and isinstance(event.get("timings_ms"), dict):
            timings.update(_safe_timing_dict(event["timings_ms"]))
        if event.get("stage") in {"harness_tool", "retrieval.extended"} and isinstance(
            event.get("latency_ms"), int | float
        ):
            timings["extended_tool_search_total"] = timings.get("extended_tool_search_total", 0) + int(
                event["latency_ms"]
            )
        if event.get("stage") == "harness" and isinstance(event.get("timings_ms"), dict):
            timings.update(_safe_timing_dict(event["timings_ms"]))
    validation_timings = validation.get("timings_ms")
    if isinstance(validation_timings, dict):
        timings.update(_safe_timing_dict(validation_timings))
    return timings


def _safe_timing_dict(payload: dict[Any, Any]) -> dict[str, int]:
    return {
        str(key): max(0, int(value)) for key, value in payload.items() if isinstance(value, int | float) and str(key)
    }


def _failure_stage_event(
    exc: Exception,
    *,
    stage: str,
    last_successful_stage: str,
    retrieval: RetrievalResult | None = None,
) -> dict[str, Any]:
    metadata = getattr(exc, "metadata", None)
    model_call = dict(metadata) if isinstance(metadata, dict) else {}
    code = safe_error_code(exc)
    if model_call and "safe_error_code" not in model_call:
        model_call["safe_error_code"] = code
    return {
        "stage": stage,
        "stable_stage": stage,
        "status": "failed",
        "last_successful_stage": last_successful_stage,
        "error": {
            "code": code,
            "retryable": is_retryable_exception(exc),
        },
        "model_call": model_call,
        "context_source_summary": _context_source_summary(retrieval, []),
    }


def _context_source_summary(retrieval: RetrievalResult | None, citations: list[Any]) -> list[dict[str, Any]]:
    if retrieval is None:
        return []
    citation_ids = {str(citation) for citation in citations}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for evidence in retrieval.evidence:
        metadata = dict(evidence.metadata or {})
        raw_query_context = metadata.get("query_context")
        query_context: dict[Any, Any] = raw_query_context if isinstance(raw_query_context, dict) else {}
        subquery_id = str(metadata.get("subquery_id") or query_context.get("subquery_id") or "unknown")
        transform_id = str(metadata.get("transform_id") or query_context.get("transform_id") or "unknown")
        key = (subquery_id, transform_id)
        item = grouped.setdefault(
            key,
            {
                "subquery_id": subquery_id,
                "transform_id": transform_id,
                "evidence_count": 0,
                "citation_ids": [],
            },
        )
        item["evidence_count"] += 1
        if evidence.evidence_id in citation_ids:
            item["citation_ids"].append(evidence.evidence_id)
    return list(grouped.values())


def _safe_failure_payload(
    exc: Exception,
    *,
    stage: str,
    last_successful_stage: str,
    trace_id: str,
    request_id: str = "",
    elapsed_ms: int = 0,
    retrieval: Any | None,
    query_run_id: str | None = None,
    knowledge_base_id: str = "",
    search_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a redacted stream failure payload for chat clients."""
    safe_failure = safe_failure_from_exception(
        exc,
        stage=stage,
        request_id=request_id or trace_id,
        operation_id=query_run_id or "",
    )
    retryable = safe_failure.retryable
    safe_search_plan = search_plan or {}
    typed_retrieval = cast(RetrievalResult | None, retrieval)
    artifact = build_failure_artifact(
        query_run_id=query_run_id,
        knowledge_base_id=knowledge_base_id,
        search_plan=safe_search_plan,
        retrieval=typed_retrieval,
        stage=stage,
        last_successful_stage=last_successful_stage,
        code=safe_failure.error_code,
        retryable=retryable,
        trace_id=trace_id,
    )
    return {
        "error": "chat run failed",
        "stage": stage,
        "code": safe_failure.error_code,
        "retryable": retryable,
        "attempt": 1,
        "last_successful_stage": last_successful_stage,
        "trace_id": trace_id,
        "request_id": request_id or trace_id,
        "elapsed_ms": max(0, int(elapsed_ms)),
        "safe_message": safe_failure.error_code,
        "operation_id": query_run_id,
        "retrieval": _safe_retrieval_snapshot(retrieval),
        "search_plan": safe_search_plan,
        "root_cause": artifact["root_cause"],
        "answer_artifact": artifact,
    }


def _retryable_error(exc: Exception) -> bool:
    return is_retryable_exception(exc)


def _safe_retrieval_snapshot(retrieval: Any | None) -> dict[str, Any]:
    """Reduce retrieval diagnostics to fields safe for client-visible failures."""
    if retrieval is None:
        return {}
    payload = retrieval.model_dump() if hasattr(retrieval, "model_dump") else {}
    evidence = []
    for item in payload.get("evidence", []):
        if not isinstance(item, dict):
            continue
        evidence.append(
            {
                "evidence_id": item.get("evidence_id"),
                "chunk_id": item.get("chunk_id"),
                "title": item.get("title"),
                "source_url": item.get("source_url"),
                "scores": item.get("scores", {}),
            }
        )
    events = []
    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        safe_event = {
            "stage": event.get("stage"),
            "count": event.get("count"),
            "latency_ms": event.get("latency_ms"),
            "stage_latency_ms": event.get("stage_latency_ms"),
            "top": event.get("top", []),
            "run_contract_id": event.get("run_contract_id"),
            "index_contract_id": event.get("index_contract_id"),
        }
        candidates = []
        for candidate in list(event.get("candidates") or [])[:20]:
            if isinstance(candidate, dict):
                candidates.append(
                    {
                        "chunk_id": candidate.get("chunk_id"),
                        "title": candidate.get("title"),
                        "source_url": candidate.get("source_url"),
                        "scores": candidate.get("scores", {}),
                        "ranks": candidate.get("ranks", {}),
                    }
                )
        if candidates:
            safe_event["candidates"] = candidates
        if event.get("decision"):
            safe_event["decision"] = event.get("decision")
        events.append(safe_event)
    return {
        "trace_id": payload.get("trace_id", ""),
        "index_contract_id": payload.get("index_contract_id", ""),
        "run_contract_id": payload.get("run_contract_id", ""),
        "evidence": evidence,
        "events": events,
    }


def _kb_not_ready_http(exc: KnowledgeBaseNotReady, request_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "code": exc.code,
                "message": str(exc),
                "request_id": request_id,
                "details": exc.details,
            }
        },
    )


def _retrieval_profile_incompatible_http(exc: RetrievalProfileIncompatible, request_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "code": "RETRIEVAL_PROFILE_INCOMPATIBLE",
                "message": "the selected knowledge bases do not share a compatible retrieval profile",
                "request_id": request_id,
                "details": exc.details,
            }
        },
    )


def _unknown_retrieval_profile_http(exc: Exception, request_id: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": {
                "code": "RETRIEVAL_PROFILE_UNKNOWN",
                "message": "the requested retrieval profile is not configured",
                "request_id": request_id,
                "details": {},
            }
        },
    )


def _error_payload(
    *,
    code: str,
    message: str,
    request_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details or {},
        }
    }


def _http_error_code(status_code: int) -> str:
    return {
        400: "BAD_REQUEST",
        401: "UNAUTHENTICATED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
        422: "REQUEST_VALIDATION_FAILED",
    }.get(status_code, "HTTP_ERROR")


def _request_id(request: Request) -> str:
    candidate = request.headers.get("x-request-id")
    if candidate:
        return candidate[:128]
    return str(uuid.uuid4())


def _safe_validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    safe_errors: list[dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        safe_errors.append(
            {
                key: value
                for key, value in error.items()
                if key
                in {
                    "type",
                    "loc",
                    "msg",
                    "ctx",
                    "url",
                }
            }
        )
    return safe_errors


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


chat = stream_chat_response
search_debug = run_debug_search
