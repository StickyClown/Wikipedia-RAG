from __future__ import annotations

import json
from threading import Lock
from typing import Any, Literal, Protocol

import httpx

from wikipediarag.config import Settings


class EvalApiClient(Protocol):
    def run_chat(
        self,
        question: str,
        *,
        api: str,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]: ...

    def url_ok(self, url: str) -> bool: ...


class RetrievalEvalApiClient(Protocol):
    def run_search_debug(
        self,
        question: str,
        *,
        api: str,
        top_k: int,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
    ) -> dict[str, Any]: ...


class HttpEvalApiClient:
    def __init__(
        self,
        *,
        timeout: float = 600,
        kiwix_public_base_url: str = "",
        kiwix_internal_base_url: str = "",
        auth_mode: Literal["none", "local"] = "none",
        auth_username: str = "admin",
        auth_password: str = "",
    ):
        self.timeout = timeout
        self.kiwix_public_base_url = kiwix_public_base_url.rstrip("/")
        self.kiwix_internal_base_url = kiwix_internal_base_url.rstrip("/")
        self.auth_mode = auth_mode
        self.auth_username = auth_username
        self.auth_password = auth_password
        self._url_cache: dict[str, bool] = {}
        self._client: httpx.Client | None = None
        self._authenticated_api = ""
        self._csrf_token = ""
        self._auth_lock = Lock()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        timeout: float = 600,
        include_kiwix_urls: bool = True,
    ) -> HttpEvalApiClient:
        return cls(
            timeout=timeout,
            kiwix_public_base_url=settings.kiwix_public_base_url if include_kiwix_urls else "",
            kiwix_internal_base_url=settings.kiwix_internal_base_url if include_kiwix_urls else "",
            auth_mode=settings.eval_auth_mode,
            auth_username=settings.eval_auth_username,
            auth_password=settings.eval_auth_password,
        )

    def run_chat(
        self,
        question: str,
        *,
        api: str,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        payload = {
            "message": question,
            "mode": mode,
            "stream": True,
            "retrieval_profile": retrieval_profile,
            "retrieval_overrides": retrieval_overrides,
        }
        client = self._api_client(api)
        with client.stream(
            "POST",
            f"{api.rstrip('/')}/api/v1/chat",
            json=payload,
            headers=self._unsafe_headers(),
        ) as response:
            response.raise_for_status()
            events = list(iter_sse(response.iter_lines()))
        started = _event_data(events, "run.started") or {}
        failed = [event for event in events if event["event"] == "run.failed"]
        if failed:
            failed_event = failed[-1]
            failed_data = dict(failed_event.get("data") or {})
            return {
                "events": events,
                "failed": True,
                "failed_event": failed_event,
                "error": str(failed_data.get("code") or failed_data.get("error") or "run.failed"),
                "query_run_id": failed_event.get("query_run_id") or started.get("query_run_id"),
                "trace_id": failed_data.get("trace_id")
                or (started.get("data", {}).get("trace_id") if isinstance(started.get("data"), dict) else None),
            }
        message = _event_data(events, "message.delta") or {}
        usage = _event_data(events, "usage.updated") or {}
        completed = _event_data(events, "run.completed") or {}
        return {
            "events": events,
            "failed": False,
            "query_run_id": started.get("query_run_id") or completed.get("query_run_id"),
            "trace_id": started.get("data", {}).get("trace_id") if isinstance(started.get("data"), dict) else None,
            "answer": completed.get("data", {}).get("answer")
            if isinstance(completed.get("data"), dict)
            else message.get("data", {}).get("text", ""),
            "message": message,
            "usage": usage,
        }

    def url_ok(self, url: str) -> bool:
        if url in self._url_cache:
            return self._url_cache[url]
        urls = [url]
        if self.kiwix_public_base_url and self.kiwix_internal_base_url and url.startswith(self.kiwix_public_base_url):
            urls.append(url.replace(self.kiwix_public_base_url, self.kiwix_internal_base_url, 1))
        ok = False
        for candidate in urls:
            try:
                with httpx.Client(timeout=10, follow_redirects=True) as client:
                    response = client.get(candidate)
                if response.status_code == 200:
                    ok = True
                    break
            except httpx.HTTPError:
                continue
        self._url_cache[url] = ok
        return ok

    def run_search_debug(
        self,
        question: str,
        *,
        api: str,
        top_k: int,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "message": question,
            "top_k": top_k,
            "retrieval_profile": retrieval_profile,
            "retrieval_overrides": retrieval_overrides,
        }
        client = self._api_client(api)
        response = client.post(
            f"{api.rstrip('/')}/api/v1/search:debug",
            json=payload,
            headers=self._unsafe_headers(),
        )
        response.raise_for_status()
        return dict(response.json())

    def _api_client(self, api: str) -> httpx.Client:
        if self._client is None:
            with self._auth_lock:
                if self._client is None:
                    self._client = httpx.Client(timeout=self.timeout)
        client = self._client
        if client is None:
            raise RuntimeError("eval API client was not initialized")
        normalized_api = api.rstrip("/")
        if self.auth_mode == "local" and self._authenticated_api != normalized_api:
            with self._auth_lock:
                if self._authenticated_api != normalized_api:
                    self._login_local(client, normalized_api)
        return client

    def _login_local(self, client: httpx.Client, api: str) -> None:
        login = client.post(
            f"{api}/api/v1/auth/local/login",
            json={"username": self.auth_username, "password": self.auth_password},
        )
        login.raise_for_status()
        session = client.get(f"{api}/api/v1/auth/session")
        session.raise_for_status()
        payload = session.json()
        if not payload.get("authenticated"):
            raise RuntimeError("eval local auth did not create an authenticated API session")
        self._csrf_token = str(payload.get("csrf_token") or "")
        self._authenticated_api = api

    def _unsafe_headers(self) -> dict[str, str]:
        if self._csrf_token:
            return {"X-CSRF-Token": self._csrf_token}
        return {}


def iter_sse(lines: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current_event: str | None = None
    current_data: str | None = None
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if not line:
            if current_event and current_data:
                payload = json.loads(current_data)
                events.append({"event": current_event, "data": payload})
            current_event = None
            current_data = None
            continue
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            current_data = line.removeprefix("data: ")
    return events


def _event_data(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for event in events:
        if event.get("event") == name and isinstance(event.get("data"), dict):
            return dict(event["data"])
    return None
