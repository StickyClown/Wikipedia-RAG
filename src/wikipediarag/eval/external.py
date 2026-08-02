from __future__ import annotations

import gzip
import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx
from pydantic import BaseModel, Field

from wikipediarag.config import Settings, get_settings
from wikipediarag.embedding import normalize_for_embedding
from wikipediarag.eval.artifacts import (
    ARTIFACT_ROOT,
    DATASET_VERSION,
    dataset_hash,
    utc_now_iso,
    write_dataset,
    write_json,
    write_jsonl,
)
from wikipediarag.eval.corpus import CorpusChunk, load_alias_chunks, load_candidate_chunks
from wikipediarag.eval.corpus import load_corpus_snapshot as default_load_corpus_snapshot
from wikipediarag.eval.hashing import stable_json_hash
from wikipediarag.eval.schemas import EvalDatasetManifest, EvalTask, GoldEvidence
from wikipediarag.eval.settings import adapt_eval_settings

BindingStatus = Literal["EXACT", "REDIRECT", "AMBIGUOUS", "MISSING", "LOW_TEXT_OVERLAP", "NO_POSITIVE_QREL"]
DecisionStatus = Literal["AUTO_ACCEPT", "REVIEW", "REJECT"]
CandidateSplit = Literal["train", "dev"]
CandidateReviewStatus = Literal["unreviewed"]
MiraclSplit = Literal["dev", "train"]

MIRACL_RU_SOURCE = "miracl-ru"
MIRACL_LOCAL_DATASET = "miracl-ru-local-v1"
EXTERNAL_TRANSFER_VERSION = "external_transfer_v2"
MIRACL_HF_DATASET_URL = "https://huggingface.co/datasets/miracl/miracl/resolve/main"
MIRACL_HF_CORPUS_URL = "https://huggingface.co/datasets/miracl/miracl-corpus/resolve/main"
MIRACL_RU_CORPUS_SHARDS = tuple(f"docs-{index}.jsonl.gz" for index in range(20))
DEFAULT_MIN_TEXT_OVERLAP = 0.08
TOKEN_RE = re.compile(r"[\wА-Яа-яЁё]+", re.UNICODE)


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
    local_section_ids: list[str] = Field(default_factory=list)
    local_chunk_ids: list[str] = Field(default_factory=list)
    local_titles: list[str] = Field(default_factory=list)
    local_overlap_score: float = 0.0
    trusted_task: TrustedTask | None = None
    eval_task: EvalTask | None = None
    notes: list[str] = Field(default_factory=list)


class MiraclQuestion(BaseModel):
    qid: str
    query: str
    positive_docids: list[str] = Field(default_factory=list)


class MiraclCorpusDoc(BaseModel):
    docid: str
    title: str
    text: str


class _BindingRef(BaseModel):
    method: Literal["exact", "redirect"]
    external_title: str
    local_title: str
    document_id: str
    section_id: str
    chunk_id: str


class _MatchedEvidence(BaseModel):
    ref: _BindingRef
    miracl_doc: MiraclCorpusDoc
    overlap_score: float


class _CorpusTitleIndex(BaseModel):
    exact: dict[str, list[_BindingRef]]
    redirects: dict[str, list[_BindingRef]]


async def transfer_miracl_ru(*, input_path: Path, settings: Settings | None = None) -> dict[str, Any]:
    resolved = adapt_eval_settings(settings or get_settings())
    snapshot = await default_load_corpus_snapshot(resolved)
    chunks = await load_candidate_chunks(limit=50000, settings=resolved)
    aliases = await load_alias_chunks(limit=50000, settings=resolved)
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
    manifest = _candidate_manifest(
        candidates,
        input_path=str(input_path),
        output_path=str(output_path),
        snapshot_id=snapshot.snapshot_id,
        index_version=snapshot.index_version,
        zim_checksum=snapshot.zim_checksum,
        retrieval_profile_hash=snapshot.retrieval_profile_hash,
        extra={"mode": "local_input"},
    )
    write_jsonl(output_path, candidates)
    write_json(manifest_path, manifest)
    write_json(output_dir / "latest.json", {"jsonl": str(output_path), "manifest": str(manifest_path)})
    return {"jsonl": str(output_path), "manifest": str(manifest_path), "count": len(candidates)}


async def transfer_miracl_ru_from_huggingface(
    *,
    split: MiraclSplit = "dev",
    limit: int = 100,
    output_suite: str = MIRACL_LOCAL_DATASET,
    cache_dir: Path | None = None,
    min_text_overlap: float = DEFAULT_MIN_TEXT_OVERLAP,
    settings: Settings | None = None,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("--limit must be >= 1")
    if min_text_overlap < 0.0 or min_text_overlap > 1.0:
        raise ValueError("--min-text-overlap must be between 0 and 1")
    resolved = adapt_eval_settings(settings or get_settings())
    snapshot = await default_load_corpus_snapshot(resolved)
    chunks = await load_candidate_chunks(limit=50000, settings=resolved)
    aliases = await load_alias_chunks(limit=50000, settings=resolved)
    actual_cache_dir = cache_dir or (ARTIFACT_ROOT / "external" / MIRACL_RU_SOURCE / "cache")
    download_report = await download_miracl_ru_huggingface(split=split, cache_dir=actual_cache_dir)
    topics = load_miracl_topics(download_report["topics_path"])
    positive_qrels = load_miracl_positive_qrels(download_report["qrels_path"])
    questions = [
        MiraclQuestion(qid=qid, query=query, positive_docids=positive_qrels.get(qid, [])) for qid, query in topics
    ]
    provenance = {
        "source_dataset": MIRACL_RU_SOURCE,
        "source_repo": "miracl/miracl",
        "source_corpus_repo": "miracl/miracl-corpus",
        "source_split": split,
        "snapshot_id": snapshot.snapshot_id,
        "index_version": snapshot.index_version,
        "zim_checksum": snapshot.zim_checksum,
        "retrieval_profile_hash": snapshot.retrieval_profile_hash,
    }
    build = build_miracl_local_dataset(
        questions,
        corpus_shards=download_report["corpus_shard_paths"],
        chunks=chunks,
        aliases=aliases,
        provenance=provenance,
        limit=limit,
        output_suite=output_suite,
        min_text_overlap=min_text_overlap,
    )
    run_id = _miracl_run_id(split=split, limit=limit, output_suite=output_suite, payload=build["manifest"])
    external_dir = ARTIFACT_ROOT / "external" / MIRACL_RU_SOURCE
    candidates_path = external_dir / f"{run_id}-candidates.jsonl"
    manifest_path = external_dir / f"{run_id}.manifest.json"
    manifest = {
        **build["manifest"],
        "transfer_version": EXTERNAL_TRANSFER_VERSION,
        "created_at": utc_now_iso(),
        "mode": "huggingface",
        "split": split,
        "limit": limit,
        "min_text_overlap": min_text_overlap,
        "cache_dir": str(actual_cache_dir),
        "topics_path": str(download_report["topics_path"]),
        "qrels_path": str(download_report["qrels_path"]),
        "corpus_shard_paths": [str(path) for path in download_report["corpus_shard_paths"]],
        "downloaded": download_report["downloaded"],
    }
    write_jsonl(candidates_path, build["candidates"])
    write_json(manifest_path, manifest)
    write_json(external_dir / "latest.json", {"jsonl": str(candidates_path), "manifest": str(manifest_path)})
    return {
        "jsonl": str(candidates_path),
        "manifest": str(manifest_path),
        "dataset": build["dataset_manifest"].model_dump(mode="json") if build["dataset_manifest"] else None,
        "dataset_jsonl": build["dataset_jsonl"],
        "dataset_manifest": build["dataset_manifest_path"],
        "accepted": build["accepted"],
        "candidate_count": build["candidate_count"],
        "by_binding_status": build["manifest"]["by_binding_status"],
        "by_decision_status": build["manifest"]["by_decision_status"],
    }


async def download_miracl_ru_huggingface(*, split: MiraclSplit, cache_dir: Path) -> dict[str, Any]:
    await anyio.Path(cache_dir).mkdir(parents=True, exist_ok=True)
    topics_path = cache_dir / f"topics.miracl-v1.0-ru-{split}.tsv"
    qrels_path = cache_dir / f"qrels.miracl-v1.0-ru-{split}.tsv"
    corpus_dir = cache_dir / "miracl-corpus-v1.0-ru"
    await anyio.Path(corpus_dir).mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []
    topics_url = f"{MIRACL_HF_DATASET_URL}/miracl-v1.0-ru/topics/{topics_path.name}"
    qrels_url = f"{MIRACL_HF_DATASET_URL}/miracl-v1.0-ru/qrels/{qrels_path.name}"
    if await _download_url_to_path(topics_url, topics_path):
        downloaded.append(str(topics_path))
    if await _download_url_to_path(qrels_url, qrels_path):
        downloaded.append(str(qrels_path))
    shard_paths: list[Path] = []
    for shard_name in MIRACL_RU_CORPUS_SHARDS:
        shard_path = corpus_dir / shard_name
        shard_url = f"{MIRACL_HF_CORPUS_URL}/miracl-corpus-v1.0-ru/{shard_name}"
        if await _download_url_to_path(shard_url, shard_path):
            downloaded.append(str(shard_path))
        shard_paths.append(shard_path)
    return {
        "topics_path": topics_path,
        "qrels_path": qrels_path,
        "corpus_shard_paths": shard_paths,
        "downloaded": downloaded,
    }


async def _download_url_to_path(url: str, path: Path) -> bool:
    async_path = anyio.Path(path)
    if await async_path.exists() and (await async_path.stat()).st_size > 0:
        return False
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    timeout = httpx.Timeout(30.0, read=300.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as file:
                async for chunk in response.aiter_bytes():
                    if chunk:
                        file.write(chunk)
    temporary.replace(path)
    return True


def build_miracl_local_dataset(
    questions: list[MiraclQuestion],
    *,
    corpus_shards: list[Path],
    chunks: list[CorpusChunk],
    aliases: list[tuple[str, CorpusChunk]],
    provenance: dict[str, str],
    limit: int,
    output_suite: str,
    min_text_overlap: float,
) -> dict[str, Any]:
    title_index = _build_title_index(chunks, aliases)
    chunks_by_document = _chunks_by_document(chunks)
    required_docids = {docid for question in questions for docid in question.positive_docids}
    corpus_by_docid: dict[str, MiraclCorpusDoc] = {}
    processed_qids: set[str] = set()
    candidates: list[CorpusBoundCandidate] = []
    tasks: list[EvalTask] = []
    no_positive = [question for question in questions if not question.positive_docids]
    for ordinal, question in enumerate(no_positive, start=1):
        candidate = _miracl_candidate(
            question,
            ordinal=ordinal,
            binding_status="NO_POSITIVE_QREL",
            decision_status="REJECT",
            match_method="none",
            matched=[],
            provenance=provenance,
            notes=["no positive qrel found for MIRACL question"],
        )
        candidates.append(candidate)
        processed_qids.add(question.qid)
    answerable = [question for question in questions if question.positive_docids]
    for shard_path in corpus_shards:
        for doc in iter_miracl_corpus_shard(shard_path):
            if doc.docid in required_docids:
                corpus_by_docid.setdefault(doc.docid, doc)
        for ordinal, question in enumerate(answerable, start=1):
            if question.qid in processed_qids:
                continue
            available_docs = [corpus_by_docid[docid] for docid in question.positive_docids if docid in corpus_by_docid]
            if not available_docs:
                continue
            candidate = bind_miracl_question_to_local(
                question,
                available_docs,
                title_index=title_index,
                chunks_by_document=chunks_by_document,
                provenance=provenance,
                ordinal=ordinal,
                min_text_overlap=min_text_overlap,
            )
            if candidate.eval_task is not None:
                candidates.append(candidate)
                tasks.append(candidate.eval_task)
                processed_qids.add(question.qid)
                if len(tasks) >= limit:
                    return _write_miracl_dataset(
                        candidates=candidates,
                        tasks=tasks,
                        output_suite=output_suite,
                        provenance=provenance,
                    )
            elif all(docid in corpus_by_docid for docid in question.positive_docids):
                candidates.append(candidate)
                processed_qids.add(question.qid)
    for ordinal, question in enumerate(answerable, start=1):
        if question.qid in processed_qids:
            continue
        candidate = _miracl_candidate(
            question,
            ordinal=ordinal,
            binding_status="MISSING",
            decision_status="REJECT",
            match_method="missing_corpus_doc",
            matched=[],
            provenance=provenance,
            notes=["positive MIRACL corpus doc was not found in downloaded shards"],
        )
        candidates.append(candidate)
    return _write_miracl_dataset(candidates=candidates, tasks=tasks, output_suite=output_suite, provenance=provenance)


def bind_miracl_question_to_local(
    question: MiraclQuestion,
    positive_docs: list[MiraclCorpusDoc],
    *,
    title_index: _CorpusTitleIndex,
    chunks_by_document: dict[str, list[CorpusChunk]],
    provenance: dict[str, str],
    ordinal: int,
    min_text_overlap: float,
) -> CorpusBoundCandidate:
    if not question.query.strip():
        return _miracl_candidate(
            question,
            ordinal=ordinal,
            binding_status="MISSING",
            decision_status="REJECT",
            match_method="empty_query",
            matched=[],
            provenance=provenance,
            notes=["empty MIRACL query"],
        )
    matched: list[_MatchedEvidence] = []
    notes: list[str] = []
    ambiguous_titles: list[str] = []
    missing_titles: list[str] = []
    low_overlap_titles: list[str] = []
    for doc in positive_docs:
        title_refs = _refs_for_title(doc.title, title_index)
        document_ids = _document_ids(title_refs)
        if len(document_ids) > 1:
            ambiguous_titles.append(doc.title)
            continue
        if not title_refs:
            missing_titles.append(doc.title)
            continue
        local_doc_id = next(iter(document_ids))
        best_ref, best_score = _best_local_chunk(
            doc,
            local_refs=title_refs,
            local_chunks=chunks_by_document.get(local_doc_id, []),
        )
        if best_ref is None or best_score < min_text_overlap:
            low_overlap_titles.append(f"{doc.title}:{best_score:.3f}")
            continue
        matched.append(_MatchedEvidence(ref=best_ref, miracl_doc=doc, overlap_score=best_score))
    if matched:
        return _miracl_candidate(
            question,
            ordinal=ordinal,
            binding_status="REDIRECT" if any(item.ref.method == "redirect" for item in matched) else "EXACT",
            decision_status="AUTO_ACCEPT",
            match_method="+".join(sorted({item.ref.method for item in matched})),
            matched=matched,
            provenance=provenance,
            notes=[],
        )
    if ambiguous_titles:
        return _miracl_candidate(
            question,
            ordinal=ordinal,
            binding_status="AMBIGUOUS",
            decision_status="REVIEW",
            match_method="ambiguous_title",
            matched=[],
            provenance=provenance,
            notes=[f"ambiguous local title binding: {title}" for title in ambiguous_titles],
        )
    if missing_titles:
        notes.extend(f"missing local title binding: {title}" for title in missing_titles)
    if low_overlap_titles:
        notes.extend(f"low text overlap: {title}" for title in low_overlap_titles)
    return _miracl_candidate(
        question,
        ordinal=ordinal,
        binding_status="LOW_TEXT_OVERLAP" if low_overlap_titles else "MISSING",
        decision_status="REJECT",
        match_method="low_text_overlap" if low_overlap_titles else "none",
        matched=[],
        provenance=provenance,
        notes=notes or ["no local binding accepted"],
    )


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


def load_miracl_topics(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            raise ValueError(f"invalid MIRACL topics row {line_number}: expected qid<TAB>query")
        rows.append((parts[0].strip(), parts[1].strip()))
    return rows


def load_miracl_positive_qrels(path: Path) -> dict[str, list[str]]:
    by_qid: dict[str, list[str]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            raise ValueError(f"invalid MIRACL qrels row {line_number}: expected qid Q0 docid relevance")
        qid, _unused, docid, raw_relevance = parts
        try:
            relevance = int(raw_relevance)
        except ValueError as exc:
            raise ValueError(f"invalid MIRACL qrels relevance at row {line_number}: {raw_relevance}") from exc
        if relevance > 0:
            by_qid.setdefault(qid, []).append(docid)
    return by_qid


def iter_miracl_corpus_shard(path: Path) -> Iterable[MiraclCorpusDoc]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"invalid MIRACL corpus row {line_number} in {path}")
            docid = str(payload.get("docid") or "")
            title = str(payload.get("title") or "")
            text = str(payload.get("text") or "")
            if docid and title:
                yield MiraclCorpusDoc(docid=docid, title=title, text=text)


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
        title_refs = _refs_for_title(title, title_index)
        if len(_document_ids(title_refs)) > 1:
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
        if title_refs:
            refs.extend(_first_ref_per_document(title_refs))
            methods.update(ref.method for ref in title_refs)
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
        local_section_ids=sorted({ref.section_id for ref in refs}),
        local_chunk_ids=sorted({ref.chunk_id for ref in refs}),
        local_titles=sorted({ref.local_title for ref in refs}),
        trusted_task=task,
        notes=notes,
    )


def _miracl_candidate(
    question: MiraclQuestion,
    *,
    ordinal: int,
    binding_status: BindingStatus,
    decision_status: DecisionStatus,
    match_method: str,
    matched: list[_MatchedEvidence],
    provenance: dict[str, str],
    notes: list[str],
) -> CorpusBoundCandidate:
    external_question = ExternalQuestion(
        external_id=question.qid,
        query=question.query,
        gold_titles=[item.miracl_doc.title for item in matched],
        raw={"positive_docids": question.positive_docids},
    )
    refs = [item.ref for item in matched]
    eval_task = _eval_task_from_miracl(question, matched, provenance=provenance, ordinal=ordinal) if matched else None
    return CorpusBoundCandidate(
        candidate_id=f"{MIRACL_RU_SOURCE}-{stable_json_hash([question.qid, ordinal], 16)[:12]}",
        external_question=external_question,
        binding_status=binding_status,
        decision_status=decision_status,
        split="train",
        match_method=match_method,
        matched_titles=sorted({ref.external_title for ref in refs}),
        local_document_ids=sorted(_document_ids(refs)),
        local_section_ids=sorted({ref.section_id for ref in refs}),
        local_chunk_ids=sorted({ref.chunk_id for ref in refs}),
        local_titles=sorted({ref.local_title for ref in refs}),
        local_overlap_score=max((item.overlap_score for item in matched), default=0.0),
        eval_task=eval_task,
        notes=notes,
    )


def _eval_task_from_miracl(
    question: MiraclQuestion,
    matched: list[_MatchedEvidence],
    *,
    provenance: dict[str, str],
    ordinal: int,
) -> EvalTask:
    gold_evidence = [
        GoldEvidence(
            evidence_id=f"miracl-{index}",
            document_id=item.ref.document_id,
            section_id=item.ref.section_id,
            chunk_id=item.ref.chunk_id,
            quote=item.miracl_doc.text[:500],
            supports_claim_ids=[f"miracl-qrel-{index}"],
            hop=index,
            title=item.ref.local_title,
            source_url="",
        )
        for index, item in enumerate(matched, start=1)
    ]
    return EvalTask(
        task_id=f"miracl-ru-{stable_json_hash([question.qid, ordinal], 16)[:12]}",
        question=question.query,
        task_family="single_hop_factual",
        reference_answer="",
        accepted_answers=[],
        unanswerable=False,
        expected_mode="normal_sufficient",
        gold_page_ids=sorted({item.ref.document_id for item in matched}),
        gold_section_ids=sorted({item.ref.section_id for item in matched}),
        gold_chunk_ids=sorted({item.ref.chunk_id for item in matched}),
        gold_evidence=gold_evidence,
        reasoning_path=[item.ref.local_title for item in matched],
        generator_alias="miracl_huggingface",
        verifier_alias="miracl_qrels",
        zim_checksum=provenance["zim_checksum"],
        snapshot_id=provenance["snapshot_id"],
        index_version=provenance["index_version"],
        retrieval_profile_hash=provenance["retrieval_profile_hash"],
        language="ru",
        tags=["miracl-ru", "external", "retrieval_only"],
        generation_seed=int(stable_json_hash([question.qid], 8), 16),
    )


def _write_miracl_dataset(
    *,
    candidates: list[CorpusBoundCandidate],
    tasks: list[EvalTask],
    output_suite: str,
    provenance: dict[str, str],
) -> dict[str, Any]:
    dataset_manifest: EvalDatasetManifest | None = None
    dataset_jsonl = ""
    dataset_manifest_path = ""
    if tasks:
        digest = dataset_hash(tasks)
        dataset_jsonl_path = ARTIFACT_ROOT / "datasets" / output_suite / f"{output_suite}-{digest[:12]}.jsonl"
        dataset_manifest = EvalDatasetManifest(
            dataset_name=output_suite,
            dataset_version=DATASET_VERSION,
            dataset_hash=digest,
            task_count=len(tasks),
            created_at=utc_now_iso(),
            snapshot_id=provenance["snapshot_id"],
            index_version=provenance["index_version"],
            zim_checksum=provenance["zim_checksum"],
            retrieval_profile_hash=provenance["retrieval_profile_hash"],
            generator_alias="miracl_huggingface",
            verifier_alias="miracl_qrels",
            jsonl_path=str(dataset_jsonl_path),
        )
        write_dataset(tasks, dataset_manifest)
        dataset_jsonl = str(dataset_jsonl_path)
        dataset_manifest_path = str(dataset_jsonl_path.with_suffix(".manifest.json"))
    manifest = _candidate_manifest(
        candidates,
        input_path="huggingface",
        output_path="",
        snapshot_id=provenance["snapshot_id"],
        index_version=provenance["index_version"],
        zim_checksum=provenance["zim_checksum"],
        retrieval_profile_hash=provenance["retrieval_profile_hash"],
        extra={
            "output_suite": output_suite,
            "dataset_jsonl": dataset_jsonl,
            "dataset_manifest": dataset_manifest_path,
            "accepted": len(tasks),
        },
    )
    return {
        "candidates": candidates,
        "dataset_manifest": dataset_manifest,
        "dataset_jsonl": dataset_jsonl,
        "dataset_manifest_path": dataset_manifest_path,
        "accepted": len(tasks),
        "candidate_count": len(candidates),
        "manifest": manifest,
    }


def _candidate_manifest(
    candidates: list[CorpusBoundCandidate],
    *,
    input_path: str,
    output_path: str,
    snapshot_id: str,
    index_version: str,
    zim_checksum: str,
    retrieval_profile_hash: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    binding_counts = Counter(candidate.binding_status for candidate in candidates)
    decision_counts = Counter(candidate.decision_status for candidate in candidates)
    by_reason = Counter(
        candidate.match_method if candidate.decision_status != "AUTO_ACCEPT" else "accepted" for candidate in candidates
    )
    return {
        "dataset_source": MIRACL_RU_SOURCE,
        "transfer_version": EXTERNAL_TRANSFER_VERSION,
        "created_at": utc_now_iso(),
        "input_path": input_path,
        "candidate_count": len(candidates),
        "jsonl_path": output_path,
        "snapshot_id": snapshot_id,
        "index_version": index_version,
        "zim_checksum": zim_checksum,
        "retrieval_profile_hash": retrieval_profile_hash,
        "by_binding_status": dict(sorted(binding_counts.items())),
        "by_decision_status": dict(sorted(decision_counts.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "split_policy": "retrieval_only_auto_accept_exact_or_redirect",
        **extra,
    }


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
                section_id=chunk.section_id,
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
                section_id=chunk.section_id,
                chunk_id=chunk.chunk_id,
            )
        )
    return _CorpusTitleIndex(exact=exact, redirects=redirects)


def _chunks_by_document(chunks: list[CorpusChunk]) -> dict[str, list[CorpusChunk]]:
    by_document: dict[str, list[CorpusChunk]] = {}
    for chunk in chunks:
        by_document.setdefault(chunk.document_id, []).append(chunk)
    return by_document


def _refs_for_title(title: str, title_index: _CorpusTitleIndex) -> list[_BindingRef]:
    normalized = _title_key(title)
    exact_refs = title_index.exact.get(normalized, [])
    if exact_refs:
        return exact_refs
    return title_index.redirects.get(normalized, [])


def _best_local_chunk(
    doc: MiraclCorpusDoc,
    *,
    local_refs: list[_BindingRef],
    local_chunks: list[CorpusChunk],
) -> tuple[_BindingRef | None, float]:
    if not local_refs:
        return None, 0.0
    method = local_refs[0].method
    external_title = local_refs[0].external_title
    local_title = local_refs[0].local_title
    best_chunk: CorpusChunk | None = None
    best_score = 0.0
    for chunk in local_chunks:
        score = _text_overlap_score(doc.text, chunk.content)
        if score > best_score:
            best_score = score
            best_chunk = chunk
    if best_chunk is None:
        ref = _first_ref_per_document(local_refs)[0]
        return ref, 0.0
    return (
        _BindingRef(
            method=method,
            external_title=external_title,
            local_title=local_title,
            document_id=best_chunk.document_id,
            section_id=best_chunk.section_id,
            chunk_id=best_chunk.chunk_id,
        ),
        best_score,
    )


def _text_overlap_score(left: str, right: str) -> float:
    left_tokens = _content_tokens(left)
    right_tokens = _content_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), 120)


def _content_tokens(value: str) -> set[str]:
    return {token for token in TOKEN_RE.findall(value.casefold()) if len(token) > 2}


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


def _document_ids(refs: Iterable[_BindingRef]) -> set[str]:
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


def _miracl_run_id(*, split: str, limit: int, output_suite: str, payload: dict[str, Any]) -> str:
    digest = stable_json_hash({"split": split, "limit": limit, "output_suite": output_suite, "payload": payload})[:12]
    return f"{MIRACL_RU_SOURCE}-{split}-{limit}-{digest}"
