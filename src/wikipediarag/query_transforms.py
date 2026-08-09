"""Bounded, deterministic query transformations used by retrieval harnesses."""

from __future__ import annotations

import re

_QUOTED = re.compile(r"[«\"“]([^»\"”]{2,160})[»\"”]")


def normalize_query(query: str) -> str:
    return " ".join(str(query).split())


def bounded_rewrite(query: str) -> str | None:
    normalized = normalize_query(query)
    if len(normalized) < 80 and not _QUOTED.search(normalized) and not re.search(r"\b\d{3,}\b", normalized):
        return None
    # Keep the rewrite lexical and deterministic: remove conversational lead-ins
    # while preserving identifiers and quoted titles.
    rewritten = re.sub(r"^(?:расскажи|объясни|найди|пожалуйста)\s+", "", normalized, flags=re.I)
    return rewritten if rewritten != normalized else None


def bounded_decomposition(query: str, *, max_subqueries: int = 6) -> list[str]:
    normalized = normalize_query(query)
    parts = re.split(r"\s+(?:и|а также|and|also|;|\?)\s+", normalized, flags=re.I)
    values: list[str] = []
    for part in parts:
        value = normalize_query(part)
        if len(value) >= 3 and value.casefold() not in {item.casefold() for item in values}:
            values.append(value)
    return values[: max(1, min(max_subqueries, 6))]


def bridge_queries(query: str) -> list[str]:
    normalized = normalize_query(query)
    return [normalized] if len(bounded_decomposition(normalized, max_subqueries=2)) > 1 else []


def query_context(query: str, *, role: str = "primary", order: int = 1) -> dict[str, object]:
    normalized = normalize_query(query)
    return {"text": normalized, "role": role, "order": order}
