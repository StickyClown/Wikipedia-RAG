"""PostgreSQL repository for the global model control plane."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.db import json_dumps
from wikipediarag.ids import new_uuid
from wikipediarag.model_control import config_hash


def _json(value: Any) -> str:
    return json_dumps(value if value is not None else {})


async def _freeze_aliases(conn: AsyncConnection, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Copy the executable alias contract into a revision before it is hashed."""
    frozen = dict(snapshot)
    aliases: dict[str, dict[str, Any]] = {}
    result = await conn.execute(
        text(
            """
            SELECT m.alias, m.provider, m.provider_model, m.operation, m.connection_id,
                   m.input_modalities, m.capabilities, m.context_window_tokens,
                   m.max_output_tokens, m.dimensions, m.tokenizer_contract,
                   m.model_defaults, m.thinking_capabilities, m.startup_canary,
                   c.driver AS connection_driver, c.base_url, c.endpoint_paths,
                   c.safe_headers, c.tls_verify, c.request_adapter AS connection_request_adapter,
                   c.request_defaults AS connection_request_defaults
            FROM model_aliases AS m
            LEFT JOIN model_provider_connections AS c ON c.id=m.connection_id
            WHERE m.is_enabled=true AND (m.connection_id IS NULL OR c.enabled=true)
            ORDER BY m.alias
            """
        )
    )
    for row in result.mappings():
        item = dict(row)
        # The adapter belongs to the connection.  Keep the historical aliases
        # as well so old consumers can read a revision, but never read a
        # mutable model_aliases adapter column (it does not exist).
        item["request_adapter"] = item.get("connection_request_adapter") or {}
        item["request_defaults"] = item.get("connection_request_defaults") or {}
        aliases[str(item["alias"])] = item
    frozen["aliases"] = aliases
    return frozen


async def list_connections(conn: AsyncConnection) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT c.*, EXISTS(
              SELECT 1 FROM model_connection_credentials cr
              WHERE cr.connection_id = c.id AND cr.state = 'active'
            ) AS has_credentials,
            COALESCE((SELECT cr.source FROM model_connection_credentials cr
              WHERE cr.connection_id = c.id AND cr.state = 'active' LIMIT 1), 'database') AS credential_source
            FROM model_provider_connections c ORDER BY c.name
            """
        )
    )
    return [dict(row) for row in result.mappings().all()]


async def get_connection(conn: AsyncConnection, connection_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT c.*, EXISTS(
              SELECT 1 FROM model_connection_credentials cr
              WHERE cr.connection_id = c.id AND cr.state = 'active'
            ) AS has_credentials,
            COALESCE((SELECT cr.source FROM model_connection_credentials cr
              WHERE cr.connection_id = c.id AND cr.state = 'active' LIMIT 1), 'database') AS credential_source
            FROM model_provider_connections c WHERE c.id = :id
            """
        ),
        {"id": connection_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def create_connection(
    conn: AsyncConnection,
    *,
    name: str,
    driver: str,
    base_url: str,
    endpoint_paths: Mapping[str, Any] | None,
    request_adapter: Mapping[str, Any] | None,
    request_defaults: Mapping[str, Any] | None,
    safe_headers: Mapping[str, Any] | None,
    tls_verify: bool,
    enabled: bool,
) -> dict[str, Any]:
    connection_id = new_uuid()
    await conn.execute(
        text(
            """
            INSERT INTO model_provider_connections
              (id,name,driver,base_url,endpoint_paths,request_adapter,request_defaults,safe_headers,tls_verify,enabled)
            VALUES (:id,:name,:driver,:base_url,CAST(:endpoint_paths AS jsonb),CAST(:request_adapter AS jsonb),
                    CAST(:request_defaults AS jsonb),CAST(:safe_headers AS jsonb),:tls_verify,:enabled)
            """
        ),
        {
            "id": str(connection_id),
            "name": name,
            "driver": driver,
            "base_url": base_url.rstrip("/"),
            "endpoint_paths": _json(endpoint_paths),
            "request_adapter": _json(request_adapter),
            "request_defaults": _json(request_defaults),
            "safe_headers": _json(safe_headers),
            "tls_verify": tls_verify,
            "enabled": enabled,
        },
    )
    created = await get_connection(conn, str(connection_id))
    if created is None:  # pragma: no cover - defensive database invariant
        raise RuntimeError("model connection was not created")
    return created


async def patch_connection(
    conn: AsyncConnection,
    *,
    connection_id: str,
    row_version: int,
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    allowed = {
        "name",
        "base_url",
        "endpoint_paths",
        "request_adapter",
        "request_defaults",
        "safe_headers",
        "tls_verify",
        "enabled",
    }
    changes = {key: value for key, value in changes.items() if key in allowed}
    if changes.get("enabled") is False:
        referenced = await conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM model_aliases WHERE connection_id=:id)"),
            {"id": connection_id},
        )
        if referenced.scalar_one():
            raise RuntimeError("MODEL_REFERENCED_CANNOT_DISABLE")
    if not changes:
        current = await get_connection(conn, connection_id)
        if current is None:
            raise LookupError("MODEL_CONNECTION_NOT_FOUND")
        return current
    assignments: list[str] = []
    params: dict[str, Any] = {"id": connection_id, "row_version": row_version}
    for key, value in changes.items():
        if key in {"endpoint_paths", "request_adapter", "request_defaults", "safe_headers"}:
            assignments.append(f"{key} = CAST(:{key} AS jsonb)")
            params[key] = _json(value)
        elif key == "base_url":
            assignments.append("base_url = :base_url")
            params[key] = str(value).rstrip("/")
        else:
            assignments.append(f"{key} = :{key}")
            params[key] = value
    assignments.extend(["row_version = row_version + 1", "updated_at = now()"])
    result = await conn.execute(
        text(
            f"UPDATE model_provider_connections SET {', '.join(assignments)} "  # noqa: S608
            "WHERE id = :id AND row_version = :row_version"
        ),
        params,
    )
    if result.rowcount != 1:
        current = await get_connection(conn, connection_id)
        if current is None:
            raise LookupError("MODEL_CONNECTION_NOT_FOUND")
        raise RuntimeError("MODEL_CONFIG_VERSION_CONFLICT")
    updated = await get_connection(conn, connection_id)
    assert updated is not None
    return updated


async def upsert_credential(
    conn: AsyncConnection,
    *,
    connection_id: str,
    encrypted_payload: str,
    source: str = "database",
) -> int:
    result = await conn.execute(
        text(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
            "FROM model_connection_credentials WHERE connection_id = :id"
        ),
        {"id": connection_id},
    )
    version = int(result.scalar_one())
    credential_id = new_uuid()
    await conn.execute(
        text(
            """
            INSERT INTO model_connection_credentials(id,connection_id,version,encrypted_payload,state,source)
            VALUES (:id,:connection_id,:version,:payload,'pending',:source)
            """
        ),
        {
            "id": str(credential_id),
            "connection_id": connection_id,
            "version": version,
            "payload": encrypted_payload,
            "source": source,
        },
    )
    return version


async def activate_credential(conn: AsyncConnection, *, connection_id: str, version: int) -> None:
    await conn.execute(
        text("UPDATE model_connection_credentials SET state='retired' WHERE connection_id=:id AND state='active'"),
        {"id": connection_id},
    )
    result = await conn.execute(
        text(
            "UPDATE model_connection_credentials SET state='active', activated_at=now() "
            "WHERE connection_id=:id AND version=:version AND state='pending'"
        ),
        {"id": connection_id, "version": version},
    )
    if result.rowcount != 1:
        raise LookupError("MODEL_CREDENTIAL_VERSION_NOT_FOUND")


async def list_models(conn: AsyncConnection) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT m.*, c.name AS connection_name, c.driver, c.base_url, c.request_adapter, c.request_defaults
            FROM model_aliases m LEFT JOIN model_provider_connections c ON c.id = m.connection_id
            ORDER BY m.alias
            """
        )
    )
    return [dict(row) for row in result.mappings().all()]


async def get_model(conn: AsyncConnection, model_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT m.*, c.name AS connection_name, c.driver, c.base_url, c.request_adapter, c.request_defaults
            FROM model_aliases m LEFT JOIN model_provider_connections c ON c.id = m.connection_id
            WHERE m.id = :id
            """
        ),
        {"id": model_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def create_model(conn: AsyncConnection, payload: Mapping[str, Any]) -> dict[str, Any]:
    model_id = new_uuid()
    provider = payload.get("provider") or payload.get("driver")
    if provider is None and payload.get("connection_id"):
        connection = await conn.execute(
            text("SELECT driver FROM model_provider_connections WHERE id=:id"),
            {"id": payload["connection_id"]},
        )
        provider = connection.scalar()
    fields = {
        "id": str(model_id),
        "alias": payload["alias"],
        "provider": provider or "openai_compatible",
        "provider_model": payload["provider_model"],
        "operation": payload["operation"],
        "connection_id": payload.get("connection_id"),
        "input_modalities": _json(payload.get("input_modalities", ["text"])),
        "capabilities": _json(payload.get("capabilities", {})),
        "context_window_tokens": payload.get("context_window_tokens"),
        "max_output_tokens": payload.get("max_output_tokens"),
        "dimensions": payload.get("dimensions"),
        "tokenizer_contract": _json(payload.get("tokenizer_contract", {})),
        "model_defaults": _json(payload.get("model_defaults", {})),
        "thinking_capabilities": _json(payload.get("thinking_capabilities", {})),
        "startup_canary": _json(payload.get("startup_canary", {})),
    }
    await conn.execute(
        text(
            """
            INSERT INTO model_aliases
              (id,alias,provider,provider_model,operation,connection_id,input_modalities,capabilities,
               context_window_tokens,max_output_tokens,dimensions,tokenizer_contract,model_defaults,thinking_capabilities,startup_canary)
            VALUES (:id,:alias,:provider,:provider_model,:operation,:connection_id,
                    CAST(:input_modalities AS jsonb),CAST(:capabilities AS jsonb),
                    :context_window_tokens,:max_output_tokens,:dimensions,
                    CAST(:tokenizer_contract AS jsonb),CAST(:model_defaults AS jsonb),
                    CAST(:thinking_capabilities AS jsonb),CAST(:startup_canary AS jsonb))
            """
        ),
        fields,
    )
    created = await get_model(conn, str(model_id))
    assert created is not None
    return created


async def patch_model(
    conn: AsyncConnection, *, model_id: str, row_version: int, changes: Mapping[str, Any]
) -> dict[str, Any]:
    allowed = {
        "provider_model",
        "connection_id",
        "input_modalities",
        "capabilities",
        "context_window_tokens",
        "max_output_tokens",
        "dimensions",
        "tokenizer_contract",
        "model_defaults",
        "thinking_capabilities",
        "startup_canary",
        "is_enabled",
    }
    changes = {key: value for key, value in changes.items() if key in allowed}
    if changes.get("is_enabled") is False:
        referenced = await conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM model_stage_bindings WHERE model_alias="
                "(SELECT alias FROM model_aliases WHERE id=:id))"
            ),
            {"id": model_id},
        )
        if referenced.scalar_one():
            raise RuntimeError("MODEL_REFERENCED_CANNOT_DISABLE")
    if not changes:
        model = await get_model(conn, model_id)
        if model is None:
            raise LookupError("MODEL_NOT_FOUND")
        return model
    assignments: list[str] = []
    params: dict[str, Any] = {"id": model_id, "row_version": row_version}
    json_fields = {
        "input_modalities",
        "capabilities",
        "tokenizer_contract",
        "model_defaults",
        "thinking_capabilities",
        "startup_canary",
    }
    for key, value in changes.items():
        if key in json_fields:
            assignments.append(f"{key}=CAST(:{key} AS jsonb)")
            params[key] = _json(value)
        else:
            assignments.append(f"{key}=:{key}")
            params[key] = value
    assignments.extend(["row_version=row_version+1", "updated_at=now()"])
    result = await conn.execute(
        text(
            f"UPDATE model_aliases SET {', '.join(assignments)} WHERE id=:id AND row_version=:row_version"  # noqa: S608
        ),
    )
    if result.rowcount != 1:
        if await get_model(conn, model_id) is None:
            raise LookupError("MODEL_NOT_FOUND")
        raise RuntimeError("MODEL_CONFIG_VERSION_CONFLICT")
    model = await get_model(conn, model_id)
    assert model is not None
    return model


async def current_revision(conn: AsyncConnection, *, include_archived: bool = False) -> dict[str, Any] | None:
    clause = "" if include_archived else "WHERE status IN ('draft','validated','active')"
    result = await conn.execute(  # noqa: S608 - clause is a local constant, never user input.
        text(f"SELECT * FROM model_configuration_revisions {clause} ORDER BY revision DESC LIMIT 1")  # noqa: S608
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def active_revision(conn: AsyncConnection) -> dict[str, Any] | None:
    result = await conn.execute(text("SELECT * FROM model_configuration_revisions WHERE status='active' LIMIT 1"))
    row = result.mappings().first()
    return dict(row) if row else None


async def get_revision(conn: AsyncConnection, revision_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text("SELECT * FROM model_configuration_revisions WHERE id=:id"),
        {"id": revision_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def save_draft(
    conn: AsyncConnection,
    *,
    snapshot: Mapping[str, Any],
    revision_id: str | None,
    expected_row_version: int | None,
    actor_user_id: str | None,
) -> dict[str, Any]:
    snapshot_dict = await _freeze_aliases(conn, snapshot)
    digest = config_hash(snapshot_dict)
    if revision_id:
        params = {
            "id": revision_id,
            "hash": digest,
            "snapshot": _json(snapshot_dict),
            "actor": actor_user_id,
            "expected": expected_row_version,
        }
        result = await conn.execute(
            text(
                "UPDATE model_configuration_revisions SET status='draft', "
                "config_hash=:hash, resolved_snapshot=CAST(:snapshot AS jsonb), "
                "created_by=COALESCE(:actor,created_by), updated_at=now(), "
                "row_version=row_version+1 "
                "WHERE id=:id AND status IN ('draft','validated') "
                "AND (:expected IS NULL OR row_version=:expected)"
            ),
            params,
        )
        if result.rowcount != 1:
            raise RuntimeError("MODEL_CONFIG_VERSION_CONFLICT")
        result = await conn.execute(
            text("SELECT * FROM model_configuration_revisions WHERE id=:id"), {"id": revision_id}
        )
        updated_row = result.mappings().one()
        await _replace_stage_bindings(conn, revision_id=revision_id, snapshot=snapshot_dict)
        return dict(updated_row)
    result = await conn.execute(text("SELECT COALESCE(MAX(revision),0)+1 FROM model_configuration_revisions"))
    revision = int(result.scalar_one())
    new_id = new_uuid()
    await conn.execute(
        text(
            "INSERT INTO model_configuration_revisions(id,revision,status,config_hash,resolved_snapshot,created_by) "
            "VALUES (:id,:revision,'draft',:hash,CAST(:snapshot AS jsonb),:actor)"
        ),
        {
            "id": str(new_id),
            "revision": revision,
            "hash": digest,
            "snapshot": _json(snapshot_dict),
            "actor": actor_user_id,
        },
    )
    created_revision = await current_revision(conn)
    assert created_revision is not None
    await _replace_stage_bindings(conn, revision_id=str(new_id), snapshot=snapshot_dict)
    return created_revision


async def _replace_stage_bindings(conn: AsyncConnection, *, revision_id: str, snapshot: Mapping[str, Any]) -> None:
    stages = snapshot.get("stages") or {}
    await conn.execute(
        text("DELETE FROM model_stage_bindings WHERE revision_id=:revision_id"),
        {"revision_id": revision_id},
    )
    for stage_key, binding in stages.items():
        if not isinstance(binding, Mapping):
            continue
        alias = binding.get("model_alias") or binding.get("alias")
        if not alias:
            continue
        await conn.execute(
            text(
                "INSERT INTO model_stage_bindings(id,revision_id,stage_key,model_alias,"
                "parameter_overrides,token_policy,thinking_policy) "
                "VALUES (:id,:revision_id,:stage_key,:alias,CAST(:parameters AS jsonb),"
                "CAST(:token_policy AS jsonb),CAST(:thinking AS jsonb))"
            ),
            {
                "id": str(new_uuid()),
                "revision_id": revision_id,
                "stage_key": str(stage_key),
                "alias": str(alias),
                "parameters": _json(binding.get("parameter_overrides") or binding.get("parameters") or {}),
                "token_policy": _json(binding.get("token_policy") or {}),
                "thinking": _json(binding.get("thinking_policy") or binding.get("thinking") or {}),
            },
        )


async def mark_validated(
    conn: AsyncConnection, *, revision_id: str, config_hash_value: str, report: Mapping[str, Any]
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            "UPDATE model_configuration_revisions SET status='validated', validation_report=CAST(:report AS jsonb), "
            "validated_at=now(), updated_at=now() WHERE id=:id AND status='draft' AND config_hash=:hash"
        ),
        {"id": revision_id, "hash": config_hash_value, "report": _json(report)},
    )
    if result.rowcount != 1:
        raise RuntimeError("MODEL_VALIDATION_STALE")
    row = await conn.execute(text("SELECT * FROM model_configuration_revisions WHERE id=:id"), {"id": revision_id})
    return dict(row.mappings().one())


async def activate_revision(conn: AsyncConnection, *, revision_id: str, config_hash_value: str) -> dict[str, Any]:
    result = await conn.execute(
        text(
            "SELECT * FROM model_configuration_revisions WHERE id=:id "
            "AND status='validated' AND config_hash=:hash FOR UPDATE"
        ),
        {"id": revision_id, "hash": config_hash_value},
    )
    revision = result.mappings().first()
    if revision is None:
        raise RuntimeError("MODEL_VALIDATION_STALE")
    snapshot = dict(revision.get("resolved_snapshot") or {})
    aliases = snapshot.get("aliases")
    stages = snapshot.get("stages") or {}
    if not isinstance(aliases, dict) or not isinstance(stages, dict):
        raise RuntimeError("MODEL_REVISION_SNAPSHOT_INCOMPLETE")
    required = {"provider", "provider_model", "operation", "connection_id", "request_adapter", "request_defaults"}
    for binding in stages.values():
        if not isinstance(binding, Mapping):
            raise RuntimeError("MODEL_REVISION_SNAPSHOT_INCOMPLETE")
        alias = str(binding.get("model_alias") or binding.get("alias") or "")
        contract = aliases.get(alias)
        if not alias or not isinstance(contract, Mapping) or not required.issubset(contract):
            raise RuntimeError("MODEL_REVISION_SNAPSHOT_INCOMPLETE")
    await conn.execute(
        text("UPDATE model_configuration_revisions SET status='archived', updated_at=now() WHERE status='active'")
    )
    await conn.execute(
        text(
            "UPDATE model_configuration_revisions SET status='active', "
            "activated_at=now(), updated_at=now() WHERE id=:id"
        ),
        {"id": revision_id},
    )
    active = await active_revision(conn)
    assert active is not None
    return active


async def record_validation_run(
    conn: AsyncConnection,
    *,
    target_type: str,
    target_id: str,
    config_hash_value: str,
    status: str,
    safe_error_code: str | None = None,
    measurements: Mapping[str, Any] | None = None,
) -> uuid.UUID:
    validation_id = new_uuid()
    await conn.execute(
        text(
            "INSERT INTO model_validation_runs(id,target_type,target_id,config_hash,status,"
            "safe_error_code,measurements) "
            "VALUES (:id,:target_type,:target_id,:hash,:status,:error,CAST(:measurements AS jsonb))"
        ),
        {
            "id": str(validation_id),
            "target_type": target_type,
            "target_id": target_id,
            "hash": config_hash_value,
            "status": status,
            "error": safe_error_code,
            "measurements": _json(measurements),
        },
    )
    return validation_id
