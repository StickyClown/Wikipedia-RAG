from __future__ import annotations

import re

from wikipediarag.model_client import chat_completion
from wikipediarag.schemas import Evidence, RetrievalResult

CITATION_RE = re.compile(r"\[(S\d+)\]")


async def generate_answer(question: str, retrieval: RetrievalResult) -> tuple[str, dict[str, object]]:
    if retrieval.insufficient_evidence:
        answer = (
            "Недостаточно доказательств в локальной базе, чтобы надёжно ответить на вопрос. "
            "Попробуйте расширить импорт Wikipedia или уточнить запрос."
        )
        return answer, {"insufficient_evidence": True, "citations": []}

    manifest = "\n\n".join(format_evidence(item) for item in retrieval.evidence)
    prompt = (
        "Ответь на русском языке только по источникам ниже. "
        "Каждое фактическое утверждение снабжай citation ID вида [S1].\n\n"
        f"Вопрос: {question}\n\nИсточники:\n{manifest}"
    )
    content = await chat_completion(
        [
            {
                "role": "system",
                "content": "Ты локальный RAG генератор. Не придумывай факты вне evidence.",
            },
            {"role": "user", "content": prompt},
        ]
    )
    validation = validate_citations(content, retrieval.evidence)
    if not validation["valid"]:
        repaired = (
            "Найденные источники релевантны, но сгенерированный ответ не прошёл проверку ссылок. "
            f"Краткий подтверждённый фрагмент: {retrieval.evidence[0].content[:500]} [S1]"
        )
        validation = validate_citations(repaired, retrieval.evidence)
        return repaired, validation
    return content, validation


def format_evidence(evidence: Evidence) -> str:
    section = " / ".join(evidence.section_path)
    return f"[{evidence.evidence_id}] {evidence.title} / {section}\n{evidence.content}"


def validate_citations(answer: str, evidence: list[Evidence]) -> dict[str, object]:
    allowed = {item.evidence_id for item in evidence}
    cited = CITATION_RE.findall(answer)
    unknown = sorted(set(cited) - allowed)
    return {
        "valid": bool(cited) and not unknown,
        "citations": cited,
        "unknown": unknown,
        "allowed": sorted(allowed),
        "insufficient_evidence": False,
    }
