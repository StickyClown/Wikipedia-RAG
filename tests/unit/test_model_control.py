from __future__ import annotations

import pytest

from wikipediarag.model_control import (
    ModelContract,
    ModelControlError,
    ModelDriver,
    ModelOperation,
    ThinkingMode,
    ThinkingPolicy,
    compile_token_envelope,
    map_thinking_parameters,
    merge_parameters,
    redact_connection,
    schema_canary_reserve,
    validate_stage_binding,
    validate_tokenizer_calibration,
)
from wikipediarag.model_drivers import DriverRequest, MockDriver


def test_parameter_hierarchy_only_allows_workload_to_reduce_limit() -> None:
    resolved = merge_parameters(
        {"temperature": 0.2},
        {"max_output_tokens": 500},
        {"max_output_tokens": 300},
        workload_max_tokens=120,
    )
    assert resolved == {"temperature": 0.2, "max_output_tokens": 120}


def test_unknown_parameter_is_blocked() -> None:
    with pytest.raises(ModelControlError, match="unknown model parameter"):
        merge_parameters({}, {"provider_magic": True}, {})


def test_thinking_mapping_is_explicit_per_driver() -> None:
    off = ThinkingPolicy(mode=ThinkingMode.off, effort="none")
    assert map_thinking_parameters(ModelDriver.openrouter, off)["reasoning"]["effort"] == "none"
    assert map_thinking_parameters(ModelDriver.vllm, off)["chat_template_kwargs"]["enable_thinking"] is False
    with pytest.raises(ModelControlError):
        map_thinking_parameters(ModelDriver.openai_compatible, ThinkingPolicy(mode=ThinkingMode.on, effort="low"))


def test_token_envelope_and_schema_reserve() -> None:
    assert schema_canary_reserve(100) == 132
    envelope = compile_token_envelope(
        max_input=100,
        context_window=512,
        final_output_reserve=100,
        thinking=ThinkingPolicy(mode=ThinkingMode.off, effort="none"),
    )
    assert envelope.reasoning_reserve == 0
    with pytest.raises(ModelControlError, match="MODEL_TOKEN_BUDGET_EXCEEDED"):
        compile_token_envelope(
            max_input=500,
            context_window=512,
            final_output_reserve=100,
            thinking=ThinkingPolicy(mode=ThinkingMode.off, effort="none"),
        )


def test_embedding_contract_and_catalog_only_vision() -> None:
    embedding = ModelContract(
        alias="embed",
        provider_model="qwen-embed",
        operation=ModelOperation.embedding,
        capabilities=frozenset({"embedding"}),
        dimensions=1024,
    )
    assert embedding.embedding_fingerprint
    assert validate_stage_binding("ingestion.embedding", embedding, ThinkingPolicy())
    vision = ModelContract(
        alias="vision",
        provider_model="qwen-vl",
        operation=ModelOperation.chat,
        capabilities=frozenset({"chat"}),
        input_modalities=("text", "image"),
    )
    with pytest.raises(ModelControlError, match="MODEL_VISION_CATALOG_ONLY"):
        validate_stage_binding("chat.answer", vision, ThinkingPolicy())


def test_tokenizer_calibration_has_safe_tolerance() -> None:
    validate_tokenizer_calibration(1000, 1040)
    with pytest.raises(ModelControlError, match="MODEL_TOKENIZER_CALIBRATION_FAILED"):
        validate_tokenizer_calibration(1000, 1100)


def test_connection_redaction_never_returns_encrypted_payload() -> None:
    safe = redact_connection(
        {
            "id": "c1",
            "name": "openrouter",
            "driver": "openrouter",
            "base_url": "https://example.test",
            "encrypted_payload": "ciphertext-that-must-not-leak",
            "has_credentials": True,
        }
    )
    assert safe["has_credentials"] is True
    assert "encrypted_payload" not in safe


@pytest.mark.anyio
async def test_mock_driver_canary_is_deterministic() -> None:
    result = await MockDriver().run_capability_canary(DriverRequest(base_url="http://mock", model="mock-model"))
    assert result["status"] == "passed"
    assert result["finish_reason"] == "stop"
