from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import anyio
from pydantic import BaseModel, Field

from wikipediarag.config import Settings, get_settings
from wikipediarag.embedding import normalize_for_embedding
from wikipediarag.eval.artifacts import ARTIFACT_ROOT, utc_now_iso, write_json, write_jsonl
from wikipediarag.eval.corpus import CorpusChunk, load_alias_chunks, load_candidate_chunks
from wikipediarag.eval.corpus import load_corpus_snapshot as default_load_corpus_snapshot
from wikipediarag.eval.hashing import stable_json_hash
from wikipediarag.eval.settings import adapt_eval_settings

BindingStatus = Literal["EXACT", "REDIRECT", "AMBIGUOUS", "MISSING"]
DecisionStatus = Literal["AUTO_ACCEPT", "REVIEW", "REJECT"]
CandidateSplit = Literal["train", "dev"]
CandidateReviewStatus = Literal["unreviewed"]

MIRACL_RU_SOURCE = "miracl-ru"
EXTERNAL_TRANSFER_VERSION = "external_transfer_v1"


class ExternalQuestion(BaseModel):
    source_dataset: str = MIRACL_RU_SOURCE
    language: str = "ru"
    external_id: str
    query: str
    gold_titles: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class TrustedTask(BaseModel):
    task_id: str
    question: str
    source_dataset: str
    source_external_id: str
    gold_page_ids: list[str]
    gold_chunk_ids: list[str]
    gold_titles: list[str]
    split: CandidateSplit
    review_status: CandidateReviewStatus
    provenance: dict[str, str]


class CorpusBoundCandidate(BaseModel):
    candidate_id: str
    external_question: ExternalQuestion
    binding_status: BindingStatus
    decision_status: DecisionStatus
    split: CandidateSplit
    review_status: CandidateReviewStatus = "unreviewed"
    match_method: str
    matched_titles: list[str] = Field(default_factory=list)
    local_document_ids: list[str] = Field(default_factory=list)
    local_chunk_ids: list[str] = Field(default_factory=list)
    local_titles: list[str] = Field(default_factory=list)
    trusted_task: TrustedTask | None = None
    notes: list[str] = Field(default_factory=list)


class _BindingRef(BaseModel):
    method: Literal["exact", "redirect"]
    external_title: str
    local_title: str
    document_id: str
    chunk_id: str


class _CorpusTitleIndex(BaseModel):
    exact: dict[str, list[_BindingRef]]
    redirects: dict[str, list[_BindingRef]]


async def transfer_miracl_ru(*, input_path: Path, settings: Settings | None = None) -> dict[str, str]:
    resolved = adapt_eval_settings(settings or get_settings())
    snapshot = await default_load_corpus_snapshot(resolved)
    chunks = await load_candidate_chunks(limit=5000, settings=resolved)
    aliases = await load_alias_chunks(limit=5000, settings=resolved)
    questions = await load_miracl_ru_questions(input_path)
    candidates = bind_external_questions(
        questions,
        chunks=chunks,
        aliases=aliases,
        provenance={
            "snapshot_id": snapshot.snapshot_id,
            "index_version": snapshot.index_version,
            "zim_checksum": snapshot.zim_checksum,
            "retrieval_profile_hash": snapshot.retrieval_profile_hash,
        },
    )
    output_dir = ARTIFACT_ROOT / "external" / MIRACL_RU_SOURCE
    output_key = stable_json_hash({"input": str(input_path), "count": len(candidates)})[:12]
    output_path = output_dir / f"{input_path.stem}-{output_key}-candidates.jsonl"
    manifest_path = output_path.with_suffix(".manifest.json")
    binding_counts = Counter(candidate.binding_status for candidate in candidates)
    decision_counts = Counter(candidate.decision_status for candidate in candidates)
    write_jsonl(output_path, candidates)
    manifest = {
        "dataset_source": MIRACL_RU_SOURCE,
        "transfer_version": EXTERNAL_TRANSFER_VERSION,
        "created_at": utc_now_iso(),
        "input_path": str(input_path),
        "candidate_count": len(candidates),
        "jsonl_path": str(output_path),
        "snapshot_id": snapshot.snapshot_id,
        "index_version": snapshot.index_version,
        "zim_checksum": snapshot.zim_checksum,
        "retrieval_profile_hash": snapshot.retrieval_profile_hash,
        "by_binding_status": dict(sorted(binding_counts.items())),
        "by_decision_status": dict(sorted(decision_counts.items())),
        "split_policy": "train_auto_accept_dev_review_no_test",
    }
    write_json(manifest_path, manifest)
    write_json(output_dir / "latest.json", {"jsonl": str(output_path), "manifest": str(manifest_path)})
    return {"jsonl": str(output_path), "manifest": str(manifest_path), "count": str(len(candidates))}


async def load_miracl_ru_questions(input_path: Path) -> list[ExternalQuestion]:
    input_text = await anyio.Path(input_path).read_text(encoding="utf-8")
    questions: list[ExternalQuestion] = []
    for index, line in enumerate(input_text.splitlines(), start=1):
        if not line.strip():
            continue
        questions.append(parse_miracl_ru_line(line, index=index))
    return questions


def parse_miracl_ru_line(line: str, *, index: int) -> ExternalQuestion:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        parts = line.split("\t")
        payload = {
            "id": parts[0] if parts else "",
            "query": parts[1] if len(parts) > 1 else "",
            "title": parts[2] if len(parts) > 2 else "",
        }
    if not isinstance(payload, dict):
        payload = {"id": f"miracl-{index}", "query": "", "raw": payload}
    external_id = str(payload.get("query_id") or payload.get("id") or payload.get("qid") or f"miracl-{index}")
    return ExternalQuestion(
        external_id=external_id,
        query=str(payload.get("query") or payload.get("question") or ""),
        gold_titles=_extract_gold_titles(payload),
        raw=payload,
    )


def bind_external_questions(
    questions: list[ExternalQuestion],
    *,
    chunks: list[CorpusChunk],
    aliases: list[tuple[str, CorpusChunk]],
    provenance: dict[str, str],
) -> list[CorpusBoundCandidate]:
    title_index = _build_title_index(chunks, aliases)
    return [
        bind_external_question(question, title_index=title_index, provenance=provenance, ordinal=index)
        for index, question in enumerate(questions, start=1)
    ]


def bind_external_question(
    question: ExternalQuestion,
    *,
    title_index: _CorpusTitleIndex,
    provenance: dict[str, str],
    ordinal: int,
) -> CorpusBoundCandidate:
    refs: list[_BindingRef] = []
    notes: list[str] = []
    methods: set[str] = set()
    for title in question.gold_titles:
        normalized = _title_key(title)
        exact_refs = title_index.exact.get(normalized, [])
        redirect_refs = title_index.redirects.get(normalized, [])
        if len(_document_ids(exact_refs)) > 1 or len(_document_ids(redirect_refs)) > 1:
            return _candidate(
                question,
                ordinal=ordinal,
                binding_status="AMBIGUOUS",
                decision_status="REVIEW",
                split="dev",
                match_method="ambiguous_title",
                refs=[],
                provenance=provenance,
                notes=[f"ambiguous title binding: {title}"],
            )
        if exact_refs:
            refs.extend(_first_ref_per_document(exact_refs))
            methods.add("exact")
            continue
        if redirect_refs:
            refs.extend(_first_ref_per_document(redirect_refs))
            methods.add("redirect")
            continue
        notes.append(f"missing local title binding: {title}")
    if not question.gold_titles or notes:
        return _candidate(
            question,
            ordinal=ordinal,
            binding_status="MISSING",
            decision_status="REJECT",
            split="train",
            match_method="none",
            refs=[],
            provenance=provenance,
            notes=notes or ["no gold title found in external row"],
        )
    binding_status: BindingStatus = "REDIRECT" if methods == {"redirect"} or "redirect" in methods else "EXACT"
    decision_status: DecisionStatus = "AUTO_ACCEPT"
    return _candidate(
        question,
        ordinal=ordinal,
        binding_status=binding_status,
        decision_status=decision_status,
        split="train",
        match_method="+".join(sorted(methods)),
        refs=refs,
        provenance=provenance,
        notes=[],
    )


def _candidate(
    question: ExternalQuestion,
    *,
    ordinal: int,
    binding_status: BindingStatus,
    decision_status: DecisionStatus,
    split: CandidateSplit,
    match_method: str,
    refs: list[_BindingRef],
    provenance: dict[str, str],
    notes: list[str],
) -> CorpusBoundCandidate:
    candidate_id = f"{question.source_dataset}-{stable_json_hash([question.external_id, ordinal], 16)[:12]}"
    task = None
    if decision_status == "AUTO_ACCEPT":
        task = TrustedTask(
            task_id=f"external-{candidate_id}",
            question=question.query,
            source_dataset=question.source_dataset,
            source_external_id=question.external_id,
            gold_page_ids=sorted(_document_ids(refs)),
            gold_chunk_ids=sorted({ref.chunk_id for ref in refs}),
            gold_titles=sorted({ref.local_title for ref in refs}),
            split=split,
            review_status="unreviewed",
            provenance=provenance,
        )
    return CorpusBoundCandidate(
        candidate_id=candidate_id,
        external_question=question,
        binding_status=binding_status,
        decision_status=decision_status,
        split=split,
        match_method=match_method,
        matched_titles=sorted({ref.external_title for ref in refs}),
        local_document_ids=sorted(_document_ids(refs)),
        local_chunk_ids=sorted({ref.chunk_id for ref in refs}),
        local_titles=sorted({ref.local_title for ref in refs}),
        trusted_task=task,
        notes=notes,
    )


def _build_title_index(chunks: list[CorpusChunk], aliases: list[tuple[str, CorpusChunk]]) -> _CorpusTitleIndex:
    exact: dict[str, list[_BindingRef]] = {}
    redirects: dict[str, list[_BindingRef]] = {}
    for chunk in chunks:
        exact.setdefault(_title_key(chunk.title), []).append(
            _BindingRef(
                method="exact",
                external_title=chunk.title,
                local_title=chunk.title,
                document_id=chunk.document_id,
                chunk_id=chunk.chunk_id,
            )
        )
    for alias, chunk in aliases:
        redirects.setdefault(_title_key(alias), []).append(
            _BindingRef(
                method="redirect",
                external_title=alias,
                local_title=chunk.title,
                document_id=chunk.document_id,
                chunk_id=chunk.chunk_id,
            )
        )
    return _CorpusTitleIndex(exact=exact, redirects=redirects)


def _extract_gold_titles(payload: dict[str, Any]) -> list[str]:
    titles: list[str] = []
    for key in ("title", "doc_title", "gold_title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            titles.append(value.strip())
    for key in ("gold_titles", "titles"):
        value = payload.get(key)
        if isinstance(value, list):
            titles.extend(str(item).strip() for item in value if str(item).strip())
    for key in ("positive_passages", "positive", "positives"):
        titles.extend(_titles_from_positive_payload(payload.get(key)))
    relevant = payload.get("relevant_docs")
    if isinstance(relevant, dict):
        titles.extend(str(key).strip() for key in relevant if str(key).strip())
    elif isinstance(relevant, list):
        titles.extend(str(item).strip() for item in relevant if str(item).strip())
    return _dedupe(titles)


def _titles_from_positive_payload(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    titles: list[str] = []
    for item in value:
        if isinstance(item, dict):
            title = item.get("title") or item.get("doc_title") or item.get("docid") or item.get("doc_id")
            if title:
                titles.append(str(title).strip())
        elif isinstance(item, str) and item.strip():
            titles.append(item.strip())
    return titles


def _first_ref_per_document(refs: list[_BindingRef]) -> list[_BindingRef]:
    by_document: dict[str, _BindingRef] = {}
    for ref in refs:
        by_document.setdefault(ref.document_id, ref)
    return list(by_document.values())


def _document_ids(refs: list[_BindingRef]) -> set[str]:
    return {ref.document_id for ref in refs}


def _title_key(title: str) -> str:
    return normalize_for_embedding(title)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _title_key(value)
        if normalized and normalized not in seen:
            result.append(value)
            seen.add(normalized)
    return result
