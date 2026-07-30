from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from argon2.low_level import Type
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.auth import ActorContext, AuthenticationMethod, PlatformRole, TenantRole
from wikipediarag.config import Settings
from wikipediarag.db import json_dumps
from wikipediarag.ids import new_uuid

LOCAL_ISSUER = "local"
_PASSWORD_HASHER = PasswordHasher(type=Type.ID)


@dataclass(frozen=True, slots=True)
class CreatedSession:
    session_id: str
    session_token: str
    csrf_token: str
    expires_at: datetime
    idle_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: str
    username: str | None
    display_name: str | None
    platform_role: PlatformRole
    password_change_required: bool


class AuthenticationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash.startswith("$argon2id$"):
        return False
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, password))
    except VerifyMismatchError:
        return False


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def local_login_enabled(settings: Settings) -> bool:
    return settings.auth_mode in {"local", "hybrid"}


def read_secret_file(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


async def ensure_bootstrap_admin(conn: AsyncConnection, settings: Settings) -> bool:
    if settings.auth_mode not in {"local", "hybrid"}:
        return False
    existing_admin = await conn.execute(text("SELECT 1 FROM users WHERE platform_role = 'PLATFORM_ADMIN' LIMIT 1"))
    if existing_admin.first() is not None:
        return False

    password = read_secret_file(settings.bootstrap_admin_password_file)
    if password is None:
        return False

    user_id = str(new_uuid())
    password_hash = hash_password(password)
    result = await conn.execute(
        text(
            """
            SELECT id, password_hash
            FROM users
            WHERE username = :username
            """
        ),
        {"username": settings.bootstrap_admin_username},
    )
    existing_user = result.mappings().first()
    if existing_user is None:
        await conn.execute(
            text(
                """
                INSERT INTO users(
                  id, email, username, display_name, platform_role,
                  password_hash, password_change_required, is_disabled
                )
                VALUES (
                  :id, :email, :username, :display_name, 'PLATFORM_ADMIN',
                  :password_hash, true, false
                )
                """
            ),
            {
                "id": user_id,
                "email": settings.bootstrap_admin_email or None,
                "username": settings.bootstrap_admin_username,
                "display_name": settings.bootstrap_admin_username,
                "password_hash": password_hash,
            },
        )
    else:
        user_id = str(existing_user["id"])
        assignments = ["platform_role = 'PLATFORM_ADMIN'", "password_change_required = true", "updated_at = now()"]
        params: dict[str, Any] = {"id": user_id}
        if existing_user["password_hash"] is None:
            assignments.append("password_hash = :password_hash")
            params["password_hash"] = password_hash
        await conn.execute(
            text(f"UPDATE users SET {', '.join(assignments)} WHERE id = :id"),  # noqa: S608
            params,
        )

    await conn.execute(
        text(
            """
            INSERT INTO auth_identities(id, user_id, issuer, subject, identity_key, provider_type, username, email)
            VALUES (
              :id, :user_id, :issuer, :subject, :identity_key, 'LOCAL', :username, :email
            )
            ON CONFLICT (issuer, subject) DO NOTHING
            """
        ),
        {
            "id": str(new_uuid()),
            "user_id": user_id,
            "issuer": LOCAL_ISSUER,
            "subject": settings.bootstrap_admin_username,
            "identity_key": f"{LOCAL_ISSUER}:{settings.bootstrap_admin_username}",
            "username": settings.bootstrap_admin_username,
            "email": settings.bootstrap_admin_email or None,
        },
    )
    return True


async def authenticate_local_user(
    conn: AsyncConnection,
    *,
    username: str,
    password: str,
) -> AuthenticatedUser:
    result = await conn.execute(
        text(
            """
            SELECT id, username, display_name, platform_role, password_hash,
                   password_change_required, is_disabled
            FROM users
            WHERE username = :username
            """
        ),
        {"username": username},
    )
    row = result.mappings().first()
    if row is None or row["password_hash"] is None or not verify_password(str(row["password_hash"]), password):
        raise AuthenticationError("INVALID_LOCAL_LOGIN", "invalid username or password")
    if bool(row["is_disabled"]):
        raise AuthenticationError("USER_DISABLED", "user is disabled", status_code=403)
    return AuthenticatedUser(
        user_id=str(row["id"]),
        username=str(row["username"]) if row["username"] is not None else None,
        display_name=str(row["display_name"]) if row["display_name"] is not None else None,
        platform_role=PlatformRole(row["platform_role"]),
        password_change_required=bool(row["password_change_required"]),
    )


async def create_session(
    conn: AsyncConnection,
    *,
    user_id: str,
    authentication_method: AuthenticationMethod,
    settings: Settings,
    remember_me: bool = False,
    active_tenant_id: str | None = None,
    server_side_tokens: dict[str, Any] | None = None,
) -> CreatedSession:
    now = datetime.now(UTC)
    max_seconds = settings.remember_me_max_seconds if remember_me else settings.session_max_seconds
    idle_seconds = settings.remember_me_idle_seconds if remember_me else settings.session_idle_seconds
    session_token = new_opaque_token()
    csrf_token = new_opaque_token()
    session_id = str(new_uuid())
    expires_at = now + timedelta(seconds=max_seconds)
    idle_expires_at = now + timedelta(seconds=idle_seconds)
    await conn.execute(
        text(
            """
            INSERT INTO auth_sessions(
              id, user_id, session_token_hash, csrf_token_hash, active_tenant_id,
              authentication_method, server_side_tokens, expires_at, idle_expires_at
            )
            VALUES (
              :id, :user_id, :session_token_hash, :csrf_token_hash, :active_tenant_id,
              :authentication_method, CAST(:server_side_tokens AS jsonb), :expires_at, :idle_expires_at
            )
            """
        ),
        {
            "id": session_id,
            "user_id": user_id,
            "session_token_hash": hash_secret(session_token),
            "csrf_token_hash": hash_secret(csrf_token),
            "active_tenant_id": active_tenant_id,
            "authentication_method": authentication_method.value,
            "server_side_tokens": json_dumps(server_side_tokens or {}),
            "expires_at": expires_at,
            "idle_expires_at": idle_expires_at,
        },
    )
    return CreatedSession(session_id, session_token, csrf_token, expires_at, idle_expires_at)


async def load_actor_for_session(
    conn: AsyncConnection,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> ActorContext | None:
    now = datetime.now(UTC)
    result = await conn.execute(
        text(
            """
            SELECT s.id AS session_id, s.user_id, s.active_tenant_id, s.authentication_method,
                   u.platform_role, tm.role AS tenant_role
            FROM auth_sessions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN tenant_memberships tm
              ON tm.user_id = s.user_id AND tm.tenant_id = s.active_tenant_id
            WHERE s.session_token_hash = :session_token_hash
              AND s.revoked_at IS NULL
              AND s.expires_at > :now
              AND s.idle_expires_at > :now
              AND u.is_disabled = false
            """
        ),
        {"session_token_hash": hash_secret(session_token), "now": now},
    )
    row = result.mappings().first()
    if row is None:
        return None
    await conn.execute(
        text("UPDATE auth_sessions SET last_seen_at = now(), updated_at = now() WHERE id = :id"),
        {"id": row["session_id"]},
    )
    return ActorContext(
        user_id=str(row["user_id"]),
        platform_role=PlatformRole(row["platform_role"]),
        active_tenant_id=str(row["active_tenant_id"]) if row["active_tenant_id"] is not None else None,
        tenant_role=TenantRole(row["tenant_role"]) if row["tenant_role"] is not None else None,
        session_id=str(row["session_id"]),
        authentication_method=AuthenticationMethod(row["authentication_method"]),
        request_id=request_id,
        trace_id=trace_id,
    )


async def rotate_session_token(
    conn: AsyncConnection,
    *,
    session_id: str,
) -> str:
    session_token = new_opaque_token()
    await conn.execute(
        text(
            """
            UPDATE auth_sessions
            SET session_token_hash = :session_token_hash,
                rotation_counter = rotation_counter + 1,
                updated_at = now()
            WHERE id = :id AND revoked_at IS NULL
            """
        ),
        {"id": session_id, "session_token_hash": hash_secret(session_token)},
    )
    return session_token


async def rotate_csrf_token(
    conn: AsyncConnection,
    *,
    session_id: str,
) -> str:
    csrf_token = new_opaque_token()
    await conn.execute(
        text(
            """
            UPDATE auth_sessions
            SET csrf_token_hash = :csrf_token_hash,
                updated_at = now()
            WHERE id = :id AND revoked_at IS NULL
            """
        ),
        {"id": session_id, "csrf_token_hash": hash_secret(csrf_token)},
    )
    return csrf_token


async def revoke_session(conn: AsyncConnection, *, session_id: str) -> None:
    await conn.execute(
        text("UPDATE auth_sessions SET revoked_at = now(), updated_at = now() WHERE id = :id"),
        {"id": session_id},
    )


async def select_active_tenant(
    conn: AsyncConnection,
    *,
    session_id: str,
    user_id: str,
    platform_role: PlatformRole,
    tenant_id: str,
) -> TenantRole | None:
    tenant = await conn.execute(text("SELECT id FROM tenants WHERE id = :id"), {"id": tenant_id})
    if tenant.first() is None:
        raise AuthenticationError("TENANT_NOT_FOUND", "tenant not found", status_code=404)
    tenant_role: TenantRole | None = None
    if platform_role != PlatformRole.platform_admin:
        membership = await conn.execute(
            text(
                """
                SELECT role
                FROM tenant_memberships
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                """
            ),
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        row = membership.mappings().first()
        if row is None:
            raise AuthenticationError("TENANT_ACCESS_DENIED", "tenant access denied", status_code=403)
        tenant_role = TenantRole(row["role"])
    await conn.execute(
        text("UPDATE auth_sessions SET active_tenant_id = :tenant_id, updated_at = now() WHERE id = :session_id"),
        {"tenant_id": tenant_id, "session_id": session_id},
    )
    return tenant_role


async def change_local_password(
    conn: AsyncConnection,
    *,
    user_id: str,
    current_password: str,
    new_password: str,
) -> None:
    result = await conn.execute(
        text("SELECT password_hash FROM users WHERE id = :id"),
        {"id": user_id},
    )
    row = result.mappings().first()
    if row is None or row["password_hash"] is None or not verify_password(str(row["password_hash"]), current_password):
        raise AuthenticationError("INVALID_CURRENT_PASSWORD", "invalid current password", status_code=403)
    await conn.execute(
        text(
            """
            UPDATE users
            SET password_hash = :password_hash,
                password_change_required = false,
                updated_at = now()
            WHERE id = :id
            """
        ),
        {"id": user_id, "password_hash": hash_password(new_password)},
    )


async def csrf_token_matches(conn: AsyncConnection, *, session_id: str, csrf_token: str) -> bool:
    result = await conn.execute(
        text("SELECT csrf_token_hash FROM auth_sessions WHERE id = :id AND revoked_at IS NULL"),
        {"id": session_id},
    )
    row = result.mappings().first()
    return row is not None and secrets.compare_digest(str(row["csrf_token_hash"]), hash_secret(csrf_token))


async def load_session_user(conn: AsyncConnection, *, user_id: str) -> AuthenticatedUser | None:
    result = await conn.execute(
        text(
            """
            SELECT id, username, display_name, platform_role, password_change_required
            FROM users
            WHERE id = :id AND is_disabled = false
            """
        ),
        {"id": user_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return AuthenticatedUser(
        user_id=str(row["id"]),
        username=str(row["username"]) if row["username"] is not None else None,
        display_name=str(row["display_name"]) if row["display_name"] is not None else None,
        platform_role=PlatformRole(row["platform_role"]),
        password_change_required=bool(row["password_change_required"]),
    )


def test_actor_context(settings: Settings, *, request_id: str, trace_id: str) -> ActorContext:
    return ActorContext(
        user_id=settings.default_user_id,
        platform_role=PlatformRole.user,
        active_tenant_id=settings.default_tenant_id,
        tenant_role=TenantRole.tenant_admin,
        session_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-session:{request_id}")),
        authentication_method=AuthenticationMethod.test,
        request_id=request_id,
        trace_id=trace_id,
    )
