from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from wikipediarag.auth_service import AuthenticationError, hash_secret
from wikipediarag.config import Settings
from wikipediarag.oidc_service import (
    OidcProviderMetadata,
    decrypt_server_tokens,
    derive_pkce_verifier,
    encrypt_server_tokens,
    pkce_s256_challenge,
    validate_id_token,
)


@pytest.mark.asyncio
async def test_validate_id_token_accepts_valid_fake_provider_token() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(app_env="test", auth_mode="test", oidc_client_id="rag-client")
    metadata = _metadata()
    nonce = "nonce-value"
    token = _id_token(key, issuer=metadata.issuer, audience=settings.oidc_client_id, nonce=nonce)

    async with _jwks_client(key) as client:
        claims = await validate_id_token(
            token,
            settings=settings,
            metadata=metadata,
            expected_nonce_hash=hash_secret(nonce),
            client=client,
        )

    assert claims["sub"] == "subject-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("issuer", "audience", "nonce", "expected_code"),
    [
        ("https://issuer.invalid/realms/rag", "rag-client", "nonce-value", "OIDC_ISSUER_INVALID"),
        ("https://issuer.test/realms/rag", "wrong-client", "nonce-value", "OIDC_AUDIENCE_INVALID"),
        ("https://issuer.test/realms/rag", "rag-client", "wrong-nonce", "OIDC_NONCE_INVALID"),
    ],
)
async def test_validate_id_token_rejects_invalid_claims(
    issuer: str,
    audience: str,
    nonce: str,
    expected_code: str,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(app_env="test", auth_mode="test", oidc_client_id="rag-client")
    token = _id_token(key, issuer=issuer, audience=audience, nonce=nonce)

    async with _jwks_client(key) as client:
        with pytest.raises(AuthenticationError) as exc_info:
            await validate_id_token(
                token,
                settings=settings,
                metadata=_metadata(),
                expected_nonce_hash=hash_secret("nonce-value"),
                client=client,
            )

    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
async def test_validate_id_token_rejects_invalid_signature() -> None:
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(app_env="test", auth_mode="test", oidc_client_id="rag-client")
    token = _id_token(signing_key, issuer=_metadata().issuer, audience=settings.oidc_client_id, nonce="nonce-value")

    async with _jwks_client(jwks_key) as client:
        with pytest.raises(AuthenticationError) as exc_info:
            await validate_id_token(
                token,
                settings=settings,
                metadata=_metadata(),
                expected_nonce_hash=hash_secret("nonce-value"),
                client=client,
            )

    assert exc_info.value.code == "OIDC_SIGNATURE_INVALID"


def test_pkce_verifier_is_deterministic_and_s256_challenge_is_not_plain() -> None:
    verifier = derive_pkce_verifier(b"app-secret", "state-value")
    challenge = pkce_s256_challenge(verifier)

    assert verifier == derive_pkce_verifier(b"app-secret", "state-value")
    assert challenge != verifier
    assert len(verifier) >= 43
    assert len(challenge) >= 43


def test_server_tokens_are_encrypted_and_decryptable() -> None:
    settings = Settings(app_env="test", auth_mode="test")
    payload = {"access_token": "access-value", "refresh_token": "refresh-value"}

    encrypted = encrypt_server_tokens(settings, payload)

    assert "access-value" not in json.dumps(encrypted)
    assert decrypt_server_tokens(settings, encrypted) == payload


def _metadata() -> OidcProviderMetadata:
    issuer = "https://issuer.test/realms/rag"
    return OidcProviderMetadata(
        issuer=issuer,
        authorization_endpoint=f"{issuer}/protocol/openid-connect/auth",
        token_endpoint=f"{issuer}/protocol/openid-connect/token",
        jwks_uri=f"{issuer}/protocol/openid-connect/certs",
    )


def _id_token(key: Any, *, issuer: str, audience: str, nonce: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": "subject-1",
            "preferred_username": "user1",
            "nonce": nonce,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        key,
        algorithm="RS256",
        headers={"kid": "fake-key-1"},
    )


def _jwks_client(key: Any) -> httpx.AsyncClient:
    public_jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    public_jwk["kid"] = "fake-key-1"
    public_jwk["alg"] = "RS256"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [public_jwk]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
