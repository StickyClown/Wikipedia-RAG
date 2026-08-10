from __future__ import annotations

import pytest

from wikipediarag.answerability import decide_answerability
from wikipediarag.answering import ModelOutputError, _parse_answer_draft
from wikipediarag.config import Settings
from wikipediarag.gateway_app import _validate_json_schema_value
from wikipediarag.reliability import safe_failure_from_exception
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import AnswerabilityStatus, Evidence


def _evidence() -> list[Evidence]:
    return [
        Evidence(
            evidence_id="S1",
            chunk_id="chunk-1",
            document_id="doc-1",
            title="Россия",
            section_path=["Россия"],
            content="Россия — государство в Восточной Европе и Северной Азии.",
            source_url="https://example.test/ru",
        )
    ]


def test_structured_answer_accepts_bom_and_fenced_json() -> None:
    payload = _parse_answer_draft(
        "\ufeff```json\n"
        '{"answer_markdown":"Россия — государство [S1]",'
        '"claims":[{"claim_id":"c1","text":"Россия — государство",'
        '"evidence_ids":["S1"],"type":"fact"}],"insufficient_evidence":false}\n```',
        _evidence(),
        strict=True,
    )
    assert payload["claims"][0]["evidence_ids"] == ["S1"]


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ('{"answer_markdown":"broken"', "MODEL_OUTPUT_TRUNCATED"),
        ('{"answer_markdown":"ok","claims":[],"insufficient_evidence":false} trailing', "MODEL_OUTPUT_INVALID"),
    ],
)
def test_structured_answer_rejects_truncated_and_trailing_content(content: str, code: str) -> None:
    with pytest.raises(ModelOutputError) as exc:
        _parse_answer_draft(content, _evidence(), strict=True)
    assert exc.value.safe_code == code


def test_unknown_claim_evidence_is_a_safe_failure() -> None:
    with pytest.raises(ModelOutputError) as exc:
        _parse_answer_draft(
            '{"answer_markdown":"x [S9]",'
            '"claims":[{"claim_id":"c1","text":"x",'
            '"evidence_ids":["S9"],"type":"fact"}],"insufficient_evidence":false}',
            _evidence(),
            strict=True,
        )
    failure = safe_failure_from_exception(exc.value, stage="generation")
    assert failure.error_code == "MODEL_OUTPUT_INVALID"
    assert failure.retryable is False


def test_gateway_schema_validator_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        _validate_json_schema_value(
            {"ok": True, "provider_payload": "secret"},
            {
                "type": "object",
                "required": ["ok"],
                "additionalProperties": False,
                "properties": {"ok": {"type": "boolean"}},
            },
            path="$",
            root={},
        )


def test_definition_query_does_not_treat_parenthetical_title_as_exact() -> None:
    evidence = [
        Evidence(
            evidence_id="S1",
            chunk_id="c1",
            title="(232) Россия",
            section_path=[],
            content="Астероид главного пояса.",
            source_url="https://example.test/asteroid",
        )
    ]
    decision = decide_answerability("Что такое Россия?", evidence, get_retrieval_profile("test_mock", Settings()))
    assert decision.signals["exact_title_match"] is False
    assert decision.status in {AnswerabilityStatus.partial, AnswerabilityStatus.unanswerable}
