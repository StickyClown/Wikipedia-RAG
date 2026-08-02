from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, cast

import httpx
from fastapi import File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text

from wikipediarag.answerability import should_try_extended_search
from wikipediarag.answering import generate_answer
from wikipediarag.auth import (
    ActorContext,
    AuthenticationMethod,
    AuthorizationError,
    GrantSubjectType,
    GroupType,
    KnowledgeBaseRole,
    PlatformRole,
    has_kb_role,
    require_active_tenant,
    require_tenant_admin,
)
from wikipediarag.auth import require_kb_role as enforce_kb_role
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
    rotate_session_token,
    select_active_tenant,
    test_actor_context,
)
from wikipediarag.config import get_settings
from wikipediarag.db import connect, ensure_schema
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
from wikipediarag.document_access import (
    DocumentAccessScope,
    is_document_visible,
    normalize_document_access,
)
from wikipediarag.document_ingestion import UploadValidationError, safe_upload_filename, sha256_hex
from wikipediarag.extended import run_extended_search, should_start_extended
from wikipediarag.ids import stable_hash
from wikipediarag.observability import content_policy, safe_error_code, safe_telemetry_payload
from wikipediarag.oidc_service import complete_oidc_callback, encrypt_server_tokens, oidc_login_enabled, start_oidc_flow
from wikipediarag.repository import (
    complete_query_run,
    create_document_deletion_job,
    create_document_upload_records,
    create_ingestion_job,
    create_knowledge_source,
    create_query_run,
    create_reprocess_job,
    create_research_resume_job,
    create_research_run,
    create_source_sync_job,
    create_upload_batch,
    create_upload_session,
    fail_query_run,
    fetch_document_context_chunks,
    get_document_lifecycle,
    get_document_public,
    get_knowledge_base,
    get_knowledge_source,
    get_research_run,
    get_source_sync_run_public,
    get_upload_batch_status,
    get_upload_session,
    insert_audit_event,
    insert_retrieval_event,
    list_document_sections,
    list_document_versions_public,
    list_knowledge_bases,
    list_knowledge_sources_public,
    list_research_runs,
    list_source_active_document_refs,
    load_actor_document_access_scope,
    load_effective_knowledge_base_role,
    load_index_version_by_read_alias,
    load_research_detail_records,
    load_retrieval_events,
    request_cancel,
    request_research_cancel,
    request_research_pause,
    request_resume,
    search_document_chunks,
    soft_delete_document,
    update_document_access_metadata,
    update_knowledge_source,
    update_knowledge_source_document_access_default,
    update_query_run_usage,
)
from wikipediarag.retrieval import retrieve, retrieve_multi
from wikipediarag.retrieval_contract import KnowledgeBaseNotReady, validate_active_retrieval_contract
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import (
    AccessGroupResponse,
    AuthOidcStartResponse,
    AuthSessionResponse,
    AuthUserResponse,
    ChatRequest,
    DebugSearchRequest,
    DocumentAccessPatch,
    DocumentAccessResponse,
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
    KnowledgeBaseGrantCreate,
    KnowledgeBaseGrantPatch,
    KnowledgeBasePatch,
    LocalLoginRequest,
    LocalPasswordChangeRequest,
    QueryRunEvaluationRequest,
    QueryRunFeedbackRequest,
    ResearchRunActionResponse,
    ResearchRunCreate,
    ResearchRunDetail,
    ResearchRunListResponse,
    ResearchRunStatus,
    RetrievalResult,
    SearchRequest,
    SearchResponse,
    SourceAccessPatch,
    SourceAccessResponse,
    SourceCreate,
    SourceHealthResponse,
    SourcePatch,
    SourceResponse,
    SourceSyncRequest,
    SourceSyncResponse,
    SourceSyncRunResponse,
    SseEvent,
    TenantCreate,
    TenantPatch,
    TenantSelectionRequest,
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
from wikipediarag.search_index import READ_ALIAS, delete_document_chunks, update_document_access
from wikipediarag.search_service import run_public_search
from wikipediarag.source_connectors import ConnectorError, connector_for_kind
from wikipediarag.storage import create_presigned_put_url, head_object, put_bytes


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


async def health() -> dict[str, str]:
    """Report process liveness without checking downstream dependencies."""
    return {"status": "ok"}


async def ready() -> dict[str, Any]:
    """Report API readiness for PostgreSQL and the Model Gateway dependency."""
    settings = get_settings()
    components: dict[str, str] = {}
    try:
        async with connect() as conn:
            await conn.execute(text("SELECT 1"))
        components["postgres"] = "ok"
    except Exception:
        components["postgres"] = "failed"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{settings.model_gateway_url.rstrip('/')}/ready")
            gateway_ready = response.status_code == 200 and response.json().get("status") == "ok"
            components["model_gateway"] = "ok" if gateway_ready else "failed"
    except Exception:
        components["model_gateway"] = "failed"
    status = "ok" if all(value == "ok" for value in components.values()) else "degraded"
    return {"status": status, "components": components}


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
        active_tenant_id = (
            settings.default_tenant_id
            if user.platform_role == PlatformRole.platform_admin
            else await _default_active_tenant(conn, user.user_id)
        )
        created = await create_session(
            conn,
            user_id=user.user_id,
            authentication_method=AuthenticationMethod.local,
            settings=settings,
            remember_me=payload.remember_me,
            active_tenant_id=active_tenant_id,
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
        active_tenant_id=active_tenant_id,
        tenant_role=None,
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
        active_tenant_id=login.active_tenant_id,
        tenant_role=None,
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
            active_tenant_id=actor.active_tenant_id,
            tenant_role=actor.tenant_role.value if actor.tenant_role is not None else None,
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
        active_tenant_id=actor.active_tenant_id,
        tenant_role=actor.tenant_role.value if actor.tenant_role is not None else None,
        authentication_method=actor.authentication_method.value,
        session_id=actor.session_id,
        csrf_token=csrf_token,
        expires_at=None,
    )


async def select_session_tenant(
    payload: TenantSelectionRequest,
    request: Request,
    response: Response,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> AuthSessionResponse:
    """Switch the active tenant for the current session after authorization checks."""
    actor = await _require_actor(request)
    await _require_csrf(actor, x_csrf_token)
    settings = get_settings()
    async with connect() as conn:
        tenant_role = await select_active_tenant(
            conn,
            session_id=actor.session_id,
            user_id=actor.user_id,
            platform_role=actor.platform_role,
            tenant_id=payload.tenant_id,
        )
        session_token = await rotate_session_token(conn, session_id=actor.session_id)
        user = await load_session_user(conn, user_id=actor.user_id)
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="auth.tenant_selected",
            target_type="tenant",
            target_id=payload.tenant_id,
            outcome="success",
        )
    _set_session_cookie(response, session_token, settings, max_age=settings.session_max_seconds)
    return AuthSessionResponse(
        authenticated=True,
        user=_auth_user_response(user) if user is not None else None,
        active_tenant_id=payload.tenant_id,
        tenant_role=tenant_role.value if tenant_role is not None else None,
        authentication_method=actor.authentication_method.value,
        session_id=actor.session_id,
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


async def admin_list_tenants(request: Request) -> list[dict[str, Any]]:
    """List tenants for platform administrators."""
    actor = await _require_actor(request)
    _require_platform_admin(actor)
    async with connect() as conn:
        result = await conn.execute(text("SELECT id, slug, name, created_at, updated_at FROM tenants ORDER BY slug"))
        return [cast(dict[str, Any], _jsonable(dict(row))) for row in result.mappings()]


async def admin_create_tenant(payload: TenantCreate, request: Request) -> dict[str, str]:
    """Create a tenant record for platform administrators."""
    actor = await _require_actor(request)
    _require_platform_admin(actor)
    tenant_id = str(uuid.uuid4())
    async with connect() as conn:
        await conn.execute(
            text("INSERT INTO tenants(id, slug, name) VALUES (:id, :slug, :name)"),
            {"id": tenant_id, "slug": payload.slug, "name": payload.name},
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="admin.tenant_created",
            target_type="tenant",
            target_id=tenant_id,
            outcome="success",
        )
    return {"id": tenant_id, "slug": payload.slug, "name": payload.name}


async def admin_patch_tenant(tenant_id: str, payload: TenantPatch, request: Request) -> dict[str, str]:
    """Update mutable tenant attributes for platform administrators."""
    actor = await _require_actor(request)
    _require_platform_admin(actor)
    if payload.name is None:
        return {"status": "unchanged"}
    async with connect() as conn:
        await conn.execute(
            text("UPDATE tenants SET name = :name, updated_at = now() WHERE id = :id"),
            {"id": tenant_id, "name": payload.name},
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="admin.tenant_updated",
            target_type="tenant",
            target_id=tenant_id,
            outcome="success",
        )
    return {"status": "updated"}


async def list_groups(request: Request) -> list[dict[str, Any]]:
    """List tenant groups and local membership summaries."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    require_tenant_admin(actor)
    async with connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT g.id, g.name, g.group_type, g.external_id, g.created_at, g.updated_at,
                       COALESCE(json_agg(gm.user_id) FILTER (WHERE gm.user_id IS NOT NULL), '[]') AS member_user_ids
                FROM groups g
                LEFT JOIN group_memberships gm ON gm.group_id = g.id
                WHERE g.tenant_id = :tenant_id
                GROUP BY g.id
                ORDER BY g.name
                """
            ),
            {"tenant_id": tenant_id},
        )
        return [cast(dict[str, Any], _jsonable(dict(row))) for row in result.mappings()]


async def list_access_groups(kb_id: str, request: Request) -> list[AccessGroupResponse]:
    """List tenant groups that KB managers can use in document ACL allowlists."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.manager)
        result = await conn.execute(
            text(
                """
                SELECT id, name, group_type, external_id
                FROM groups
                WHERE tenant_id = :tenant_id
                ORDER BY name
                """
            ),
            {"tenant_id": tenant_id},
        )
        return [AccessGroupResponse.model_validate(_jsonable(dict(row))) for row in result.mappings()]


async def create_group(payload: GroupCreate, request: Request) -> dict[str, str]:
    """Create a tenant-local or OIDC-backed group."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    require_tenant_admin(actor)
    group_type = GroupType(payload.group_type)
    if group_type == GroupType.oidc and not (payload.external_id or payload.name.startswith("/")):
        raise HTTPException(status_code=422, detail="OIDC groups require a full external group path")
    group_id = str(uuid.uuid4())
    async with connect() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO groups(id, tenant_id, name, group_type, external_id)
                VALUES (:id, :tenant_id, :name, :group_type, :external_id)
                """
            ),
            {
                "id": group_id,
                "tenant_id": tenant_id,
                "name": payload.name,
                "group_type": group_type.value,
                "external_id": payload.external_id,
            },
        )
        if group_type == GroupType.local:
            await _replace_local_group_members(conn, group_id=group_id, member_user_ids=payload.member_user_ids)
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
    tenant_id = require_active_tenant(actor)
    require_tenant_admin(actor)
    async with connect() as conn:
        group = await _load_group(conn, tenant_id=tenant_id, group_id=group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        if payload.name is not None:
            await conn.execute(
                text("UPDATE groups SET name = :name, updated_at = now() WHERE id = :id"),
                {"id": group_id, "name": payload.name},
            )
        if payload.member_user_ids is not None:
            if GroupType(group["group_type"]) != GroupType.local:
                raise HTTPException(status_code=409, detail="OIDC group membership is externally managed")
            await _replace_local_group_members(conn, group_id=group_id, member_user_ids=payload.member_user_ids)
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
    """Delete a tenant group and its membership rows."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    require_tenant_admin(actor)
    async with connect() as conn:
        await conn.execute(text("DELETE FROM group_memberships WHERE group_id = :id"), {"id": group_id})
        await conn.execute(
            text("DELETE FROM groups WHERE id = :id AND tenant_id = :tenant_id"),
            {"id": group_id, "tenant_id": tenant_id},
        )
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
    """List knowledge bases visible in the active tenant."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        return await list_knowledge_bases(conn, tenant_id)


async def create_knowledge_base(payload: KnowledgeBaseCreate, request: Request) -> dict[str, str]:
    """Create a tenant knowledge base and grant the creator owner access."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    kb_id = str(uuid.uuid4())
    async with connect() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO knowledge_bases(id, tenant_id, name)
                VALUES (:id, :tenant_id, :name)
                """
            ),
            {"id": kb_id, "tenant_id": tenant_id, "name": payload.name},
        )
        await conn.execute(
            text(
                """
                INSERT INTO knowledge_base_grants(
                  id, tenant_id, knowledge_base_id, subject_type, subject_id, role, created_by_user_id
                )
                VALUES (:id, :tenant_id, :kb_id, 'USER', :user_id_text, 'OWNER', :user_id)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "user_id": actor.user_id,
                "user_id_text": actor.user_id,
            },
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
    """Read a knowledge base after viewer-role enforcement."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.viewer)
        kb = await get_knowledge_base(conn, tenant_id, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return cast(dict[str, Any], _jsonable(kb))


async def patch_knowledge_base(kb_id: str, payload: KnowledgeBasePatch, request: Request) -> dict[str, str]:
    """Rename a knowledge base after manager-role enforcement."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    if payload.name is None:
        return {"status": "unchanged"}
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.manager)
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
    """Delete a knowledge base after owner-role enforcement."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.owner)
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
    return {"status": "deleted"}


async def list_kb_grants(kb_id: str, request: Request) -> list[dict[str, Any]]:
    """List knowledge-base grants for managers and owners."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.manager)
        result = await conn.execute(
            text(
                """
                SELECT id, subject_type, subject_id, role, created_by_user_id, metadata, created_at, updated_at
                FROM knowledge_base_grants
                WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id
                ORDER BY created_at DESC
                """
            ),
            {"tenant_id": tenant_id, "kb_id": kb_id},
        )
        return [cast(dict[str, Any], _jsonable(dict(row))) for row in result.mappings()]


async def create_kb_grant(kb_id: str, payload: KnowledgeBaseGrantCreate, request: Request) -> dict[str, Any]:
    """Create or update a knowledge-base grant with ACL metadata."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    role = KnowledgeBaseRole(payload.role)
    subject_type = GrantSubjectType(payload.subject_type)
    async with connect() as conn:
        required = KnowledgeBaseRole.owner if role == KnowledgeBaseRole.owner else KnowledgeBaseRole.manager
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=required)
        grant_id = str(uuid.uuid4())
        metadata = await _kb_grant_acl_metadata(
            conn,
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_id=payload.subject_id,
            role=role,
            actor=actor,
        )
        await conn.execute(
            text(
                """
                INSERT INTO knowledge_base_grants(
                  id, tenant_id, knowledge_base_id, subject_type, subject_id, role, created_by_user_id, metadata
                )
                VALUES (
                  :id, :tenant_id, :kb_id, :subject_type, :subject_id, :role,
                  :created_by_user_id, CAST(:metadata AS jsonb)
                )
                ON CONFLICT (tenant_id, knowledge_base_id, subject_type, subject_id)
                DO UPDATE SET role = EXCLUDED.role, metadata = EXCLUDED.metadata, updated_at = now()
                """
            ),
            {
                "id": grant_id,
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "subject_type": subject_type.value,
                "subject_id": payload.subject_id,
                "role": role.value,
                "created_by_user_id": actor.user_id,
                "metadata": json.dumps(metadata, ensure_ascii=False),
            },
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="knowledge_base.grant_created",
            target_type="knowledge_base",
            target_id=kb_id,
            outcome="success",
        )
    return {"id": grant_id, "role": role.value, "metadata": metadata}


async def patch_kb_grant(
    kb_id: str,
    grant_id: str,
    payload: KnowledgeBaseGrantPatch,
    request: Request,
) -> dict[str, Any]:
    """Update a knowledge-base grant role and refresh ACL metadata."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    role = KnowledgeBaseRole(payload.role)
    async with connect() as conn:
        required = KnowledgeBaseRole.owner if role == KnowledgeBaseRole.owner else KnowledgeBaseRole.manager
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=required)
        existing = await conn.execute(
            text(
                """
                SELECT subject_type, subject_id
                FROM knowledge_base_grants
                WHERE id = :id AND tenant_id = :tenant_id AND knowledge_base_id = :kb_id
                """
            ),
            {"id": grant_id, "tenant_id": tenant_id, "kb_id": kb_id},
        )
        grant = existing.mappings().first()
        if grant is None:
            raise HTTPException(status_code=404, detail="knowledge base grant not found")
        metadata = await _kb_grant_acl_metadata(
            conn,
            tenant_id=tenant_id,
            subject_type=GrantSubjectType(str(grant["subject_type"])),
            subject_id=str(grant["subject_id"]),
            role=role,
            actor=actor,
        )
        await conn.execute(
            text(
                """
                UPDATE knowledge_base_grants
                SET role = :role, metadata = CAST(:metadata AS jsonb), updated_at = now()
                WHERE id = :id AND tenant_id = :tenant_id AND knowledge_base_id = :kb_id
                """
            ),
            {
                "id": grant_id,
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "role": role.value,
                "metadata": json.dumps(metadata, ensure_ascii=False),
            },
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="knowledge_base.grant_updated",
            target_type="knowledge_base_grant",
            target_id=grant_id,
            outcome="success",
        )
    return {"status": "updated", "metadata": metadata}


async def delete_kb_grant(kb_id: str, grant_id: str, request: Request) -> dict[str, str]:
    """Remove a knowledge-base grant for a manager-scoped request."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.manager)
        await conn.execute(
            text(
                """
                DELETE FROM knowledge_base_grants
                WHERE id = :id AND tenant_id = :tenant_id AND knowledge_base_id = :kb_id
                """
            ),
            {"id": grant_id, "tenant_id": tenant_id, "kb_id": kb_id},
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="knowledge_base.grant_deleted",
            target_type="knowledge_base_grant",
            target_id=grant_id,
            outcome="success",
        )
    return {"status": "deleted"}


async def list_sources(kb_id: str, request: Request) -> dict[str, Any]:
    """List external knowledge sources without exposing stored credentials."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.viewer)
        rows = await list_knowledge_sources_public(conn, tenant_id=tenant_id, knowledge_base_id=kb_id)
    sources = [SourceResponse.model_validate(_source_public_payload(row)).model_dump(mode="json") for row in rows]
    return {"sources": sources}


async def create_source(kb_id: str, payload: SourceCreate, request: Request) -> SourceResponse:
    """Create an external source with credentials stored only in encrypted server state."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    _reject_secrets_in_config(payload.config)
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.manager)
        if await get_knowledge_base(conn, tenant_id, kb_id) is None:
            raise HTTPException(status_code=404, detail="knowledge base not found")
        metadata = {**payload.metadata, "source_contract": "external_source_v1"}
        if payload.document_access_default is not None:
            metadata["document_access_default"] = normalize_document_access(payload.document_access_default)
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
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.viewer)
        row = await get_knowledge_source(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, source_id=source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    return SourceResponse.model_validate(_source_public_payload(row))


async def patch_source_access(
    kb_id: str,
    source_id: str,
    payload: SourceAccessPatch,
    request: Request,
) -> SourceAccessResponse:
    """Update default document access for a source and optionally existing source documents."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    document_access = normalize_document_access(payload.model_dump(mode="json"))
    updated_documents = 0
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.manager)
        source = await get_knowledge_source(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, source_id=source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        await update_knowledge_source_document_access_default(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            source_id=source_id,
            document_access=document_access,
        )
        targets = (
            await list_source_active_document_refs(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                source_id=source_id,
            )
            if payload.apply_to_existing
            else []
        )
        for target in targets:
            await update_document_access_metadata(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_id=str(target["document_id"]),
                document_version_id=str(target["document_version_id"]) if target.get("document_version_id") else None,
                document_access=document_access,
                origin="source_default",
            )
        updated_documents = len(targets)
        kb = await get_knowledge_base(conn, tenant_id, kb_id)
        read_alias = str((kb or {}).get("active_index") or READ_ALIAS)
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="source.document_access_updated",
            target_type="knowledge_source",
            target_id=source_id,
            outcome="success",
        )
    for target in targets:
        await asyncio.to_thread(
            update_document_access,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            document_id=str(target["document_id"]),
            document_access=document_access,
            origin="source_default",
            settings=settings,
            read_alias=read_alias,
        )
    return SourceAccessResponse(
        source_id=source_id,
        knowledge_base_id=kb_id,
        document_access_default=document_access,
        updated_documents=updated_documents,
    )


async def patch_source(kb_id: str, source_id: str, payload: SourcePatch, request: Request) -> SourceResponse:
    """Update source settings while keeping credentials out of public responses."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    if payload.config is not None:
        _reject_secrets_in_config(payload.config)
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.manager)
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
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.editor)
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
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.editor)
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
    return SourceSyncResponse(source_id=source_id, run_id=str(run_id), job_id=str(job_id), status="received")


async def get_source_sync_run(run_id: str, request: Request) -> SourceSyncRunResponse:
    """Read source sync-run status after KB viewer authorization."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        row = await get_source_sync_run_public(conn, tenant_id=tenant_id, run_id=run_id)
        if row is not None:
            await _require_kb_role(
                conn,
                actor=actor,
                tenant_id=tenant_id,
                kb_id=str(row["knowledge_base_id"]),
                role=KnowledgeBaseRole.viewer,
            )
    if row is None:
        raise HTTPException(status_code=404, detail="source sync run not found")
    return SourceSyncRunResponse.model_validate(_sync_run_public_payload(row))


async def create_wikipedia_import(payload: ImportRequest, request: Request) -> dict[str, str]:
    """Queue a bounded XML Wikipedia import job for the default knowledge base."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    config = {
        "limit": payload.limit,
        "xml_path": payload.xml_path or str(settings.wiki_xml_path),
        "index_path": payload.index_path or str(settings.wiki_index_path),
        "snapshot_id": payload.snapshot_id or settings.wiki_snapshot_id,
        "retrieval_profile": settings.retrieval_profile,
    }
    async with connect() as conn:
        await _require_kb_role(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            kb_id=settings.default_kb_id,
            role=KnowledgeBaseRole.editor,
        )
        job_id = await create_ingestion_job(
            conn,
            tenant_id,
            settings.default_kb_id,
            "wikipedia_xml",
            config,
        )
    return {"job_id": str(job_id)}


async def create_zim_import(payload: ZimImportRequest, request: Request) -> dict[str, str]:
    """Queue a bounded ZIM Wikipedia import job for the default knowledge base."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    config = {
        "limit": payload.limit or 10000,
        "zim_path": payload.zim_path,
        "zim_dir": str(settings.zim_dir),
        "zim_filename": payload.zim_filename or settings.zim_filename,
        "snapshot_id": payload.snapshot_id,
        "kiwix_public_base_url": settings.kiwix_public_base_url,
        "kiwix_book_name": settings.kiwix_book_name,
        "retrieval_profile": settings.retrieval_profile,
    }
    async with connect() as conn:
        await _require_kb_role(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            kb_id=settings.default_kb_id,
            role=KnowledgeBaseRole.editor,
        )
        job_id = await create_ingestion_job(
            conn,
            tenant_id,
            settings.default_kb_id,
            "wikipedia_zim",
            config,
        )
    return {"job_id": str(job_id)}


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
        await _require_kb_role(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            kb_id=str(job["knowledge_base_id"]),
            role=KnowledgeBaseRole.editor,
        )
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
        await _require_kb_role(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            kb_id=str(job["knowledge_base_id"]),
            role=KnowledgeBaseRole.editor,
        )
        await request_resume(conn, job_id)
    return {"status": "resume_requested"}


async def upload_document_multipart(
    kb_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    parser_profile: Annotated[str, Form()] = "standard",
    metadata_json: Annotated[str, Form()] = "{}",
) -> UploadCompleteResponse:
    """Accept a small multipart document upload and enqueue asynchronous ingestion."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
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
    data = await file.read()
    if len(data) > settings.upload_max_bytes:
        raise HTTPException(status_code=413, detail="uploaded file exceeds configured upload size limit")
    checksum = sha256_hex(data)
    content_type = file.content_type or "application/octet-stream"
    object_key = f"uploads/{tenant_id}/{kb_id}/api/{stable_hash([filename, checksum], 16)}/{checksum}"
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.editor)
        if await get_knowledge_base(conn, tenant_id, kb_id) is None:
            raise HTTPException(status_code=404, detail="knowledge base not found")
    await asyncio.to_thread(put_bytes, object_key, data, content_type=content_type, settings=settings)
    async with connect() as conn:
        session_id, _expires_at = await create_upload_session(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            checksum_sha256=checksum,
            object_key=object_key,
            parser_profile=parser_profile,
            metadata={**metadata, "api_multipart_upload": True},
            ttl_seconds=settings.upload_session_ttl_seconds,
        )
        upload_session = await get_upload_session(conn, tenant_id=tenant_id, upload_session_id=str(session_id))
        if upload_session is None:
            raise HTTPException(status_code=500, detail="upload session was not created")
        document_hash = stable_hash([tenant_id, kb_id, checksum, filename], 24)
        document_id = f"doc:{document_hash}"
        document_version_id = "docv:" + stable_hash(
            [document_id, checksum, parser_profile, "normalized_document_v1"],
            32,
        )
        job_id = await create_document_upload_records(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            upload_session=upload_session,
            document_id=document_id,
            document_version_id=document_version_id,
            content_hash=checksum,
            metadata={**metadata, "api_multipart_upload": True},
        )
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="document.upload_multipart",
            target_type="document",
            target_id=document_id,
            outcome="success",
        )
    return UploadCompleteResponse(
        document_id=document_id,
        document_version_id=document_version_id,
        job_id=str(job_id),
        status="received",
    )


async def create_upload_session_endpoint(payload: UploadSessionCreate, request: Request) -> UploadSessionAccepted:
    """Create a presigned object-storage upload session without exposing object keys."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    try:
        filename = safe_upload_filename(payload.filename)
    except UploadValidationError as exc:
        raise HTTPException(status_code=422, detail={"error": {"code": exc.code, "message": exc.safe_message}}) from exc
    kb_id = payload.knowledge_base_id or settings.default_kb_id
    async with connect() as conn:
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.editor)
        kb = await get_knowledge_base(conn, tenant_id, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    object_key = (
        f"uploads/{tenant_id}/{kb_id}/{stable_hash([filename, payload.checksum_sha256], 16)}/{payload.checksum_sha256}"
    )
    async with connect() as conn:
        session_id, expires_at = await create_upload_session(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            filename=filename,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
            checksum_sha256=payload.checksum_sha256.lower(),
            object_key=object_key,
            parser_profile=payload.parser_profile,
            metadata=payload.metadata,
            ttl_seconds=settings.upload_session_ttl_seconds,
        )
    upload_url = await asyncio.to_thread(
        create_presigned_put_url,
        object_key,
        content_type=payload.content_type,
        expires_seconds=settings.upload_session_ttl_seconds,
        settings=settings,
    )
    return UploadSessionAccepted(
        upload_session_id=str(session_id),
        upload_url=upload_url,
        expires_at=expires_at,
        required_headers={"Content-Type": payload.content_type},
    )


async def create_upload_batch_endpoint(payload: UploadBatchCreate, request: Request) -> UploadBatchAccepted:
    """Create presigned upload sessions for a batch of validated files."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
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
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.editor)
        kb = await get_knowledge_base(conn, tenant_id, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")

    async with connect() as conn:
        batch_id = await create_upload_batch(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
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
                batch_id=str(batch_id),
                filename=filename,
                content_type=item.content_type,
                size_bytes=item.size_bytes,
                checksum_sha256=checksum,
                object_key=object_key,
                parser_profile=item.parser_profile,
                metadata=item.metadata,
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
    return UploadBatchAccepted(
        batch_id=str(batch_id),
        knowledge_base_id=kb_id,
        status="received",
        total_items=len(accepted_items),
        items=accepted_items,
    )


async def get_upload_batch_endpoint(batch_id: str, request: Request) -> UploadBatchStatus:
    """Read safe upload batch progress for an authorized editor."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        status = await get_upload_batch_status(conn, tenant_id=tenant_id, batch_id=batch_id)
        if status is not None:
            await _require_kb_role(
                conn,
                actor=actor,
                tenant_id=tenant_id,
                kb_id=str(status["knowledge_base_id"]),
                role=KnowledgeBaseRole.editor,
            )
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
            await _require_kb_role(
                conn,
                actor=actor,
                tenant_id=tenant_id,
                kb_id=str(session["knowledge_base_id"]),
                role=KnowledgeBaseRole.editor,
            )
    if session is None:
        raise HTTPException(status_code=404, detail="upload session not found")
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
    document_hash = stable_hash(
        [
            tenant_id,
            str(session["knowledge_base_id"]),
            str(session["checksum_sha256"]),
            str(session["filename"]),
        ],
        24,
    )
    document_id = f"doc:{document_hash}"
    document_version_id = "docv:" + stable_hash(
        [
            document_id,
            str(session["checksum_sha256"]),
            str(session["parser_profile"]),
            "normalized_document_v1",
        ],
        32,
    )
    session_metadata = dict(session.get("metadata") or {})
    if payload is not None:
        session_metadata.update(payload.metadata)
    async with connect() as conn:
        job_id = await create_document_upload_records(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=str(session["knowledge_base_id"]),
            upload_session={**session, "metadata": session_metadata},
            document_id=document_id,
            document_version_id=document_version_id,
            content_hash=str(session["checksum_sha256"]),
            metadata=session_metadata,
        )
    return UploadCompleteResponse(
        document_id=document_id,
        document_version_id=document_version_id,
        job_id=str(job_id),
        status="received",
    )


async def get_document(document_id: str, request: Request) -> dict[str, Any]:
    """Read public document metadata after viewer-role enforcement."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        document = await _load_viewer_document(conn, actor=actor, tenant_id=tenant_id, document_id=document_id)
    return cast(dict[str, Any], _jsonable(_document_public_payload(document)))


async def get_document_versions(document_id: str, request: Request) -> dict[str, Any]:
    """List public document versions after viewer-role enforcement."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        await _load_viewer_document(conn, actor=actor, tenant_id=tenant_id, document_id=document_id)
        versions = await list_document_versions_public(conn, tenant_id, document_id)
    return {"document_id": document_id, "versions": _jsonable(versions)}


async def patch_document_access(
    document_id: str,
    payload: DocumentAccessPatch,
    request: Request,
) -> DocumentAccessResponse:
    """Update document access metadata for a manager-scoped document."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    document_access = normalize_document_access(payload.model_dump(mode="json"))
    async with connect() as conn:
        document = await get_document_public(conn, tenant_id, document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="document not found")
        kb_id = str(document["knowledge_base_id"])
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.manager)
        version_id = str(document["current_version_id"]) if document.get("current_version_id") else None
        await update_document_access_metadata(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            document_id=document_id,
            document_version_id=version_id,
            document_access=document_access,
            origin="manual",
        )
        kb = await get_knowledge_base(conn, tenant_id, kb_id)
        read_alias = str((kb or {}).get("active_index") or READ_ALIAS)
        await _audit(
            conn,
            request=request,
            actor=actor,
            action="document.access_updated",
            target_type="document",
            target_id=document_id,
            outcome="success",
        )
    await asyncio.to_thread(
        update_document_access,
        tenant_id=tenant_id,
        knowledge_base_id=kb_id,
        document_id=document_id,
        document_access=document_access,
        origin="manual",
        settings=settings,
        read_alias=read_alias,
    )
    return DocumentAccessResponse(
        document_id=document_id,
        knowledge_base_id=kb_id,
        document_access=document_access,
        document_access_origin="manual",
    )


async def get_document_structure(document_id: str, request: Request) -> DocumentStructureResponse:
    """Return the published document section tree for the viewer."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        document = await _load_viewer_document(conn, actor=actor, tenant_id=tenant_id, document_id=document_id)
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
        document_access=normalize_document_access(metadata.get("document_access")),
        document_access_origin=str(metadata.get("document_access_origin") or ""),
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
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        document = await _load_viewer_document(conn, actor=actor, tenant_id=tenant_id, document_id=document_id)
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
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        document = await _load_viewer_document(conn, actor=actor, tenant_id=tenant_id, document_id=document_id)
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
    tenant_id = require_active_tenant(actor)
    purge_after = datetime.now(UTC) + timedelta(days=max(0, settings.document_soft_delete_retention_days))
    async with connect() as conn:
        document = await get_document_lifecycle(conn, tenant_id, document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="document not found")
        kb_id = str(document["knowledge_base_id"])
        await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.owner)
        kb = await get_knowledge_base(conn, tenant_id, kb_id)
        read_alias = str(kb.get("active_index") or READ_ALIAS) if kb else READ_ALIAS
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
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        document = await get_document_public(conn, tenant_id, document_id)
        if document is None or not document.get("current_version_id"):
            raise HTTPException(status_code=404, detail="document not found")
        await _require_kb_role(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            kb_id=str(document["knowledge_base_id"]),
            role=KnowledgeBaseRole.editor,
        )
        job_id = await create_reprocess_job(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=str(document["knowledge_base_id"]),
            document_id=document_id,
            document_version_id=str(document["current_version_id"]),
        )
    return DocumentReprocessResponse(
        document_id=document_id,
        document_version_id=str(document["current_version_id"]),
        job_id=str(job_id),
        status="received",
    )


async def search(payload: SearchRequest, request: Request) -> SearchResponse:
    """Run public hybrid search for the viewer-scoped KB set."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    kb_ids = _kb_scope_ids(payload.knowledge_base_ids, settings.default_kb_id)
    try:
        async with connect() as conn:
            kb_roles = await _load_kb_scope_roles(
                conn,
                actor=actor,
                tenant_id=tenant_id,
                kb_ids=kb_ids,
            )
            await _require_search_scope_ready(conn, tenant_id=tenant_id, kb_ids=kb_ids)
            access_scopes = await _document_access_scopes(
                conn,
                actor=actor,
                tenant_id=tenant_id,
                kb_ids=kb_ids,
                kb_roles=kb_roles,
            )
            return await run_public_search(
                conn,
                payload,
                tenant_id=tenant_id,
                knowledge_base_ids=kb_ids,
                settings=settings,
                document_access_scopes=access_scopes,
            )
    except KnowledgeBaseNotReady as exc:
        raise _kb_not_ready_http(exc, actor.request_id) from exc
    raise RuntimeError("search response was not returned")


async def stream_chat_response(payload: ChatRequest, request: Request) -> StreamingResponse:
    """Run retrieval, answer generation, diagnostics persistence, and SSE streaming for chat."""
    settings = get_settings()
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    request_id = str(uuid.uuid4())
    trace_id = stable_hash([request_id, payload.message], 32)
    active_profile = get_retrieval_profile(
        payload.retrieval_profile,
        settings,
        payload.retrieval_overrides,
    )
    kb_ids = _kb_scope_ids(payload.knowledge_base_ids, settings.default_kb_id)
    primary_kb_id = kb_ids[0]
    classifier_suggested_extended = should_start_extended(payload.message)
    route_decision = initial_route_decision(
        mode=payload.mode.value,
        extended_policy=active_profile.postprocess.extended_search,
        classifier_suggested_extended=classifier_suggested_extended,
    )
    if len(kb_ids) > 1 and route_decision["route"] != "direct_retrieval":
        route_decision = {
            "route": "direct_retrieval",
            "reason": "multi_kb_extended_search_not_enabled_v1",
        }
    search_plan = build_search_plan(
        query=payload.message,
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
            kb_roles = await _load_kb_scope_roles(
                conn,
                actor=actor,
                tenant_id=tenant_id,
                kb_ids=kb_ids,
            )
            for kb_id in kb_ids:
                await validate_active_retrieval_contract(
                    conn,
                    tenant_id=tenant_id,
                    knowledge_base_id=kb_id,
                    profile=active_profile,
                    retrieval_overrides=payload.retrieval_overrides,
                    settings=settings,
                )
            access_scopes = await _document_access_scopes(
                conn,
                actor=actor,
                tenant_id=tenant_id,
                kb_ids=kb_ids,
                kb_roles=kb_roles,
            )
    except KnowledgeBaseNotReady as exc:
        raise _kb_not_ready_http(exc, request_id) from exc
    search_filters = {"document_access_scopes": access_scopes}
    async with connect() as conn:
        initial_usage = _initial_query_run_usage(
            mode=payload.mode.value,
            profile=active_profile,
            retrieval_overrides=payload.retrieval_overrides,
            knowledge_base_ids=kb_ids,
            route_decision=route_decision,
            trace_id=trace_id,
            settings=settings,
        )
        query_run_id = await create_query_run(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=primary_kb_id,
            user_id=actor.user_id,
            request_id=request_id,
            client_request_id=payload.client_request_id,
            mode=payload.mode.value,
            input_text=payload.message,
            trace_id=trace_id,
            usage=initial_usage,
        )

    async def event_stream() -> AsyncIterator[str]:
        """Coordinate the event_stream workflow while preserving tenant and API contracts."""
        sequence = 1
        current_stage = "question_received"
        last_successful_stage = "question_received"
        retrieval: Any | None = None
        actual_search_plan = search_plan
        yield _event(
            SseEvent(
                event="run.started",
                request_id=request_id,
                query_run_id=str(query_run_id),
                sequence=sequence,
                data={"trace_id": trace_id, "search_plan": actual_search_plan},
            )
        )
        try:
            async with connect() as conn:
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
                        retrieval_overrides=payload.retrieval_overrides,
                    ),
                )
                use_harness_first = route_decision["route"] == "extended_first"
                last_successful_stage = "path_selected"
                if use_harness_first:
                    current_stage = "extended_search"
                    retrieval = await run_extended_search(
                        conn,
                        payload.message,
                        tenant_id=tenant_id,
                        knowledge_base_id=primary_kb_id,
                        query_run_id=str(query_run_id),
                        trace_id=trace_id,
                        settings=settings,
                        profile=active_profile,
                        profile_overrides=payload.retrieval_overrides,
                        search_filters=search_filters,
                    )
                    last_successful_stage = "extended_search"
                else:
                    current_stage = "retrieval"
                    if len(kb_ids) > 1:
                        retrieval = await retrieve_multi(
                            conn,
                            payload.message,
                            tenant_id=tenant_id,
                            knowledge_base_ids=kb_ids,
                            query_run_id=str(query_run_id),
                            trace_id=trace_id,
                            settings=settings,
                            profile=active_profile,
                            profile_overrides=payload.retrieval_overrides,
                            search_filters=search_filters,
                        )
                    else:
                        retrieval = await retrieve(
                            conn,
                            payload.message,
                            tenant_id=tenant_id,
                            knowledge_base_id=primary_kb_id,
                            query_run_id=str(query_run_id),
                            trace_id=trace_id,
                            settings=settings,
                            profile=active_profile,
                            search_filters=search_filters,
                        )
                    last_successful_stage = "retrieval"
                    if (
                        len(kb_ids) == 1
                        and retrieval.answerability
                        and should_try_extended_search(retrieval.answerability)
                        and active_profile.postprocess.extended_search
                        in {
                            "always",
                            "conditional",
                        }
                    ):
                        repair_decision = repair_route_decision(retrieval.answerability)
                        actual_search_plan = build_search_plan(
                            query=payload.message,
                            mode=payload.mode.value,
                            route=repair_decision["route"],
                            route_reason=repair_decision["reason"],
                            knowledge_base_id=primary_kb_id,
                            knowledge_base_ids=kb_ids,
                            trace_id=trace_id,
                            profile=active_profile,
                        )
                        current_stage = "extended_search"
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
                                retrieval_overrides=payload.retrieval_overrides,
                            ),
                        )
                        retrieval = await run_extended_search(
                            conn,
                            payload.message,
                            tenant_id=tenant_id,
                            knowledge_base_id=primary_kb_id,
                            query_run_id=str(query_run_id),
                            trace_id=trace_id,
                            settings=settings,
                            profile=active_profile,
                            profile_overrides=payload.retrieval_overrides,
                            search_filters=search_filters,
                        )
                        last_successful_stage = "extended_search"
            current_stage = "answer_generation"
            answer, validation = await generate_answer(payload.message, retrieval, settings, active_profile)
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
                        "answer_artifact": answer_artifact,
                    },
                    model_alias=str(validation.get("model_alias") or ""),
                    provider_request_id=str(validation.get("provider_request_id") or ""),
                )
                last_successful_stage = "query_run_complete"
            sequence += 1
            yield _event(
                SseEvent(
                    event="run.completed",
                    request_id=request_id,
                    query_run_id=str(query_run_id),
                    sequence=sequence,
                    data={"answer": answer, "root_cause": answer_artifact["root_cause"]},
                )
            )
        except asyncio.CancelledError:
            async with connect() as conn:
                await fail_query_run(conn, query_run_id=str(query_run_id), error_code="ClientDisconnected")
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
                await fail_query_run(conn, query_run_id=str(query_run_id), error_code=type(exc).__name__)
            sequence += 1
            failure = _safe_failure_payload(
                exc,
                stage=current_stage,
                last_successful_stage=last_successful_stage,
                trace_id=trace_id,
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

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def query_run_retrieval(query_run_id: str, request: Request) -> dict[str, Any]:
    """Return stored retrieval events for an editor-authorized query run."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        run = await _load_query_run_for_actor(conn, tenant_id=tenant_id, query_run_id=query_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="query run not found")
        await _require_kb_scope_role(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            kb_ids=_query_run_kb_scope(run),
            role=KnowledgeBaseRole.editor,
        )
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
        await _require_kb_scope_role(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            kb_ids=_query_run_kb_scope(run),
            role=KnowledgeBaseRole.editor,
        )
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
        await _require_kb_scope_role(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            kb_ids=_query_run_kb_scope(run),
            role=KnowledgeBaseRole.editor,
        )
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


async def create_research_run_endpoint(payload: ResearchRunCreate, request: Request) -> ResearchRunActionResponse:
    """Create a durable single-KB Deep Research run and enqueue worker execution."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    settings = get_settings()
    request_id = _request_id(request)
    kb_id = payload.knowledge_base_id or settings.default_kb_id
    profile = get_retrieval_profile(payload.retrieval_profile, settings, payload.retrieval_overrides)
    try:
        async with connect() as conn:
            await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=KnowledgeBaseRole.viewer)
            await validate_active_retrieval_contract(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                profile=profile,
                retrieval_overrides=payload.retrieval_overrides,
                settings=settings,
            )
            run_id, job_id = await create_research_run(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                user_id=actor.user_id,
                topic=payload.topic,
                retrieval_profile=profile.name,
                retrieval_overrides=payload.retrieval_overrides,
                context_policy=context_policy_for_profile(profile),
                questions=build_research_questions(payload.topic),
            )
            await insert_audit_event(
                conn,
                actor=actor,
                tenant_id=tenant_id,
                action="research_run.created",
                target_type="research_run",
                target_id=str(run_id),
                outcome="success",
                metadata={"knowledge_base_id": kb_id, "job_id": str(job_id)},
            )
    except KnowledgeBaseNotReady as exc:
        raise _kb_not_ready_http(exc, request_id) from exc
    return ResearchRunActionResponse(run_id=str(run_id), status=ResearchRunStatus.received, job_id=str(job_id))


async def list_research_runs_endpoint(
    request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> ResearchRunListResponse:
    """List research runs in the active tenant that are visible to the actor."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        rows = await list_research_runs(conn, tenant_id=tenant_id, limit=limit)
        visible: list[dict[str, Any]] = []
        for row in rows:
            kb_role = await _load_kb_role_optional(
                conn,
                actor=actor,
                tenant_id=tenant_id,
                kb_id=str(row["knowledge_base_id"]),
            )
            is_creator = str(row.get("user_id") or "") == actor.user_id
            if (is_creator and has_kb_role(kb_role, KnowledgeBaseRole.viewer)) or has_kb_role(
                kb_role, KnowledgeBaseRole.editor
            ):
                visible.append(_research_run_summary(row))
    return ResearchRunListResponse.model_validate({"runs": _jsonable(visible)})


async def get_research_run_endpoint(research_run_id: str, request: Request) -> ResearchRunDetail:
    """Return a safe current-ACL view of a durable Deep Research run."""
    actor = await _require_actor(request)
    tenant_id = require_active_tenant(actor)
    async with connect() as conn:
        run = await _load_research_run_or_404(conn, tenant_id=tenant_id, research_run_id=research_run_id)
        kb_role = await _authorize_research_run(conn, actor=actor, tenant_id=tenant_id, run=run, action="read")
        access_scope = await load_actor_document_access_scope(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            knowledge_base_id=str(run["knowledge_base_id"]),
            effective_kb_role=kb_role,
        )
        records = await load_research_detail_records(conn, tenant_id=tenant_id, research_run_id=research_run_id)
    detail = _research_detail(run, records=records, access_scope=access_scope)
    return ResearchRunDetail.model_validate(_jsonable(detail))


async def research_run_events(research_run_id: str, request: Request) -> dict[str, Any]:
    """Return compact lifecycle events for a research run without raw source text."""
    detail = await get_research_run_endpoint(research_run_id, request)
    return {
        "run_id": research_run_id,
        "episodes": [item.model_dump(mode="json") for item in detail.episodes],
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
        job_id = await create_research_resume_job(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=str(run["knowledge_base_id"]),
            research_run_id=research_run_id,
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
        kb_roles = await _require_kb_scope_role(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            role=KnowledgeBaseRole.editor,
        )
        access_scopes = await _document_access_scopes(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            kb_roles=kb_roles,
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
                search_filters={"document_access_scopes": access_scopes},
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
        "/api/v1/auth/session/tenant",
    }:
        async with connect() as conn:
            user = await load_session_user(conn, user_id=actor.user_id)
        if user is not None and user.password_change_required:
            raise AuthenticationError("PASSWORD_CHANGE_REQUIRED", "password change is required", status_code=403)
    return actor


def _require_platform_admin(actor: ActorContext) -> None:
    if actor.platform_role != PlatformRole.platform_admin:
        raise AuthorizationError("PLATFORM_ADMIN_REQUIRED", "platform administrator access is required")


async def _require_kb_role(
    conn: Any,
    *,
    actor: ActorContext,
    tenant_id: str,
    kb_id: str,
    role: KnowledgeBaseRole,
) -> KnowledgeBaseRole:
    """Enforce the effective knowledge-base role unless the actor is platform admin."""
    actual = await _load_kb_role_optional(
        conn,
        actor=actor,
        tenant_id=tenant_id,
        kb_id=kb_id,
    )
    enforce_kb_role(actual, role)
    return actual or role


async def _load_kb_role_optional(
    conn: Any,
    *,
    actor: ActorContext,
    tenant_id: str,
    kb_id: str,
) -> KnowledgeBaseRole | None:
    if actor.platform_role == PlatformRole.platform_admin:
        return KnowledgeBaseRole.owner
    return await load_effective_knowledge_base_role(
        conn,
        user_id=actor.user_id,
        tenant_id=tenant_id,
        knowledge_base_id=kb_id,
    )


async def _kb_grant_acl_metadata(
    conn: Any,
    *,
    tenant_id: str,
    subject_type: GrantSubjectType,
    subject_id: str,
    role: KnowledgeBaseRole,
    actor: ActorContext,
) -> dict[str, Any]:
    """Build public ACL sync metadata for KB grant changes."""
    source = "direct_user_grant"
    external_group_path = None
    if subject_type == GrantSubjectType.group:
        group = await _load_group(conn, tenant_id=tenant_id, group_id=subject_id)
        group_type = str(group.get("group_type") or "") if group else ""
        source = "oidc_group_grant" if group_type == GroupType.oidc.value else "local_group_grant"
        if group is not None and group_type == GroupType.oidc.value:
            external_group_path = str(group.get("external_id") or group.get("name") or "")
    synced_at = datetime.now(UTC).isoformat()
    acl_sync: dict[str, Any] = {
        "source": source,
        "status": "in_sync",
        "last_synced_at": synced_at,
    }
    if external_group_path:
        acl_sync["external_group_path"] = external_group_path
    return {
        "schema_version": "kb_grant_acl_metadata_v1",
        "acl_snapshot": {
            "scope": "knowledge_base",
            "subject_type": subject_type.value,
            "subject_id": subject_id,
            "role": role.value,
            "source": "knowledge_base_grants",
        },
        "acl_sync": acl_sync,
        "updated_by_user_id": actor.user_id,
    }


async def _replace_local_group_members(conn: Any, *, group_id: str, member_user_ids: list[str]) -> None:
    await conn.execute(
        text("DELETE FROM group_memberships WHERE group_id = :group_id AND membership_type = 'LOCAL'"),
        {"group_id": group_id},
    )
    for user_id in member_user_ids:
        await conn.execute(
            text(
                """
                INSERT INTO group_memberships(group_id, user_id, membership_type)
                VALUES (:group_id, :user_id, 'LOCAL')
                ON CONFLICT (group_id, user_id, membership_type) DO NOTHING
                """
            ),
            {"group_id": group_id, "user_id": user_id},
        )


async def _load_group(conn: Any, *, tenant_id: str, group_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text("SELECT * FROM groups WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": group_id, "tenant_id": tenant_id},
    )
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


def _kb_scope_ids(knowledge_base_ids: list[str], default_kb_id: str) -> list[str]:
    scope: list[str] = []
    for kb_id in knowledge_base_ids or [default_kb_id]:
        normalized = str(kb_id).strip()
        if normalized and normalized not in scope:
            scope.append(normalized)
    return scope or [default_kb_id]


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


async def _load_viewer_document(conn: Any, *, actor: ActorContext, tenant_id: str, document_id: str) -> dict[str, Any]:
    """Load an active document and enforce document-level visibility."""
    document = await get_document_public(conn, tenant_id, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    kb_role = await _load_kb_role_optional(
        conn,
        actor=actor,
        tenant_id=tenant_id,
        kb_id=str(document["knowledge_base_id"]),
    )
    metadata = dict(document.get("metadata") or {})
    access_scope = await load_actor_document_access_scope(
        conn,
        actor=actor,
        tenant_id=tenant_id,
        knowledge_base_id=str(document["knowledge_base_id"]),
        effective_kb_role=kb_role,
    )
    if not is_document_visible(metadata, access_scope):
        raise HTTPException(status_code=404, detail="document not found")
    return document


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
        "document_access_default": normalize_document_access(metadata.get("document_access_default")),
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
    payload["document_access"] = normalize_document_access(metadata.get("document_access"))
    payload["document_access_origin"] = str(metadata.get("document_access_origin") or "")
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


async def _require_kb_scope_role(
    conn: Any,
    *,
    actor: ActorContext,
    tenant_id: str,
    kb_ids: list[str],
    role: KnowledgeBaseRole,
) -> dict[str, KnowledgeBaseRole]:
    roles: dict[str, KnowledgeBaseRole] = {}
    for kb_id in kb_ids:
        actual = await _require_kb_role(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id, role=role)
        if actual is not None:
            roles[kb_id] = actual
    return roles


async def _load_kb_scope_roles(
    conn: Any,
    *,
    actor: ActorContext,
    tenant_id: str,
    kb_ids: list[str],
) -> dict[str, KnowledgeBaseRole | None]:
    return {
        kb_id: await _load_kb_role_optional(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id) for kb_id in kb_ids
    }


async def _document_access_scopes(
    conn: Any,
    *,
    actor: ActorContext,
    tenant_id: str,
    kb_ids: list[str],
    kb_roles: Mapping[str, KnowledgeBaseRole | None],
) -> dict[str, DocumentAccessScope]:
    return {
        kb_id: await load_actor_document_access_scope(
            conn,
            actor=actor,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            effective_kb_role=kb_roles.get(kb_id),
        )
        for kb_id in kb_ids
    }


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


def _research_run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(run.get("id") or ""),
        "knowledge_base_id": str(run.get("knowledge_base_id") or ""),
        "user_id": str(run.get("user_id") or "") or None,
        "topic": str(run.get("topic") or ""),
        "retrieval_profile": str(run.get("retrieval_profile") or ""),
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
) -> KnowledgeBaseRole:
    kb_id = str(run["knowledge_base_id"])
    kb_role = await _load_kb_role_optional(conn, actor=actor, tenant_id=tenant_id, kb_id=kb_id)
    is_creator = str(run.get("user_id") or "") == actor.user_id
    if action == "read":
        if (is_creator and has_kb_role(kb_role, KnowledgeBaseRole.viewer)) or has_kb_role(
            kb_role, KnowledgeBaseRole.editor
        ):
            return cast(KnowledgeBaseRole, kb_role)
    elif (is_creator and has_kb_role(kb_role, KnowledgeBaseRole.viewer)) or has_kb_role(
        kb_role, KnowledgeBaseRole.editor
    ):
        return cast(KnowledgeBaseRole, kb_role)
    raise HTTPException(status_code=403, detail="research run access denied")


def _research_detail(
    run: dict[str, Any],
    *,
    records: dict[str, list[dict[str, Any]]],
    access_scope: DocumentAccessScope,
) -> dict[str, Any]:
    evidence = visible_research_evidence(records["evidence"], access_scope)
    visible_evidence_ids = {str(row.get("id")) for row in evidence}
    claims = []
    for row in records["claims"]:
        evidence_ids = [str(item) for item in row.get("evidence_ids") or []]
        if not evidence_ids or any(item in visible_evidence_ids for item in evidence_ids):
            claims.append(row)
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
        "run": _research_run_summary(run),
        "questions": records["questions"],
        "coverage": coverage,
        "evidence": evidence,
        "claims": claims,
        "reflections": records["reflections"],
        "episodes": records["episodes"],
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
) -> dict[str, Any]:
    """Build the safe initial query-run usage envelope used by chat and debug search."""
    return {
        "trace_id": trace_id,
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
    if len(knowledge_base_ids) > 1:
        return {"decision": "skipped", "reason": reason or "multi_kb_extended_search_not_enabled_v1"}
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
            "type": type(exc).__name__,
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
    retrieval: Any | None,
    query_run_id: str | None = None,
    knowledge_base_id: str = "",
    search_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a redacted stream failure payload for chat clients."""
    retryable = _retryable_error(exc)
    safe_search_plan = search_plan or {}
    typed_retrieval = cast(RetrievalResult | None, retrieval)
    artifact = build_failure_artifact(
        query_run_id=query_run_id,
        knowledge_base_id=knowledge_base_id,
        search_plan=safe_search_plan,
        retrieval=typed_retrieval,
        stage=stage,
        last_successful_stage=last_successful_stage,
        code=type(exc).__name__,
        retryable=retryable,
        trace_id=trace_id,
    )
    return {
        "error": "chat run failed",
        "stage": stage,
        "code": type(exc).__name__,
        "retryable": retryable,
        "attempt": 1,
        "last_successful_stage": last_successful_stage,
        "trace_id": trace_id,
        "safe_message": type(exc).__name__,
        "retrieval": _safe_retrieval_snapshot(retrieval),
        "search_plan": safe_search_plan,
        "root_cause": artifact["root_cause"],
        "answer_artifact": artifact,
    }


def _retryable_error(exc: Exception) -> bool:
    return type(exc).__name__ not in {"ValueError", "ValidationError", "KnowledgeBaseNotReady"}


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
