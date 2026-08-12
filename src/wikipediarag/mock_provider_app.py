from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import uvicorn
from fastapi import FastAPI

from wikipediarag.config import get_settings
from wikipediarag.embedding import embed_text, normalize_for_embedding

app = FastAPI(title="WikipediaRag Mock OpenAI-compatible Provider")

EVIDENCE_RE = re.compile(r"\[(S\d+)\]\s+([^\n]+)\n(.+?)(?=\n\n\[S\d+\]|\Z)", re.DOTALL)
_delayed_chat_requests = 0


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/embeddings")
async def create_embeddings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    raw_inputs = payload.get("input", [])
    inputs = raw_inputs if isinstance(raw_inputs, list) else [str(raw_inputs)]
    return {
        "object": "list",
        "model": payload.get("model", "embed_default"),
        "data": [
            {
                "object": "embedding",
                "index": index,
                "embedding": embed_text(str(text), settings.embedding_dimensions),
            }
            for index, text in enumerate(inputs)
        ],
        "usage": {
            "prompt_tokens": sum(len(str(item).split()) for item in inputs),
            "total_tokens": 0,
        },
    }


@app.post("/v1/rerank")
async def rerank(payload: dict[str, Any]) -> dict[str, Any]:
    query_terms = set(normalize_for_embedding(str(payload.get("query", ""))).split())
    documents = [str(item) for item in payload.get("documents", [])]
    results: list[dict[str, Any]] = []
    for index, document in enumerate(documents):
        doc_terms = set(normalize_for_embedding(document).split())
        score = len(query_terms & doc_terms) / max(len(query_terms), 1)
        results.append({"index": index, "relevance_score": round(score, 6), "document": {"text": document}})
    results.sort(key=_rerank_score, reverse=True)
    top_n = int(payload.get("top_n") or len(results))
    return {"model": payload.get("model", "rerank_default"), "results": results[:top_n]}


@app.post("/v1/chat/completions")
async def chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
    global _delayed_chat_requests
    settings = get_settings()
    if (
        settings.mock_provider_chat_delay_seconds > 0
        and _delayed_chat_requests < settings.mock_provider_chat_delay_requests
    ):
        _delayed_chat_requests += 1
        await asyncio.sleep(settings.mock_provider_chat_delay_seconds)
    messages = payload.get("messages", [])
    user_text = "\n".join(str(item.get("content", "")) for item in messages if item.get("role") == "user")
    evidences = EVIDENCE_RE.findall(user_text)
    response_format = payload.get("response_format")
    output_mode = settings.mock_provider_output_mode
    if output_mode == "malformed_json":
        answer = "this is not JSON"
    elif output_mode == "truncated_json":
        answer = '{"answer_markdown": "truncated", "claims": ['
    elif output_mode == "schema_mismatch":
        answer = json.dumps({"ok": True}, ensure_ascii=False)
    elif isinstance(response_format, dict) and response_format.get("type") == "json_schema":
        if evidences:
            first_id, title, body = evidences[0]
            sentence = _first_sentence(body)
            answer = json.dumps(
                {
                    "answer_markdown": f"{sentence} [{first_id}]",
                    "answer_mode": "single",
                    "interpretations": [],
                    "clarification_question": None,
                    "claims": [
                        {
                            "claim_id": "mock-claim-1",
                            "text": sentence,
                            "evidence_ids": [first_id],
                            "type": "fact",
                        }
                    ],
                    "insufficient_evidence": False,
                },
                ensure_ascii=False,
            )
        else:
            answer = json.dumps(
                {
                    "answer_markdown": "Недостаточно доказательств в локальной базе.",
                    "answer_mode": "single",
                    "interpretations": [],
                    "clarification_question": None,
                    "claims": [],
                    "insufficient_evidence": True,
                    "insufficient_evidence_reason": "insufficient_context",
                },
                ensure_ascii=False,
            )
    elif isinstance(response_format, dict) and response_format.get("type") == "json_object" and not evidences:
        answer = json.dumps({"ok": True}, ensure_ascii=False)
    elif not evidences:
        answer = "Недостаточно доказательств в локальной базе, чтобы надёжно ответить на вопрос."
    else:
        first_id, title, body = evidences[0]
        sentence = _first_sentence(body)
        answer = f"{sentence} [{first_id}]\n\nИсточник: {title} [{first_id}]"
    return {
        "id": "mock-chat-completion",
        "object": "chat.completion",
        "model": payload.get("model", "generator_fast"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(user_text.split()),
            "completion_tokens": len(answer.split()),
        },
    }


def _first_sentence(text: str) -> str:
    normalized = " ".join(text.split())
    for separator in (". ", "! ", "? "):
        if separator in normalized:
            return normalized.split(separator, 1)[0][:700] + separator.strip()
    return normalized[:700]


def _rerank_score(item: dict[str, Any]) -> float:
    value = item.get("relevance_score", 0.0)
    return float(value) if isinstance(value, int | float | str) else 0.0


def main() -> None:
    uvicorn.run("wikipediarag.mock_provider_app:app", host="0.0.0.0", port=8080)  # noqa: S104


if __name__ == "__main__":
    main()
