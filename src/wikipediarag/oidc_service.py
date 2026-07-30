from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.auth import AuthenticationMethod, PlatformRole
from wikipediarag.auth_service import (
    AuthenticationError,
    create_session,
    hash_secret,
    new_opaque_token,
    read_secret_file,
)
from wikipediarag.config import Settings
from wikipediarag.db import json_dumps
from wikipediarag.ids import new_uuid, stable_hash

_DEVELOPMENT_APP_SECRET = "development-local-app-secret-change-before-production"  # noqa: S105


@dataclass(frozen=True, slots=True)
class OidcProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None = None


@dataclass(frozen=True, slots=True)
class OidcStart:
    authorization_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OidcLoginResult:
    user_id: str
    username: str | None
    display_name: str | None
    platform_role: PlatformRole
    password_change_required: bool
    session_id: str
    session_token: str
    expires_at: datetime
    active_tenant_id: str | None


def oidc_login_enabled(settings: Settings) -> bool:
    return settings.auth_mode in {"oidc", "hybrid"}


def load_app_secret(settings: Settings) -> bytes:
    secret = read_secret_file(settings.app_secret_file)
    if secret is None:
        if settings.app_env in {"development", "test"}:
            secret = _DEVELOPMENT_APP_SECRET
        else:
            raise AuthenticationError("APP_SECRET_REQUIRED", "application secret file is required", status_code=500)
    return secret.encode("utf-8")


def derive_pkce_verifier(secret: bytes, state: str) -> str:
    digest = hmac.new(secret, state.encode("utf-8"), hashlib.sha256).digest()
    return _b64url(digest)


def pkce_s256_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def encrypt_server_tokens(settings: Settings, payload: dict[str, Any]) -> dict[str, str]:
    key = hashlib.sha256(load_app_secret(settings)).digest()
    nonce = hashlib.sha256(new_opaque_token().encode("utf-8")).digest()[:12]
    ciphertext = AESGCM(key).encrypt(nonce, json_dumps(payload).encode("utf-8"), None)
    return {
        "v": "aesgcm-v1",
        "nonce": _b64url(nonce),
        "ciphertext": _b64url(ciphertext),
    }


def decrypt_server_tokens(settings: Settings, payload: dict[str, str]) -> dict[str, Any]:
    if payload.get("v") != "aesgcm-v1":
        raise AuthenticationError("TOKEN_STORAGE_INVALID", "server-side token storage is invalid", status_code=500)
    key = hashlib.sha256(load_app_secret(settings)).digest()
    nonce = _b64url_decode(payload["nonce"])
    ciphertext = _b64url_decode(payload["ciphertext"])
    raw = AESGCM(key).decrypt(nonce, ciphertext, None)
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise AuthenticationError("TOKEN_STORAGE_INVALID", "server-side token storage is invalid", status_code=500)
    return decoded


async def fetch_oidc_metadata(settings: Settings, client: httpx.AsyncClient | None = None) -> OidcProviderMetadata:
    if not settings.oidc_discovery_url:
        raise AuthenticationError("OIDC_NOT_CONFIGURED", "OIDC discovery URL is not configured", status_code=500)
    close_client = client is None
    resolved_client = client or httpx.AsyncClient(timeout=10)
    try:
        response = await resolved_client.get(settings.oidc_discovery_url)
        response.raise_for_status()
        data = response.json()
    finally:
        if close_client:
            await resolved_client.aclose()
    metadata = OidcProviderMetadata(
        issuer=str(data["issuer"]),
        authorization_endpoint=str(data["authorization_endpoint"]),
        token_endpoint=str(data["token_endpoint"]),
        jwks_uri=str(data["jwks_uri"]),
        end_session_endpoint=str(data["end_session_endpoint"]) if data.get("end_session_endpoint") else None,
    )
    expected_issuer = settings.oidc_issuer or metadata.issuer
    if metadata.issuer != expected_issuer:
        raise AuthenticationError(
            "OIDC_ISSUER_INVALID",
            "OIDC issuer does not match configured issuer",
            status_code=502,
        )
    return metadata


async def start_oidc_flow(
    conn: AsyncConnection,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> OidcStart:
    if not oidc_login_enabled(settings):
        raise AuthenticationError("OIDC_LOGIN_DISABLED", "OIDC login is disabled", status_code=403)
    if not settings.oidc_client_id:
        raise AuthenticationError("OIDC_NOT_CONFIGURED", "OIDC client ID is not configured", status_code=500)
    metadata = await fetch_oidc_metadata(settings, client)
    state = new_opaque_token()
    nonce = new_opaque_token()
    verifier = derive_pkce_verifier(load_app_secret(settings), state)
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    await conn.execute(
        text(
            """
            INSERT INTO auth_oidc_flows(
              id, state_hash, nonce_hash, code_verifier_hash, redirect_uri, expires_at
            )
            VALUES (
              :id, :state_hash, :nonce_hash, :code_verifier_hash, :redirect_uri, :expires_at
            )
            """
        ),
        {
            "id": str(new_uuid()),
            "state_hash": hash_secret(state),
            "nonce_hash": hash_secret(nonce),
            "code_verifier_hash": hash_secret(verifier),
            "redirect_uri": settings.oidc_redirect_uri,
            "expires_at": expires_at,
        },
    )
    query_params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_s256_challenge(verifier),
        "code_challenge_method": "S256",
    }
    if settings.oidc_scope:
        query_params["scope"] = settings.oidc_scope
    query = urlencode(query_params)
    return OidcStart(
        authorization_url=f"{metadata.authorization_endpoint}?{query}",
        expires_at=expires_at,
    )


async def complete_oidc_callback(
    conn: AsyncConnection,
    *,
    settings: Settings,
    code: str,
    state: str,
    client: httpx.AsyncClient | None = None,
) -> OidcLoginResult:
    metadata = await fetch_oidc_metadata(settings, client)
    flow = await _consume_oidc_flow(conn, state=state)
    verifier = derive_pkce_verifier(load_app_secret(settings), state)
    if hash_secret(verifier) != flow["code_verifier_hash"]:
        raise AuthenticationError("OIDC_PKCE_INVALID", "OIDC PKCE verifier is invalid", status_code=400)
    token_response = await _exchange_code_for_tokens(
        settings=settings,
        metadata=metadata,
        code=code,
        code_verifier=verifier,
        client=client,
    )
    id_token = token_response.get("id_token")
    if not isinstance(id_token, str):
        raise AuthenticationError("OIDC_ID_TOKEN_MISSING", "OIDC ID token is missing", status_code=502)
    claims = await validate_id_token(
        id_token,
        settings=settings,
        metadata=metadata,
        expected_nonce_hash=str(flow["nonce_hash"]),
        client=client,
    )
    user = await _upsert_oidc_user(conn, settings=settings, issuer=metadata.issuer, claims=claims)
    server_tokens = encrypt_server_tokens(
        settings,
        {
            "issuer": metadata.issuer,
            "subject": str(claims[settings.oidc_claim_sub]),
            "access_token": token_response.get("access_token"),
            "refresh_token": token_response.get("refresh_token"),
            "token_type": token_response.get("token_type"),
            "expires_in": token_response.get("expires_in"),
        },
    )
    active_tenant_id = (
        None if user["platform_role"] == PlatformRole.platform_admin else await _default_user_tenant(conn, user["id"])
    )
    created = await create_session(
        conn,
        user_id=user["id"],
        authentication_method=AuthenticationMethod.oidc,
        settings=settings,
        active_tenant_id=active_tenant_id,
        server_side_tokens=server_tokens,
    )
    if active_tenant_id is not None:
        await sync_oidc_group_memberships(
            conn,
            tenant_id=active_tenant_id,
            user_id=user["id"],
            group_paths=_claim_values(claims, settings.oidc_claim_groups),
        )
    return OidcLoginResult(
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        platform_role=user["platform_role"],
        password_change_required=False,
        session_id=created.session_id,
        session_token=created.session_token,
        expires_at=created.expires_at,
        active_tenant_id=active_tenant_id,
    )


async def validate_id_token(
    id_token: str,
    *,
    settings: Settings,
    metadata: OidcProviderMetadata,
    expected_nonce_hash: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    header = jwt.get_unverified_header(id_token)
    algorithm = str(header.get("alg", ""))
    if not algorithm or algorithm.lower() == "none":
        raise AuthenticationError("OIDC_SIGNATURE_INVALID", "OIDC ID token signature is invalid", status_code=401)
    key = await _load_jwk_key(metadata.jwks_uri, str(header.get("kid", "")), client=client)
    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=[algorithm],
            audience=settings.oidc_client_id,
            issuer=metadata.issuer,
            options={"require": ["exp", "iat", settings.oidc_claim_sub]},
        )
    except jwt.InvalidIssuerError as exc:
        raise AuthenticationError("OIDC_ISSUER_INVALID", "OIDC issuer is invalid", status_code=401) from exc
    except jwt.InvalidAudienceError as exc:
        raise AuthenticationError("OIDC_AUDIENCE_INVALID", "OIDC audience is invalid", status_code=401) from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError(
            "OIDC_SIGNATURE_INVALID",
            "OIDC ID token signature is invalid",
            status_code=401,
        ) from exc
    if hash_secret(str(claims.get("nonce", ""))) != expected_nonce_hash:
        raise AuthenticationError("OIDC_NONCE_INVALID", "OIDC nonce is invalid", status_code=401)
    return dict(claims)


async def _consume_oidc_flow(conn: AsyncConnection, *, state: str) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            DELETE FROM auth_oidc_flows
            WHERE state_hash = :state_hash AND expires_at > now()
            RETURNING nonce_hash, code_verifier_hash, redirect_uri
            """
        ),
        {"state_hash": hash_secret(state)},
    )
    row = result.mappings().first()
    if row is None:
        raise AuthenticationError("OIDC_STATE_INVALID", "OIDC state is invalid or expired", status_code=400)
    return dict(row)


async def _exchange_code_for_tokens(
    *,
    settings: Settings,
    metadata: OidcProviderMetadata,
    code: str,
    code_verifier: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    client_secret = read_secret_file(settings.oidc_client_secret_file)
    if client_secret is None:
        raise AuthenticationError("OIDC_CLIENT_SECRET_REQUIRED", "OIDC client secret is required", status_code=500)
    close_client = client is None
    resolved_client = client or httpx.AsyncClient(timeout=10)
    try:
        response = await resolved_client.post(
            metadata.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.oidc_redirect_uri,
                "client_id": settings.oidc_client_id,
                "client_secret": client_secret,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if close_client:
            await resolved_client.aclose()
    if not isinstance(payload, dict):
        raise AuthenticationError("OIDC_TOKEN_RESPONSE_INVALID", "OIDC token response is invalid", status_code=502)
    return payload


async def _load_jwk_key(jwks_uri: str, kid: str, *, client: httpx.AsyncClient | None = None) -> Any:
    close_client = client is None
    resolved_client = client or httpx.AsyncClient(timeout=10)
    try:
        response = await resolved_client.get(jwks_uri)
        response.raise_for_status()
        jwks = response.json()
    finally:
        if close_client:
            await resolved_client.aclose()
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list):
        raise AuthenticationError("OIDC_JWKS_INVALID", "OIDC JWKS is invalid", status_code=502)
    for key_data in keys:
        if isinstance(key_data, dict) and str(key_data.get("kid", "")) == kid:
            return jwt.PyJWK.from_dict(key_data).key
    raise AuthenticationError("OIDC_JWKS_KEY_NOT_FOUND", "OIDC signing key was not found", status_code=401)


async def _upsert_oidc_user(
    conn: AsyncConnection,
    *,
    settings: Settings,
    issuer: str,
    claims: dict[str, Any],
) -> dict[str, Any]:
    subject = str(claims[settings.oidc_claim_sub])
    identity_key = f"{issuer}:{subject}"
    existing = await conn.execute(
        text(
            """
            SELECT u.id, u.username, u.display_name, u.platform_role
            FROM auth_identities ai
            JOIN users u ON u.id = ai.user_id
            WHERE ai.issuer = :issuer AND ai.subject = :subject
            """
        ),
        {"issuer": issuer, "subject": subject},
    )
    row = existing.mappings().first()
    profile = _profile_from_claims(settings, claims)
    if row is not None:
        await conn.execute(
            text(
                """
                UPDATE auth_identities
                SET username = :username,
                    email = :email,
                    claims = CAST(:claims AS jsonb),
                    updated_at = now()
                WHERE issuer = :issuer AND subject = :subject
                """
            ),
            {
                "issuer": issuer,
                "subject": subject,
                "username": profile["username"],
                "email": profile["email"],
                "claims": json_dumps(_safe_claims(settings, claims)),
            },
        )
        return {
            "id": str(row["id"]),
            "username": str(row["username"]) if row["username"] is not None else None,
            "display_name": str(row["display_name"]) if row["display_name"] is not None else None,
            "platform_role": PlatformRole(row["platform_role"]),
        }

    if not _auto_provision_allowed(settings, claims):
        raise AuthenticationError("OIDC_PROVISIONING_DENIED", "OIDC auto-provisioning denied", status_code=403)

    user_id = str(new_uuid())
    username = await _unique_username(conn, profile["username"] or f"oidc-{stable_hash([identity_key], 10)}")
    platform_role = _mapped_platform_role(settings, claims)
    await conn.execute(
        text(
            """
            INSERT INTO users(id, email, username, display_name, platform_role, password_hash, password_change_required)
            VALUES (:id, NULL, :username, :display_name, :platform_role, NULL, false)
            """
        ),
        {
            "id": user_id,
            "username": username,
            "display_name": profile["display_name"] or username,
            "platform_role": platform_role.value,
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO auth_identities(
              id, user_id, issuer, subject, identity_key, provider_type, username, email, claims
            )
            VALUES (
              :id, :user_id, :issuer, :subject, :identity_key, 'OIDC', :username, :email, CAST(:claims AS jsonb)
            )
            """
        ),
        {
            "id": str(new_uuid()),
            "user_id": user_id,
            "issuer": issuer,
            "subject": subject,
            "identity_key": identity_key,
            "username": profile["username"],
            "email": profile["email"],
            "claims": json_dumps(_safe_claims(settings, claims)),
        },
    )
    return {
        "id": user_id,
        "username": username,
        "display_name": profile["display_name"] or username,
        "platform_role": platform_role,
    }


async def sync_oidc_group_memberships(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    user_id: str,
    group_paths: set[str],
) -> None:
    await conn.execute(
        text(
            """
            DELETE FROM group_memberships gm
            USING groups g
            WHERE gm.group_id = g.id
              AND g.tenant_id = :tenant_id
              AND gm.user_id = :user_id
              AND gm.membership_type = 'OIDC'
            """
        ),
        {"tenant_id": tenant_id, "user_id": user_id},
    )
    for group_path in sorted(path for path in group_paths if path.startswith("/")):
        group_id = str(new_uuid())
        await conn.execute(
            text(
                """
                INSERT INTO groups(id, tenant_id, name, group_type, external_id)
                VALUES (:id, :tenant_id, :name, 'OIDC', :external_id)
                ON CONFLICT (tenant_id, group_type, external_id) DO NOTHING
                """
            ),
            {"id": group_id, "tenant_id": tenant_id, "name": group_path, "external_id": group_path},
        )
        result = await conn.execute(
            text(
                """
                SELECT id
                FROM groups
                WHERE tenant_id = :tenant_id AND group_type = 'OIDC' AND external_id = :external_id
                """
            ),
            {"tenant_id": tenant_id, "external_id": group_path},
        )
        row = result.mappings().first()
        if row is None:
            continue
        await conn.execute(
            text(
                """
                INSERT INTO group_memberships(group_id, user_id, membership_type)
                VALUES (:group_id, :user_id, 'OIDC')
                ON CONFLICT (group_id, user_id, membership_type) DO NOTHING
                """
            ),
            {"group_id": row["id"], "user_id": user_id},
        )


async def _unique_username(conn: AsyncConnection, preferred: str) -> str:
    cleaned = preferred[:180] or "oidc-user"
    result = await conn.execute(text("SELECT 1 FROM users WHERE username = :username"), {"username": cleaned})
    if result.first() is None:
        return cleaned
    return f"{cleaned[:160]}-{stable_hash([cleaned, new_uuid()], 10)}"


async def _default_user_tenant(conn: AsyncConnection, user_id: str) -> str | None:
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


def _profile_from_claims(settings: Settings, claims: dict[str, Any]) -> dict[str, str | None]:
    return {
        "username": _claim_string(claims, settings.oidc_claim_username),
        "display_name": _claim_string(claims, settings.oidc_claim_name),
        "email": _claim_string(claims, settings.oidc_claim_email),
    }


def _claim_string(claims: dict[str, Any], path: str) -> str | None:
    value = _claim_value(claims, path)
    return str(value) if value not in {None, ""} else None


def _claim_values(claims: dict[str, Any], path: str) -> set[str]:
    value = _claim_value(claims, path)
    if isinstance(value, list):
        return {str(item) for item in value}
    if isinstance(value, str):
        return {value}
    return set()


def _claim_value(claims: dict[str, Any], path: str) -> Any:
    current: Any = claims
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _auto_provision_allowed(settings: Settings, claims: dict[str, Any]) -> bool:
    domains = _csv_set(settings.oidc_auto_provision_domains)
    groups = _csv_set(settings.oidc_auto_provision_groups)
    roles = _csv_set(settings.oidc_auto_provision_roles)
    if not domains and not groups and not roles:
        return True
    email = _claim_string(claims, settings.oidc_claim_email) or ""
    email_domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    claim_groups = _claim_values(claims, settings.oidc_claim_groups)
    claim_roles = _claim_values(claims, settings.oidc_claim_realm_roles)
    return bool(
        (domains and email_domain in domains)
        or (groups and bool(groups & claim_groups))
        or (roles and bool(roles & claim_roles))
    )


def _mapped_platform_role(settings: Settings, claims: dict[str, Any]) -> PlatformRole:
    admin_roles = _csv_set(settings.oidc_platform_admin_roles)
    if admin_roles and admin_roles & _claim_values(claims, settings.oidc_claim_realm_roles):
        return PlatformRole.platform_admin
    return PlatformRole.user


def _safe_claims(settings: Settings, claims: dict[str, Any]) -> dict[str, Any]:
    keys = {
        settings.oidc_claim_sub,
        settings.oidc_claim_username,
        settings.oidc_claim_name,
        settings.oidc_claim_email,
        settings.oidc_claim_email_verified,
        settings.oidc_claim_groups,
        settings.oidc_claim_realm_roles,
    }
    safe: dict[str, Any] = {}
    for key in keys:
        top_level = key.split(".", 1)[0]
        if top_level in claims:
            safe[top_level] = claims[top_level]
    return safe


def _b64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _b64url_decode(payload: str) -> bytes:
    padding = "=" * ((4 - len(payload) % 4) % 4)
    return base64.urlsafe_b64decode(f"{payload}{padding}".encode("ascii"))
