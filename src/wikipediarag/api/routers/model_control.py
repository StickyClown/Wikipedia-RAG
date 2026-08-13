from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import APIRouter, Header, HTTPException, Request, Response
from sqlalchemy import text

from wikipediarag.api import handlers
from wikipediarag.config import get_settings
from wikipediarag.db import connect, json_dumps
from wikipediarag.model_control import (
    STAGE_BY_KEY,
    STAGE_CATALOG,
    ModelContract,
    ModelControlError,
    ModelDriver,
    ModelOperation,
    ThinkingPolicy,
    config_hash,
    merge_parameters,
    redact_connection,
    validate_stage_binding,
)
from wikipediarag.model_control_repository import (
    activate_credential,
    activate_revision,
    active_revision,
    create_connection,
    create_model,
    current_revision,
    get_connection,
    get_model,
    list_connections,
    list_models,
    mark_validated,
    patch_connection,
    patch_model,
    record_validation_run,
    save_draft,
    upsert_credential,
)
from wikipediarag.model_drivers import DriverRequest, driver_for
from wikipediarag.oidc_service import decrypt_server_tokens, encrypt_server_tokens
from wikipediarag.repository import insert_audit_event
from wikipediarag.schemas import (
    ModelConfigurationDraft,
    ModelConnectionCreate,
    ModelConnectionPatch,
    ModelCreate,
    ModelPatch,
)

router = APIRouter()


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MODEL_ENDPOINT_URL_INVALID",
                "message": "base URL must be an HTTP(S) URL without embedded credentials",
            },
        )
    allowed_hosts = {
        item.strip().lower() for item in get_settings().model_endpoint_host_allowlist.split(",") if item.strip()
    }
    if allowed_hosts and parsed.hostname and parsed.hostname.lower() not in allowed_hosts:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MODEL_ENDPOINT_HOST_FORBIDDEN",
                "message": "endpoint host is not in the administrator allowlist",
            },
        )
    if parsed.hostname in {"0.0.0.0", "::", "169.254.169.254", "metadata.google.internal"}:  # noqa: S104
        raise HTTPException(
            status_code=422,
            detail={"code": "MODEL_ENDPOINT_HOST_FORBIDDEN", "message": "endpoint host is not allowed"},
        )
    return url.rstrip("/")


async def _admin(request: Request, *, csrf: str | None = None) -> Any:
    actor = await handlers._require_actor(request)
    if request.method == "PUT":
        await handlers._require_csrf(actor, csrf)
    handlers._require_platform_admin(actor)
    return actor


def _safe_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if message == "MODEL_CONFIG_VERSION_CONFLICT":
        return HTTPException(
            status_code=409, detail={"code": message, "message": "configuration was changed by another administrator"}
        )
    if message == "MODEL_VALIDATION_STALE":
        return HTTPException(
            status_code=409, detail={"code": message, "message": "validation belongs to an older configuration hash"}
        )
    if message == "MODEL_CONNECTION_NOT_FOUND" or message == "MODEL_NOT_FOUND":
        return HTTPException(
            status_code=404, detail={"code": message, "message": "model control-plane object was not found"}
        )
    if message == "MODEL_REFERENCED_CANNOT_DISABLE":
        return HTTPException(
            status_code=409,
            detail={"code": message, "message": "a referenced connection or model cannot be disabled"},
        )
    return HTTPException(status_code=422, detail={"code": "MODEL_CONFIG_INVALID", "message": message})


def _connection_response(row: dict[str, Any]) -> dict[str, Any]:
    return redact_connection(row)


@router.get("/api/v1/admin/model-connections")
async def admin_list_model_connections(request: Request) -> list[dict[str, Any]]:
    await _admin(request)
    async with connect() as conn:
        return [_connection_response(row) for row in await list_connections(conn)]


@router.post("/api/v1/admin/model-connections")
async def admin_create_model_connection(request: Request, payload: ModelConnectionCreate) -> dict[str, Any]:
    actor = await _admin(request)
    base_url = _safe_url(payload.base_url)
    if any(
        "secret" in key.lower() or "token" in key.lower() or "password" in key.lower() for key in payload.safe_headers
    ):
        raise HTTPException(
            status_code=422, detail={"code": "MODEL_SECRET_FIELD_FORBIDDEN", "message": "secrets belong in credentials"}
        )
    async with connect() as conn:
        row = await create_connection(
            conn,
            name=payload.name,
            driver=payload.driver,
            base_url=base_url,
            endpoint_paths=payload.endpoint_paths,
            request_adapter=payload.request_adapter,
            request_defaults=payload.request_defaults,
            safe_headers=payload.safe_headers,
            tls_verify=payload.tls_verify,
            enabled=payload.enabled,
        )
        if payload.credentials:
            encrypted = encrypt_server_tokens(get_settings(), payload.credentials)
            await upsert_credential(conn, connection_id=str(row["id"]), encrypted_payload=json_dumps(encrypted))
        await insert_audit_event(
            conn,
            actor=actor,
            action="model_connection.create",
            target_type="model_connection",
            target_id=str(row["id"]),
            outcome="success",
        )
        return _connection_response(row)


@router.patch("/api/v1/admin/model-connections/{connection_id}")
async def admin_patch_model_connection(
    request: Request,
    connection_id: str,
    payload: ModelConnectionPatch,
    if_match: int | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    actor = await _admin(request)
    if if_match is None:
        raise HTTPException(
            status_code=428, detail={"code": "MODEL_CONFIG_VERSION_REQUIRED", "message": "If-Match is required"}
        )
    changes = payload.model_dump(exclude_unset=True)
    credentials = changes.pop("credentials", None)
    if "base_url" in changes:
        changes["base_url"] = _safe_url(changes["base_url"])
    if any(
        "secret" in key.lower() or "token" in key.lower() or "password" in key.lower()
        for key in changes.get("safe_headers", {})
    ):
        raise HTTPException(
            status_code=422, detail={"code": "MODEL_SECRET_FIELD_FORBIDDEN", "message": "secrets belong in credentials"}
        )
    try:
        async with connect() as conn:
            row = await patch_connection(conn, connection_id=connection_id, row_version=if_match, changes=changes)
            if credentials:
                encrypted = encrypt_server_tokens(get_settings(), credentials)
                await upsert_credential(conn, connection_id=connection_id, encrypted_payload=json_dumps(encrypted))
            await insert_audit_event(
                conn,
                actor=actor,
                action="model_connection.patch",
                target_type="model_connection",
                target_id=connection_id,
                outcome="success",
            )
            return _connection_response(row)
    except (RuntimeError, LookupError) as exc:
        raise _safe_error(exc) from exc


async def _connection_probe(connection: dict[str, Any]) -> dict[str, Any]:
    if connection["driver"] == ModelDriver.mock:
        return {"status": "passed", "safe_error_code": None}
    paths = connection.get("endpoint_paths") or {}
    path = paths.get("models") or ("/models" if connection["driver"] == ModelDriver.openrouter else "/v1/models")
    url = f"{str(connection['base_url']).rstrip('/')}/{str(path).lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=bool(connection.get("tls_verify", True))) as client:
            response = await client.get(
                url, headers=connection.get("_probe_headers") or connection.get("safe_headers") or {}
            )
        if response.status_code >= 400:
            return {"status": "failed", "safe_error_code": f"MODEL_ENDPOINT_HTTP_{response.status_code}"}
        return {"status": "passed", "safe_error_code": None}
    except httpx.TimeoutException:
        return {"status": "failed", "safe_error_code": "MODEL_ENDPOINT_TIMEOUT"}
    except (httpx.HTTPError, OSError):
        return {"status": "failed", "safe_error_code": "MODEL_ENDPOINT_UNAVAILABLE"}


async def _probe_connection_with_credentials(conn: Any, connection: dict[str, Any]) -> dict[str, Any]:
    result = dict(connection)
    credential = await conn.execute(
        text(
            "SELECT encrypted_payload FROM model_connection_credentials "
            "WHERE connection_id=:id AND state IN ('pending','active') "
            "ORDER BY CASE WHEN state='pending' THEN 0 ELSE 1 END, version DESC LIMIT 1"
        ),
        {"id": str(connection["id"])},
    )
    encrypted = credential.scalar()
    headers = dict(connection.get("safe_headers") or {})
    if encrypted:
        try:
            payload = decrypt_server_tokens(get_settings(), json.loads(str(encrypted)))
            token = payload.get("api_key") or payload.get("token") or payload.get("access_token")
            if token:
                headers.setdefault("Authorization", f"Bearer {token}")
        except Exception as exc:  # noqa: BLE001 - credentials never leave this process.
            raise HTTPException(
                status_code=503,
                detail={"code": "MODEL_CREDENTIALS_UNREADABLE", "message": "model credentials are unavailable"},
            ) from exc
    result["_probe_headers"] = headers
    return result


@router.post("/api/v1/admin/model-connections/{connection_id}/test")
async def admin_test_model_connection(request: Request, connection_id: str) -> dict[str, Any]:
    actor = await _admin(request)
    async with connect() as conn:
        connection = await get_connection(conn, connection_id)
        if connection is None:
            raise _safe_error(LookupError("MODEL_CONNECTION_NOT_FOUND"))
        connection = await _probe_connection_with_credentials(conn, connection)
        result = await _connection_probe(connection)
        await conn.execute(
            text(
                "UPDATE model_provider_connections SET last_status=CAST(:status AS jsonb), "
                "last_checked_at=now() WHERE id=:id"
            ),
            {"id": connection_id, "status": json_dumps(result)},
        )
        if result["status"] == "passed":
            pending = await conn.execute(
                text(
                    "SELECT version FROM model_connection_credentials WHERE connection_id=:id "
                    "AND state='pending' ORDER BY version DESC LIMIT 1"
                ),
                {"id": connection_id},
            )
            version = pending.scalar()
            if version is not None:
                await activate_credential(conn, connection_id=connection_id, version=int(version))
        await insert_audit_event(
            conn,
            actor=actor,
            action="model_connection.test",
            target_type="model_connection",
            target_id=connection_id,
            outcome="success" if result["status"] == "passed" else "failure",
            metadata={"safe_error_code": result["safe_error_code"]},
        )
        return result


@router.post("/api/v1/admin/model-connections/{connection_id}/discover")
async def admin_discover_model_connection(request: Request, connection_id: str) -> dict[str, Any]:
    actor = await _admin(request)
    async with connect() as conn:
        connection = await get_connection(conn, connection_id)
        if connection is None:
            raise _safe_error(LookupError("MODEL_CONNECTION_NOT_FOUND"))
        connection = await _probe_connection_with_credentials(conn, connection)
    if connection["driver"] == ModelDriver.mock:
        return {"status": "passed", "models": []}
    paths = connection.get("endpoint_paths") or {}
    path = paths.get("models") or ("/models" if connection["driver"] == ModelDriver.openrouter else "/v1/models")
    url = f"{str(connection['base_url']).rstrip('/')}/{str(path).lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=bool(connection.get("tls_verify", True))) as client:
            response = await client.get(
                url, headers=connection.get("_probe_headers") or connection.get("safe_headers") or {}
            )
            response.raise_for_status()
            data = response.json()
        models = data.get("data", data if isinstance(data, list) else [])
        safe_models = [
            {"id": str(item.get("id"))} for item in models[:200] if isinstance(item, dict) and item.get("id")
        ]
        async with connect() as audit_conn:
            await insert_audit_event(
                audit_conn,
                actor=actor,
                action="model_connection.discover",
                target_type="model_connection",
                target_id=connection_id,
                outcome="success",
                metadata={"count": len(safe_models)},
            )
        return {"status": "passed", "models": safe_models}
    except (httpx.HTTPError, ValueError, OSError):
        return {"status": "failed", "safe_error_code": "MODEL_DISCOVERY_FAILED", "models": []}


@router.get("/api/v1/admin/models")
async def admin_list_models(request: Request) -> list[dict[str, Any]]:
    await _admin(request)
    async with connect() as conn:
        return await list_models(conn)


@router.post("/api/v1/admin/models")
async def admin_create_model(request: Request, payload: ModelCreate) -> dict[str, Any]:
    actor = await _admin(request)
    async with connect() as conn:
        row = await create_model(conn, payload.model_dump())
        await insert_audit_event(
            conn, actor=actor, action="model.create", target_type="model", target_id=str(row["id"]), outcome="success"
        )
        return row


@router.patch("/api/v1/admin/models/{model_id}")
async def admin_patch_model(
    request: Request, model_id: str, payload: ModelPatch, if_match: int | None = Header(default=None, alias="If-Match")
) -> dict[str, Any]:
    actor = await _admin(request)
    if if_match is None:
        raise HTTPException(
            status_code=428, detail={"code": "MODEL_CONFIG_VERSION_REQUIRED", "message": "If-Match is required"}
        )
    try:
        async with connect() as conn:
            row = await patch_model(
                conn, model_id=model_id, row_version=if_match, changes=payload.model_dump(exclude_unset=True)
            )
            await insert_audit_event(
                conn, actor=actor, action="model.patch", target_type="model", target_id=model_id, outcome="success"
            )
            return row
    except (RuntimeError, LookupError) as exc:
        raise _safe_error(exc) from exc


@router.post("/api/v1/admin/models/{model_id}/test")
async def admin_test_model(request: Request, model_id: str) -> dict[str, Any]:
    await _admin(request)
    async with connect() as conn:
        model = await get_model(conn, model_id)
        if model is None:
            raise _safe_error(LookupError("MODEL_NOT_FOUND"))
        if model.get("connection_id"):
            connection = await get_connection(conn, str(model["connection_id"]))
            if connection:
                connection = await _probe_connection_with_credentials(conn, connection)
                if str(model["operation"]) == "chat":
                    driver = driver_for(str(connection["driver"]))
                    result = await driver.run_capability_canary(
                        DriverRequest(
                            base_url=str(connection["base_url"]),
                            model=str(model["provider_model"]),
                            paths=connection.get("endpoint_paths") or {},
                            headers=connection.get("_probe_headers") or {},
                            tls_verify=bool(connection.get("tls_verify", True)),
                            request_adapter=connection.get("request_adapter") or {},
                            request_defaults=connection.get("request_defaults") or {},
                        ),
                        max_output_tokens=int((model.get("startup_canary") or {}).get("max_tokens") or 4096),
                    )
                else:
                    result = await _connection_probe(connection)
            else:
                result = {"status": "failed", "safe_error_code": "MODEL_CONNECTION_NOT_FOUND"}
        else:
            result = {"status": "failed", "safe_error_code": "MODEL_CONNECTION_REQUIRED"}
        await conn.execute(
            text("UPDATE model_aliases SET canary_status=CAST(:status AS jsonb), updated_at=now() WHERE id=:id"),
            {"id": model_id, "status": json_dumps(result)},
        )
        await record_validation_run(
            conn,
            target_type="model",
            target_id=model_id,
            config_hash_value=config_hash(model),
            status=result["status"],
            safe_error_code=result.get("safe_error_code"),
        )
        return result


@router.get("/api/v1/admin/model-stages")
async def admin_model_stages(request: Request) -> list[dict[str, Any]]:
    await _admin(request)
    return [
        {
            "key": stage.key,
            "operation": stage.operation.value,
            "required_capabilities": sorted(stage.required_capabilities),
            "catalog_only_vision": True,
        }
        for stage in STAGE_CATALOG
    ]


@router.get("/api/v1/admin/model-configuration")
async def admin_model_configuration(request: Request) -> dict[str, Any]:
    await _admin(request)
    async with connect() as conn:
        draft = await current_revision(conn)
        active = await active_revision(conn)
        return {
            "active": active,
            "draft": draft,
            "stages": [{"key": s.key, "operation": s.operation.value} for s in STAGE_CATALOG],
        }


@router.get("/api/v1/admin/model-configuration/export")
async def admin_export_model_configuration(request: Request) -> Response:
    await _admin(request)
    async with connect() as conn:
        connections = [_connection_response(row) for row in await list_connections(conn)]
        models = await list_models(conn)
        revision = await current_revision(conn)
    safe_models = [
        {
            "alias": row["alias"],
            "provider_model": row["provider_model"],
            "operation": row["operation"],
            "input_modalities": row.get("input_modalities") or ["text"],
            "capabilities": row.get("capabilities") or {},
            "context_window_tokens": row.get("context_window_tokens"),
            "max_output_tokens": row.get("max_output_tokens"),
            "dimensions": row.get("dimensions"),
            "tokenizer_contract": row.get("tokenizer_contract") or {},
            "model_defaults": row.get("model_defaults") or {},
            "thinking_capabilities": row.get("thinking_capabilities") or {},
            "startup_canary": row.get("startup_canary") or {},
            "connection_id": str(row.get("connection_id")) if row.get("connection_id") else None,
            "request_adapter": row.get("request_adapter") or {},
            "request_defaults": row.get("request_defaults") or {},
        }
        for row in models
    ]
    snapshot = (revision or {}).get("resolved_snapshot") or {}
    payload = {
        "version": 2,
        "connections": connections,
        "models": safe_models,
        "stages": snapshot.get("stages", {}),
    }
    return Response(
        content=yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        media_type="application/yaml",
        headers={"Content-Disposition": "attachment; filename=model-control-plane.yaml"},
    )


@router.put("/api/v1/admin/model-configuration/draft")
async def admin_save_model_configuration(
    request: Request,
    payload: ModelConfigurationDraft,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict[str, Any]:
    actor = await _admin(request, csrf=x_csrf_token)
    unknown = set(payload.stages) - set(STAGE_BY_KEY)
    if unknown:
        raise HTTPException(status_code=422, detail={"code": "MODEL_STAGE_UNKNOWN", "message": "unknown stage key"})
    try:
        async with connect() as conn:
            models = await list_models(conn)
            connections = await list_connections(conn)
            snapshot = {
                "stages": payload.stages,
                "models": [
                    {
                        "alias": row["alias"],
                        "provider_model": row["provider_model"],
                        "operation": row["operation"],
                        "connection_id": str(row["connection_id"]) if row.get("connection_id") else None,
                        "model_defaults": row.get("model_defaults") or {},
                        "startup_canary": row.get("startup_canary") or {},
                        "request_adapter": row.get("request_adapter") or {},
                        "request_defaults": row.get("request_defaults") or {},
                    }
                    for row in models
                ],
                "connections": [redact_connection(row) for row in connections],
            }
            revision = await save_draft(
                conn,
                snapshot=snapshot,
                revision_id=payload.revision_id,
                expected_row_version=payload.row_version,
                actor_user_id=actor.user_id,
            )
            await insert_audit_event(
                conn,
                actor=actor,
                action="model_configuration.draft_save",
                target_type="model_configuration",
                target_id=str(revision["id"]),
                outcome="success",
            )
            return revision
    except (RuntimeError, LookupError) as exc:
        raise _safe_error(exc) from exc


async def _validate_snapshot(conn: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    frozen_aliases = snapshot.get("aliases") or {}
    models = {
        str(alias): dict(row)
        for alias, row in frozen_aliases.items()
        if isinstance(row, dict) and str(row.get("alias") or alias) == str(alias)
    }
    failures: list[dict[str, str]] = []
    stages = snapshot.get("stages", {})
    for stage in STAGE_CATALOG:
        binding = stages.get(stage.key)
        if not binding:
            failures.append({"stage": stage.key, "code": "MODEL_STAGE_UNASSIGNED"})
            continue
        alias = binding.get("model_alias") or binding.get("alias")
        model = models.get(str(alias))
        if model is None:
            failures.append({"stage": stage.key, "code": "MODEL_REVISION_SNAPSHOT_INCOMPLETE"})
            continue
        required = {"provider", "provider_model", "operation", "connection_id", "request_adapter", "request_defaults"}
        if not required.issubset(model):
            failures.append({"stage": stage.key, "code": "MODEL_REVISION_SNAPSHOT_INCOMPLETE"})
            continue
        canary_status = model.get("canary_status") or {}
        if isinstance(canary_status, dict) and canary_status.get("status") == "failed":
            failures.append(
                {
                    "stage": stage.key,
                    "code": str(canary_status.get("safe_error_code") or "MODEL_STAGE_CANARY_FAILED"),
                }
            )
            continue
        contract = ModelContract(
            alias=str(model["alias"]),
            provider_model=str(model["provider_model"]),
            operation=ModelOperation(str(model["operation"])),
            capabilities=frozenset({str(model["operation"])})
            | frozenset(k for k, v in (model.get("capabilities") or {}).items() if v is True)
            | frozenset((model.get("capabilities") or {}).get("operations", [])),
            input_modalities=tuple(model.get("input_modalities") or ["text"]),
            context_window_tokens=model.get("context_window_tokens"),
            max_output_tokens=model.get("max_output_tokens"),
            dimensions=model.get("dimensions"),
            tokenizer_contract=model.get("tokenizer_contract") or {},
            model_defaults=model.get("model_defaults") or {},
            thinking_capabilities=model.get("thinking_capabilities") or {},
            enabled=bool(model.get("is_enabled", True)),
        )
        try:
            merge_parameters(
                {}, contract.model_defaults, binding.get("parameter_overrides") or binding.get("parameters") or {}
            )
            validate_stage_binding(stage.key, contract, ThinkingPolicy.from_mapping(binding.get("thinking_policy")))
        except ModelControlError as exc:
            failures.append({"stage": stage.key, "code": str(exc)})
    embedding_aliases = [
        str(stages.get(key, {}).get("model_alias") or stages.get(key, {}).get("alias"))
        for key in ("ingestion.embedding", "retrieval.query_embedding")
        if stages.get(key)
    ]
    if len(embedding_aliases) == 2 and embedding_aliases[0] != embedding_aliases[1]:
        left, right = models.get(embedding_aliases[0]), models.get(embedding_aliases[1])
        if (
            left
            and right
            and (
                left.get("dimensions") != right.get("dimensions")
                or left.get("provider_model") != right.get("provider_model")
            )
        ):
            failures.append({"stage": "retrieval.query_embedding", "code": "MODEL_EMBEDDING_FINGERPRINT_MISMATCH"})
    if embedding_aliases:
        configured_embedding = models.get(embedding_aliases[0])
        if configured_embedding:
            active_indices = await conn.execute(
                text("SELECT DISTINCT embedding_alias, embedding_dimensions FROM index_versions WHERE status='active'")
            )
            for index_row in active_indices.mappings():
                if str(index_row["embedding_alias"]) != embedding_aliases[0] or (
                    configured_embedding.get("dimensions") is not None
                    and int(index_row["embedding_dimensions"]) != int(configured_embedding["dimensions"])
                ):
                    failures.append({"stage": "ingestion.embedding", "code": "MODEL_CONFIG_REINDEX_REQUIRED"})
                    break
    return {"status": "failed" if failures else "passed", "failures": failures, "checked_stages": len(STAGE_CATALOG)}


@router.post("/api/v1/admin/model-configuration/draft/validate")
async def admin_validate_model_configuration(request: Request) -> dict[str, Any]:
    actor = await _admin(request)
    async with connect() as conn:
        revision = await current_revision(conn)
        if revision is None:
            raise HTTPException(
                status_code=422, detail={"code": "MODEL_DRAFT_REQUIRED", "message": "create a draft before validation"}
            )
        report = await _validate_snapshot(conn, revision["resolved_snapshot"] or {})
        if report["status"] == "passed":
            revision = await mark_validated(
                conn, revision_id=str(revision["id"]), config_hash_value=str(revision["config_hash"]), report=report
            )
            await record_validation_run(
                conn,
                target_type="revision",
                target_id=str(revision["id"]),
                config_hash_value=str(revision["config_hash"]),
                status="passed",
                measurements=report,
            )
        else:
            await record_validation_run(
                conn,
                target_type="revision",
                target_id=str(revision["id"]),
                config_hash_value=str(revision["config_hash"]),
                status="failed",
                safe_error_code="MODEL_REVISION_INVALID",
                measurements=report,
            )
        await insert_audit_event(
            conn,
            actor=actor,
            action="model_configuration.validate",
            target_type="model_configuration",
            target_id=str(revision["id"]),
            outcome="success" if report["status"] == "passed" else "failure",
            metadata={"safe_error_code": None if report["status"] == "passed" else "MODEL_REVISION_INVALID"},
        )
        return {"revision": revision, "report": report}


@router.post("/api/v1/admin/model-configuration/draft/activate")
async def admin_activate_model_configuration(request: Request) -> dict[str, Any]:
    actor = await _admin(request)
    async with connect() as conn:
        revision = await current_revision(conn)
        if revision is None:
            raise HTTPException(
                status_code=422, detail={"code": "MODEL_DRAFT_REQUIRED", "message": "create a draft before activation"}
            )
        try:
            active = await activate_revision(
                conn, revision_id=str(revision["id"]), config_hash_value=str(revision["config_hash"])
            )
        except RuntimeError as exc:
            raise _safe_error(exc) from exc
        await insert_audit_event(
            conn,
            actor=actor,
            action="model_configuration.activate",
            target_type="model_configuration",
            target_id=str(active["id"]),
            outcome="success",
            metadata={"config_hash": active["config_hash"]},
        )
        return active


@router.post("/api/v1/admin/model-configuration/revisions/{revision_id}/restore-to-draft")
async def admin_restore_model_configuration(request: Request, revision_id: str) -> dict[str, Any]:
    actor = await _admin(request)
    async with connect() as conn:
        result = await conn.execute(
            text("SELECT resolved_snapshot FROM model_configuration_revisions WHERE id=:id"),
            {"id": revision_id},
        )
        row = result.mappings().first()
        if row is None:
            raise _safe_error(LookupError("MODEL_CONFIG_REVISION_NOT_FOUND"))
        revision = await save_draft(
            conn,
            snapshot=row["resolved_snapshot"] or {},
            revision_id=None,
            expected_row_version=None,
            actor_user_id=actor.user_id,
        )
        await insert_audit_event(
            conn,
            actor=actor,
            action="model_configuration.restore",
            target_type="model_configuration",
            target_id=revision_id,
            outcome="success",
            metadata={"new_draft_id": str(revision["id"])},
        )
        return revision
