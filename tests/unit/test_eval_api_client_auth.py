from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import httpx
import pytest

from wikipediarag.eval.api_client import HttpEvalApiClient


class _FakeHttpClient:
    instances: list[_FakeHttpClient] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        _FakeHttpClient.instances.append(self)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(("POST", url, kwargs))
        request = httpx.Request("POST", url)
        if url.endswith("/api/v1/auth/local/login"):
            return httpx.Response(200, request=request, json={"authenticated": True})
        if url.endswith("/api/v1/search:debug"):
            return httpx.Response(200, request=request, json={"ok": True})
        return httpx.Response(404, request=request, json={})

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(("GET", url, kwargs))
        request = httpx.Request("GET", url)
        if url.endswith("/api/v1/auth/session"):
            return httpx.Response(200, request=request, json={"authenticated": True, "csrf_token": "csrf-token"})
        return httpx.Response(404, request=request, json={})


def test_eval_client_local_auth_reuses_cookie_session_and_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeHttpClient.instances = []
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    client = HttpEvalApiClient(auth_mode="local", auth_username="admin", auth_password="secret")  # noqa: S106

    first = client.run_search_debug(
        "question",
        api="http://api.test",
        top_k=10,
        retrieval_profile="sota_mvp",
        retrieval_overrides={},
    )
    second = client.run_search_debug(
        "question",
        api="http://api.test",
        top_k=10,
        retrieval_profile="sota_mvp",
        retrieval_overrides={},
    )

    assert first == {"ok": True}
    assert second == {"ok": True}
    fake = _FakeHttpClient.instances[0]
    login_calls = [call for call in fake.calls if call[1].endswith("/api/v1/auth/local/login")]
    debug_calls = [call for call in fake.calls if call[1].endswith("/api/v1/search:debug")]
    assert len(login_calls) == 1
    assert len(debug_calls) == 2
    assert login_calls[0][2]["json"] == {"username": "admin", "password": "secret"}
    assert debug_calls[0][2]["headers"] == {"X-CSRF-Token": "csrf-token"}


def test_eval_client_auth_none_does_not_login(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeHttpClient.instances = []
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    client = HttpEvalApiClient(auth_mode="none")

    client.run_search_debug(
        "question",
        api="http://api.test",
        top_k=10,
        retrieval_profile="sota_mvp",
        retrieval_overrides={},
    )

    fake = _FakeHttpClient.instances[0]
    assert not any(call[1].endswith("/api/v1/auth/local/login") for call in fake.calls)


def test_eval_client_local_auth_is_thread_safe_for_concurrent_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeHttpClient.instances = []
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    client = HttpEvalApiClient(auth_mode="local", auth_username="admin", auth_password="secret")  # noqa: S106
    barrier = Barrier(8)

    def run_one(index: int) -> dict[str, Any]:
        barrier.wait(timeout=5)
        return client.run_search_debug(
            f"question {index}",
            api="http://api.test",
            top_k=10,
            retrieval_profile="sota_mvp",
            retrieval_overrides={},
        )

    original_post = _FakeHttpClient.post

    def slow_post(self: _FakeHttpClient, url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/api/v1/auth/local/login"):
            time.sleep(0.05)
        return original_post(self, url, **kwargs)

    monkeypatch.setattr(_FakeHttpClient, "post", slow_post)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(run_one, range(8)))

    assert results == [{"ok": True}] * 8
    fake = _FakeHttpClient.instances[0]
    login_calls = [call for call in fake.calls if call[1].endswith("/api/v1/auth/local/login")]
    session_calls = [call for call in fake.calls if call[1].endswith("/api/v1/auth/session")]
    debug_calls = [call for call in fake.calls if call[1].endswith("/api/v1/search:debug")]
    assert len(_FakeHttpClient.instances) == 1
    assert len(login_calls) == 1
    assert len(session_calls) == 1
    assert len(debug_calls) == 8
    assert all(call[2]["headers"] == {"X-CSRF-Token": "csrf-token"} for call in debug_calls)
