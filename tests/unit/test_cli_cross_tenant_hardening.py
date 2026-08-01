from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import wikipediarag.cli as cli


class _ProbeClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: int,
    ) -> httpx.Response:
        self.calls.append({"method": method, "url": url, "json": json, "timeout": timeout})
        return self.response


class _SessionClient:
    def __init__(self, session_payload: dict[str, Any]) -> None:
        self.session_payload = session_payload
        self.headers: dict[str, str] = {}
        self.posts: list[dict[str, Any]] = []

    def get(self, url: str, *, timeout: int) -> httpx.Response:
        request = httpx.Request("GET", url)
        return httpx.Response(200, request=request, json=self.session_payload)

    def post(self, url: str, *, json: dict[str, Any], timeout: int) -> httpx.Response:
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"csrf_token": "csrf-from-login"})


def _response(status_code: int, payload: dict[str, Any]) -> httpx.Response:
    request = httpx.Request("GET", "http://api.test/resource")
    return httpx.Response(status_code, request=request, json=payload)


def test_negative_probe_passes_only_for_expected_safe_rejections() -> None:
    client = _ProbeClient(
        _response(
            404,
            {
                "error": {
                    "code": "NOT_FOUND",
                    "message": "document not found",
                    "request_id": "req",
                    "details": {},
                }
            },
        )
    )

    result = cli._negative_probe(
        cast(httpx.Client, client),
        "GET",
        "http://api.test/documents/doc-a",
        "tenant_b_get_doc_a",
    )

    assert result["passed"] is True
    assert result["status_code"] == 404
    assert result["safe_payload"]["error"]["code"] == "NOT_FOUND"


def test_negative_probe_fails_when_payload_leaks_private_storage_fields() -> None:
    client = _ProbeClient(
        _response(
            404,
            {
                "error": {
                    "code": "NOT_FOUND",
                    "message": "document not found",
                    "details": {"object_key": "uploads/tenant-a/kb-a/private"},
                }
            },
        )
    )

    result = cli._negative_probe(
        cast(httpx.Client, client),
        "GET",
        "http://api.test/documents/doc-a",
        "tenant_b_get_doc_a",
    )

    assert result["passed"] is False


def test_presigned_upload_url_must_use_server_owned_tenant_and_kb_path() -> None:
    cli._assert_presigned_url_uses_tenant_kb_path(
        {
            "upload_url": (
                "http://localhost:9000/rag-artifacts/uploads/"
                "tenant-a/kb-a/0bdc24bb99e5cefe/checksum?X-Amz-Signature=test"
            )
        },
        "tenant-a",
        "kb-a",
    )


def test_presigned_upload_url_rejects_client_supplied_object_key() -> None:
    with pytest.raises(RuntimeError, match="server-owned tenant and KB path"):
        cli._assert_presigned_url_uses_tenant_kb_path(
            {"upload_url": "http://localhost:9000/rag-artifacts/uploads/client/supplied/key"},
            "tenant-a",
            "kb-a",
        )


def test_hardening_admin_secret_file_is_preferred(tmp_path: Path) -> None:
    secret_file = tmp_path / "admin_secret"
    secret_file.write_text("from-file\n", encoding="utf-8")
    args = argparse.Namespace(admin_secret_file=str(secret_file))

    assert cli._resolve_hardening_admin_secret(args) == "from-file"


def test_hardening_admin_secret_defaults_to_bootstrap_admin_password() -> None:
    args = argparse.Namespace(admin_secret_file=None)

    assert cli._resolve_hardening_admin_secret(args) == "admin"


def test_smoke_authentication_reuses_existing_session_csrf() -> None:
    client = _SessionClient({"authenticated": True, "csrf_token": "existing-csrf"})
    cli._authenticate_smoke_session(
        cast(httpx.Client, client),
        "http://api.test",
        username="admin",
        admin_secret="",
    )

    assert client.headers["X-CSRF-Token"] == "existing-csrf"
    assert client.posts == []


def test_upload_smokes_use_active_upload_retrieval_profile() -> None:
    constants = str(cli._run_hardening_chat.__code__.co_consts) + str(cli._verify_uploaded_retrieval.__code__.co_consts)

    assert "upload_sota_mvp" in constants
    assert "upload_mock" not in constants
