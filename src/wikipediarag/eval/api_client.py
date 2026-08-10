from __future__ import annotations

import json
from collections.abc import AsyncIterator
from hashlib import sha256
from threading import Lock
from typing import Any, Literal, Protocol

import httpx

from wikipediarag.config import Settings


class SseProtocolError(RuntimeError):
    """Safe terminal error for an invalid or truncated public SSE stream."""

    safe_code = "STREAM_PROTOCOL_ERROR"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        query_run_id: str | None = None,
        last_stage: str = "",
        last_sequence: int = 0,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.query_run_id = query_run_id
        self.last_stage = last_stage
        self.last_sequence = last_sequence
        self.events = list(events or [])


class EvalApiClient(Protocol):
    """Minimal synchronous contract shared by production and legacy eval clients."""

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
    """Minimal retrieval contract; Multi-KB is an optional client capability."""

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
        knowledge_base_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "message": question,
            "mode": mode,
            "stream": True,
            "retrieval_profile": retrieval_profile,
            "retrieval_overrides": retrieval_overrides,
            "client_request_id": _client_request_id(
                question=question,
                retrieval_profile=retrieval_profile,
                retrieval_overrides=retrieval_overrides,
                mode=mode,
                knowledge_base_ids=knowledge_base_ids,
            ),
        }
        if knowledge_base_ids:
            payload["knowledge_base_ids"] = list(knowledge_base_ids)
        client = self._api_client(api)
        with client.stream(
            "POST",
            f"{api.rstrip('/')}/api/v1/chat",
            json=payload,
            headers={**self._unsafe_headers(), "Idempotency-Key": str(payload["client_request_id"])},
        ) as response:
            response.raise_for_status()
            events = list(iter_sse(response.iter_lines()))
        started = _event_data(events, "run.started") or {}
        failed = [event for event in events if event["event"] in {"run.failed", "run.cancelled"}]
        if failed:
            failed_event = failed[-1]
            failed_data = _event_body(failed_event)
            envelope = _event_envelope(failed_event)
            return {
                "events": events,
                "failed": True,
                "server_terminal_event": True,
                "last_sequence": int(envelope.get("sequence") or 0),
                "failed_event": failed_event,
                "error": str(failed_data.get("code") or failed_data.get("error") or "run.failed"),
                "failure": failed_data,
                "query_run_id": envelope.get("query_run_id") or started.get("query_run_id"),
                "trace_id": failed_data.get("trace_id")
                or (started.get("data", {}).get("trace_id") if isinstance(started.get("data"), dict) else None),
            }
        message = _event_data(events, "message.delta") or {}
        usage = _event_data(events, "usage.updated") or {}
        completed = _event_data(events, "run.completed") or {}
        if not completed:
            return {
                "events": events,
                "failed": True,
                "server_terminal_event": False,
                "last_sequence": len(events),
                "error": "STREAM_PROTOCOL_ERROR",
                "failure": {"code": "STREAM_PROTOCOL_ERROR", "retryable": True},
                "query_run_id": started.get("query_run_id"),
            }
        return {
            "events": events,
            "failed": False,
            "server_terminal_event": True,
            "last_sequence": int(_event_envelope(_event_for_name(events, "run.completed")).get("sequence") or 0),
            "query_run_id": started.get("query_run_id") or completed.get("query_run_id"),
            "trace_id": started.get("data", {}).get("trace_id") if isinstance(started.get("data"), dict) else None,
            "answer": completed.get("data", {}).get("answer")
            if isinstance(completed.get("data"), dict)
            else message.get("data", {}).get("text", ""),
            "message": message,
            "usage": usage,
        }

    async def run_chat_async(
        self,
        question: str,
        *,
        api: str,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
        mode: str,
        knowledge_base_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Consume chat SSE without a worker thread so cancellation closes the socket."""
        payload: dict[str, Any] = {
            "message": question,
            "mode": mode,
            "stream": True,
            "retrieval_profile": retrieval_profile,
            "retrieval_overrides": retrieval_overrides,
            "client_request_id": _client_request_id(
                question=question,
                retrieval_profile=retrieval_profile,
                retrieval_overrides=retrieval_overrides,
                mode=mode,
                knowledge_base_ids=knowledge_base_ids,
            ),
        }
        if knowledge_base_ids:
            payload["knowledge_base_ids"] = list(knowledge_base_ids)
        normalized_api = api.rstrip("/")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            headers = await self._async_unsafe_headers(client, normalized_api)
            headers["Idempotency-Key"] = str(payload["client_request_id"])
            async with client.stream(
                "POST",
                f"{normalized_api}/api/v1/chat",
                json=payload,
                headers=headers,
            ) as response:
                response.raise_for_status()
                try:
                    return await _collect_chat_sse(aiter_sse(response.aiter_lines()))
                except SseProtocolError as exc:
                    snapshot: dict[str, Any] | None = None
                    if exc.query_run_id:
                        try:
                            status_response = await client.get(
                                f"{normalized_api}/api/v1/query-runs/{exc.query_run_id}/retrieval",
                                headers=headers,
                            )
                            if status_response.is_success:
                                payload = status_response.json()
                                if isinstance(payload, dict) and isinstance(payload.get("run"), dict):
                                    snapshot = dict(payload["run"])
                        except (httpx.HTTPError, ValueError, TypeError):
                            snapshot = None
                    return {
                        "events": exc.events,
                        "failed": True,
                        "server_terminal_event": False,
                        "last_sequence": exc.last_sequence,
                        "last_stage": exc.last_stage,
                        "error": exc.safe_code,
                        "failure": {
                            "code": exc.safe_code,
                            "stage": exc.last_stage,
                            "retryable": exc.retryable,
                        },
                        "query_run_id": exc.query_run_id,
                        "query_run_snapshot": snapshot,
                    }

    def url_ok(self, url: str) -> bool:
        if url in self._url_cache:
            return self._url_cache[url]
        # Only Kiwix URLs are public liveness checks.  Application document URLs
        # require an authenticated API session and must not create noisy 401s.
        if not self.kiwix_public_base_url or not url.startswith(self.kiwix_public_base_url):
            self._url_cache[url] = True
            return True
        urls = [url]
        if self.kiwix_internal_base_url:
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
        knowledge_base_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "message": question,
            "top_k": top_k,
            "retrieval_profile": retrieval_profile,
            "retrieval_overrides": retrieval_overrides,
        }
        if knowledge_base_ids:
            payload["knowledge_base_ids"] = list(knowledge_base_ids)
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

    async def _async_unsafe_headers(self, client: httpx.AsyncClient, api: str) -> dict[str, str]:
        if self.auth_mode != "local":
            return {}
        login = await client.post(
            f"{api}/api/v1/auth/local/login",
            json={"username": self.auth_username, "password": self.auth_password},
        )
        login.raise_for_status()
        session = await client.get(f"{api}/api/v1/auth/session")
        session.raise_for_status()
        payload = dict(session.json())
        if not payload.get("authenticated"):
            raise RuntimeError("eval local auth did not create an authenticated API session")
        csrf_token = str(payload.get("csrf_token") or "")
        return {"X-CSRF-Token": csrf_token} if csrf_token else {}


def iter_sse(lines: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current_event: str | None = None
    current_data: list[str] = []
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        line = str(line).rstrip("\r")
        if not line:
            if current_event and current_data:
                try:
                    payload = json.loads("\n".join(current_data))
                except json.JSONDecodeError as exc:
                    raise SseProtocolError("invalid chat SSE payload") from exc
                if not isinstance(payload, dict):
                    raise SseProtocolError("invalid chat SSE event")
                events.append({"event": current_event, "data": payload})
            current_event = None
            current_data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            current_event = line.removeprefix("event:").lstrip()
        elif line.startswith("data:"):
            current_data.append(line.removeprefix("data:").lstrip())
    if current_event and current_data:
        try:
            payload = json.loads("\n".join(current_data))
        except json.JSONDecodeError as exc:
            raise SseProtocolError("invalid chat SSE payload") from exc
        if not isinstance(payload, dict):
            raise SseProtocolError("invalid chat SSE event")
        events.append({"event": current_event, "data": payload})
    return events


async def aiter_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Parse one SSE event at a time and reject malformed payloads safely."""
    current_event: str | None = None
    current_data: list[str] = []
    async for line in lines:
        line = str(line).rstrip("\r")
        if not line:
            if current_event and current_data:
                try:
                    payload = json.loads("\n".join(current_data))
                except json.JSONDecodeError as exc:
                    raise SseProtocolError("invalid chat SSE payload") from exc
                if not isinstance(payload, dict):
                    raise SseProtocolError("invalid chat SSE event")
                yield {"event": current_event, "data": payload}
            current_event = None
            current_data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            current_event = line.removeprefix("event:").lstrip()
        elif line.startswith("data:"):
            current_data.append(line.removeprefix("data:").lstrip())
    if current_event and current_data:
        try:
            payload = json.loads("\n".join(current_data))
        except json.JSONDecodeError as exc:
            raise SseProtocolError("invalid chat SSE payload") from exc
        if not isinstance(payload, dict):
            raise SseProtocolError("invalid chat SSE event")
        yield {"event": current_event, "data": payload}


async def _collect_chat_sse(events: AsyncIterator[dict[str, Any]]) -> dict[str, Any]:
    """Keep only terminally relevant SSE events while enforcing the wire contract."""
    kept: list[dict[str, Any]] = []
    expected_sequence = 1
    terminal_seen = False
    last_stage = ""
    async for event in events:
        payload = dict(event.get("data") or {})
        sequence = payload.get("sequence")
        if not isinstance(sequence, int) or sequence != expected_sequence:
            raise SseProtocolError(
                "invalid chat SSE sequence",
                query_run_id=str(payload.get("query_run_id") or "") or None,
                last_stage=last_stage,
                last_sequence=expected_sequence - 1,
                events=kept,
            )
        expected_sequence += 1
        event_name = str(event.get("event") or "")
        data = payload.get("data")
        if isinstance(data, dict):
            last_stage = str(data.get("stage") or last_stage)
        if terminal_seen:
            raise SseProtocolError(
                "chat SSE emitted more than one terminal event",
                query_run_id=str(payload.get("query_run_id") or "") or None,
                last_stage=last_stage,
                last_sequence=expected_sequence - 1,
                events=kept,
            )
        if event_name in {"run.completed", "run.failed", "run.cancelled"}:
            terminal_seen = True
        if event_name in {
            "run.completed",
            "run.failed",
            "run.cancelled",
            "run.started",
            "message.delta",
            "usage.updated",
        }:
            kept.append(event)
    if not terminal_seen:
        started = _event_data(kept, "run.started") or {}
        raise SseProtocolError(
            "chat SSE ended before a terminal event",
            query_run_id=str(started.get("query_run_id") or "") or None,
            last_stage=last_stage,
            last_sequence=expected_sequence - 1,
            events=kept,
        )
    return _chat_payload_from_events(kept)


def _client_request_id(
    *,
    question: str,
    retrieval_profile: str,
    retrieval_overrides: dict[str, Any],
    mode: str,
    knowledge_base_ids: list[str] | None,
) -> str:
    """Stable opaque identity lets eval resume an interrupted chat without another model call."""
    encoded = json.dumps(
        {
            "question": question,
            "retrieval_profile": retrieval_profile,
            "retrieval_overrides": retrieval_overrides,
            "mode": mode,
            "knowledge_base_ids": knowledge_base_ids or [],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"eval-{sha256(encoded).hexdigest()}"


def _chat_payload_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    started = _event_data(events, "run.started") or {}
    failed = [event for event in events if event["event"] in {"run.failed", "run.cancelled"}]
    if failed:
        failed_event = failed[-1]
        failed_data = _event_body(failed_event)
        envelope = _event_envelope(failed_event)
        return {
            "events": events,
            "failed": True,
            "server_terminal_event": True,
            "last_sequence": int(envelope.get("sequence") or 0),
            "failed_event": failed_event,
            "error": str(failed_data.get("code") or failed_data.get("error") or failed_event["event"]),
            "failure": failed_data,
            "query_run_id": envelope.get("query_run_id") or started.get("query_run_id"),
            "trace_id": failed_data.get("trace_id")
            or (started.get("data", {}).get("trace_id") if isinstance(started.get("data"), dict) else None),
        }
    message = _event_data(events, "message.delta") or {}
    usage = _event_data(events, "usage.updated") or {}
    completed = _event_data(events, "run.completed") or {}
    if not completed:
        last_stage = ""
        for event in events:
            data = event.get("data")
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                last_stage = str(data["data"].get("stage") or last_stage)
        return {
            "events": events,
            "failed": True,
            "server_terminal_event": False,
            "last_sequence": len(events),
            "error": "STREAM_PROTOCOL_ERROR",
            "failure": {"code": "STREAM_PROTOCOL_ERROR", "stage": last_stage, "retryable": True},
            "query_run_id": started.get("query_run_id"),
        }
    return {
        "events": events,
        "failed": False,
        "server_terminal_event": True,
        "last_sequence": int(_event_envelope(completed).get("sequence") or 0),
        "query_run_id": started.get("query_run_id") or completed.get("query_run_id"),
        "trace_id": started.get("data", {}).get("trace_id") if isinstance(started.get("data"), dict) else None,
        "answer": completed.get("data", {}).get("answer")
        if isinstance(completed.get("data"), dict)
        else message.get("data", {}).get("text", ""),
        "message": message,
        "usage": usage,
    }


def _event_data(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for event in events:
        if event.get("event") == name and isinstance(event.get("data"), dict):
            return dict(event["data"])
    return None


def _event_for_name(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("event") == name:
            return event
    return {}


def _event_envelope(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("data")
    return dict(payload) if isinstance(payload, dict) else {}


def _event_body(event: dict[str, Any]) -> dict[str, Any]:
    envelope = _event_envelope(event)
    body = envelope.get("data")
    return dict(body) if isinstance(body, dict) else {}
