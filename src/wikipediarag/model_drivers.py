"""OpenAI-compatible endpoint drivers used by the model control plane.

Drivers are request adapters only.  They never start or manage a model
process, and all failures are converted to small stable error objects.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from wikipediarag.model_control import (
    ModelControlError,
    ModelDriver,
    ModelOperation,
    ThinkingPolicy,
    compile_provider_payload,
)


class DriverError(RuntimeError):
    def __init__(self, code: str, message: str = "provider request failed") -> None:
        super().__init__(message)
        self.code = code


class UnsupportedOperationError(DriverError):
    def __init__(self, operation: str) -> None:
        super().__init__("MODEL_OPERATION_UNSUPPORTED", f"operation is not supported: {operation}")


@dataclass(frozen=True, slots=True)
class DriverRequest:
    base_url: str
    model: str
    paths: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    tls_verify: bool = True
    timeout: float = 30.0
    request_adapter: Mapping[str, Any] = field(default_factory=dict)
    request_defaults: Mapping[str, Any] = field(default_factory=dict)


class EndpointDriver:
    driver: ModelDriver
    default_paths: Mapping[str, str]
    supports_rerank: bool = False

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def _path(self, request: DriverRequest, operation: str) -> str:
        adapter_paths = (
            request.request_adapter.get("endpoint_paths") if isinstance(request.request_adapter, Mapping) else {}
        )
        return request.paths.get(operation) or (adapter_paths or {}).get(operation) or self.default_paths[operation]

    async def _request(
        self, request: DriverRequest, operation: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{request.base_url.rstrip('/')}/{self._path(request, operation).lstrip('/')}"
        close_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=request.timeout, verify=request.tls_verify)
        try:
            response = (
                await client.post(url, headers=dict(request.headers), json=payload)
                if payload is not None
                else await client.get(url, headers=dict(request.headers))
            )
            if response.status_code >= 400:
                raise DriverError(f"MODEL_ENDPOINT_HTTP_{response.status_code}")
            data = response.json()
            return data if isinstance(data, dict) else {"data": data}
        except DriverError:
            raise
        except httpx.TimeoutException as exc:
            raise DriverError("MODEL_ENDPOINT_TIMEOUT") from exc
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise DriverError("MODEL_ENDPOINT_UNAVAILABLE") from exc
        finally:
            if close_client:
                await client.aclose()

    async def discover_models(self, request: DriverRequest) -> list[dict[str, Any]]:
        data = await self._request(request, "models")
        models = data.get("data", data.get("models", []))
        return [item for item in models if isinstance(item, dict)][:200]

    def map_parameters(self, parameters: Mapping[str, Any], thinking: ThinkingPolicy | None = None) -> dict[str, Any]:
        return dict(parameters)

    async def chat(
        self,
        request: DriverRequest,
        *,
        messages: list[Mapping[str, Any]],
        parameters: Mapping[str, Any] | None = None,
        response_format: Mapping[str, Any] | None = None,
        thinking: ThinkingPolicy | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = compile_provider_payload(
            {"model": request.model, "messages": messages, **dict(parameters or {})},
            connection_defaults=request.request_defaults,
            request_adapter=request.request_adapter,
            thinking=thinking,
        )
        if response_format:
            payload["response_format"] = dict(response_format)
            if self.driver is ModelDriver.openrouter:
                payload.setdefault("provider", {})["require_parameters"] = True
        return await self._request(request, "chat", payload)

    async def stream_chat(
        self,
        request: DriverRequest,
        *,
        messages: list[Mapping[str, Any]],
        parameters: Mapping[str, Any] | None = None,
        response_format: Mapping[str, Any] | None = None,
        thinking: ThinkingPolicy | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        url = f"{request.base_url.rstrip('/')}/{self._path(request, 'chat').lstrip('/')}"
        payload: dict[str, Any] = compile_provider_payload(
            {"model": request.model, "messages": messages, "stream": True, **dict(parameters or {})},
            connection_defaults=request.request_defaults,
            request_adapter=request.request_adapter,
            thinking=thinking,
        )
        if response_format:
            payload["response_format"] = dict(response_format)
        close_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=request.timeout, verify=request.tls_verify)
        try:
            async with client.stream("POST", url, headers=dict(request.headers), json=payload) as response:
                if response.status_code >= 400:
                    raise DriverError(f"MODEL_ENDPOINT_HTTP_{response.status_code}")
                async for line in response.aiter_lines():
                    if not line or line == "data: [DONE]":
                        continue
                    raw = line[5:].strip() if line.startswith("data:") else line
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        yield value
        except DriverError:
            raise
        except httpx.TimeoutException as exc:
            raise DriverError("MODEL_ENDPOINT_TIMEOUT") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise DriverError("MODEL_ENDPOINT_UNAVAILABLE") from exc
        finally:
            if close_client:
                await client.aclose()

    async def embed(
        self, request: DriverRequest, *, inputs: list[str], parameters: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        adapter_paths = (
            request.request_adapter.get("endpoint_paths") if isinstance(request.request_adapter, Mapping) else {}
        )
        if (
            self.driver is ModelDriver.textgen_webui
            and "embeddings" not in request.paths
            and "embeddings" not in (adapter_paths or {})
        ):
            raise UnsupportedOperationError(ModelOperation.embedding.value)
        payload: dict[str, Any] = {"model": request.model, "input": inputs}
        payload.update(parameters or {})
        return await self._request(request, "embeddings", payload)

    async def rerank(
        self, request: DriverRequest, *, query: str, documents: list[str], parameters: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.supports_rerank:
            raise UnsupportedOperationError(ModelOperation.rerank.value)
        payload = {"model": request.model, "query": query, "documents": documents}
        payload.update(parameters or {})
        return await self._request(request, "rerank", payload)

    async def count_tokens(self, request: DriverRequest, *, text_value: str) -> int | None:
        if "tokenize" not in request.paths:
            return None
        data = await self._request(request, "tokenize", {"model": request.model, "text": text_value})
        value = data.get("count", data.get("token_count", data.get("tokens")))
        return int(value) if isinstance(value, (int, float)) else None

    def normalize_error(self, exc: Exception) -> dict[str, str]:
        if isinstance(exc, DriverError):
            return {"code": exc.code, "message": "model endpoint request failed"}
        return {"code": "MODEL_DRIVER_ERROR", "message": "model endpoint request failed"}

    async def run_capability_canary(
        self, request: DriverRequest, *, thinking: ThinkingPolicy | None = None, max_output_tokens: int = 4096
    ) -> dict[str, Any]:
        policy = thinking or ThinkingPolicy()
        parameters = {"max_output_tokens": max(64, int(max_output_tokens))}
        try:
            response = await self.chat(
                request,
                messages=[{"role": "user", "content": 'Return the JSON object {"ok":true}.'}],
                parameters=parameters,
                response_format={"type": "json_object"},
                thinking=policy,
            )
            choices = response.get("choices") or []
            message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                return {"status": "failed", "safe_error_code": "MODEL_CANARY_EMPTY_FINAL"}
            try:
                json.loads(content)
            except ValueError:
                return {"status": "failed", "safe_error_code": "MODEL_CANARY_INVALID_JSON"}
            usage_value = response.get("usage")
            usage = dict(usage_value) if isinstance(usage_value, dict) else {}
            return {
                "status": "passed",
                "safe_error_code": None,
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "finish_reason": (
                    choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
                ),
            }
        except (DriverError, ModelControlError) as exc:
            return {"status": "failed", "safe_error_code": self.normalize_error(exc)["code"]}


class OpenRouterDriver(EndpointDriver):
    driver = ModelDriver.openrouter
    default_paths = {"models": "/models", "chat": "/chat/completions", "embeddings": "/embeddings", "rerank": "/rerank"}
    supports_rerank = True


class VLLMDriver(EndpointDriver):
    driver = ModelDriver.vllm
    default_paths = {
        "models": "/v1/models",
        "chat": "/v1/chat/completions",
        "embeddings": "/v1/embeddings",
        "rerank": "/rerank",
    }
    supports_rerank = True


class LlamaCppDriver(EndpointDriver):
    driver = ModelDriver.llamacpp
    default_paths = {
        "models": "/v1/models",
        "chat": "/v1/chat/completions",
        "embeddings": "/v1/embeddings",
        "rerank": "/rerank",
    }
    supports_rerank = True


class TextGenerationWebUIDriver(EndpointDriver):
    driver = ModelDriver.textgen_webui
    default_paths = {"models": "/v1/models", "chat": "/v1/chat/completions", "embeddings": "/v1/embeddings"}


class OpenAICompatibleDriver(EndpointDriver):
    driver = ModelDriver.openai_compatible
    default_paths = {"models": "/v1/models", "chat": "/v1/chat/completions", "embeddings": "/v1/embeddings"}


class MockDriver(EndpointDriver):
    driver = ModelDriver.mock
    default_paths = {"models": "/models", "chat": "/chat/completions", "embeddings": "/embeddings", "rerank": "/rerank"}
    supports_rerank = True

    async def chat(self, request: DriverRequest, **kwargs: Any) -> dict[str, Any]:
        return {
            "choices": [{"message": {"role": "assistant", "content": '{"ok":true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    async def embed(
        self, request: DriverRequest, *, inputs: list[str], parameters: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        dimensions = int((parameters or {}).get("dimensions", 3))
        return {"data": [{"index": index, "embedding": [0.0] * dimensions} for index, _ in enumerate(inputs)]}

    async def rerank(
        self, request: DriverRequest, *, query: str, documents: list[str], parameters: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"results": [{"index": index, "relevance_score": 0.0} for index, _ in enumerate(documents)]}


DRIVER_TYPES: dict[ModelDriver, type[EndpointDriver]] = {
    ModelDriver.openrouter: OpenRouterDriver,
    ModelDriver.vllm: VLLMDriver,
    ModelDriver.llamacpp: LlamaCppDriver,
    ModelDriver.textgen_webui: TextGenerationWebUIDriver,
    ModelDriver.openai_compatible: OpenAICompatibleDriver,
    ModelDriver.mock: MockDriver,
}


def driver_for(driver: ModelDriver | str, *, client: httpx.AsyncClient | None = None) -> EndpointDriver:
    try:
        driver_type = ModelDriver(str(driver))
    except ValueError as exc:
        raise ModelControlError("MODEL_DRIVER_UNKNOWN") from exc
    return DRIVER_TYPES[driver_type](client=client)
