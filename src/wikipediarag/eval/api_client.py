from __future__ import annotations

import json
from typing import Any, Protocol

import httpx


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
    def __init__(self, *, timeout: float = 600, kiwix_public_base_url: str = "", kiwix_internal_base_url: str = ""):
        self.timeout = timeout
        self.kiwix_public_base_url = kiwix_public_base_url.rstrip("/")
        self.kiwix_internal_base_url = kiwix_internal_base_url.rstrip("/")
        self._url_cache: dict[str, bool] = {}

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
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", f"{api.rstrip('/')}/api/v1/chat", json=payload) as response:
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
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{api.rstrip('/')}/api/v1/search:debug", json=payload)
            response.raise_for_status()
            return dict(response.json())


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
