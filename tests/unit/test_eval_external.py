from __future__ import annotations

import gzip
import json
from pathlib import Path

import anyio
import pytest

from wikipediarag.config import Settings
from wikipediarag.eval.artifacts import read_jsonl
from wikipediarag.eval.corpus import CorpusChunk, CorpusSnapshot
from wikipediarag.eval.external import (
    MiraclQuestion,
    bind_external_questions,
    build_miracl_local_dataset,
    parse_miracl_ru_line,
    transfer_miracl_ru,
    transfer_miracl_ru_from_huggingface,
)
from wikipediarag.eval.schemas import EvalTask


def _chunk(title: str, document_id: str, chunk_id: str) -> CorpusChunk:
    return CorpusChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        section_id=chunk_id,
        title=title,
        content=f"{title} content " * 80,
        source_url=f"http://localhost/{chunk_id}",
        section_path=(title,),
        parent_chunk_id=chunk_id,
        prev_chunk_id=None,
        next_chunk_id=None,
        metadata={"zim_entry_path": title},
    )


def _chunk_with_content(title: str, document_id: str, chunk_id: str, content: str) -> CorpusChunk:
    return CorpusChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        section_id=f"section-{chunk_id}",
        title=title,
        content=content,
        source_url=f"http://localhost/{chunk_id}",
        section_path=(title,),
        parent_chunk_id=chunk_id,
        prev_chunk_id=None,
        next_chunk_id=None,
        metadata={"zim_entry_path": title},
    )


def _write_gzip_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


async def _fake_snapshot(_settings: Settings) -> CorpusSnapshot:
    return CorpusSnapshot(
        snapshot_id="snapshot",
        index_version="index",
        physical_index="physical",
        read_alias="alias",
        retrieval_profile="test_mock",
        retrieval_profile_hash="profile",
        embedding_alias="mock_embed_default",
        embedding_dimensions=64,
        zim_checksum="zim",
        zim_path=Path("wiki.zim"),
    )


def test_parse_miracl_jsonl_extracts_external_question() -> None:
    question = parse_miracl_ru_line(
        '{"query_id":"q1","query":"Что такое Россия?","positive_passages":[{"title":"Россия"}]}',
        index=1,
    )

    assert question.external_id == "q1"
    assert question.query == "Что такое Россия?"
    assert question.gold_titles == ["Россия"]


def test_parse_miracl_tsv_extracts_external_question() -> None:
    question = parse_miracl_ru_line("q2\tЧто такое Канада?\tКанада", index=2)

    assert question.external_id == "q2"
    assert question.query == "Что такое Канада?"
    assert question.gold_titles == ["Канада"]


def test_bind_external_questions_covers_exact_redirect_ambiguous_missing() -> None:
    exact = parse_miracl_ru_line('{"id":"exact","query":"q","title":"Россия"}', index=1)
    redirect = parse_miracl_ru_line('{"id":"redirect","query":"q","title":"РФ"}', index=2)
    ambiguous = parse_miracl_ru_line('{"id":"ambiguous","query":"q","title":"Город"}', index=3)
    missing = parse_miracl_ru_line('{"id":"missing","query":"q","title":"Нет такой статьи"}', index=4)

    candidates = bind_external_questions(
        [exact, redirect, ambiguous, missing],
        chunks=[
            _chunk("Россия", "doc-ru", "chunk-ru"),
            _chunk("Город", "doc-city-1", "chunk-city-1"),
            _chunk("Город", "doc-city-2", "chunk-city-2"),
        ],
        aliases=[("РФ", _chunk("Россия", "doc-ru", "chunk-ru"))],
        provenance={
            "snapshot_id": "snapshot",
            "index_version": "index",
            "zim_checksum": "zim",
            "retrieval_profile_hash": "profile",
        },
    )

    by_id = {candidate.external_question.external_id: candidate for candidate in candidates}
    assert by_id["exact"].binding_status == "EXACT"
    assert by_id["exact"].decision_status == "AUTO_ACCEPT"
    assert by_id["exact"].trusted_task is not None
    assert by_id["redirect"].binding_status == "REDIRECT"
    assert by_id["redirect"].decision_status == "AUTO_ACCEPT"
    assert by_id["ambiguous"].binding_status == "AMBIGUOUS"
    assert by_id["ambiguous"].decision_status == "REVIEW"
    assert by_id["ambiguous"].split == "dev"
    assert by_id["missing"].binding_status == "MISSING"
    assert by_id["missing"].decision_status == "REJECT"
    assert {candidate.split for candidate in candidates} <= {"train", "dev"}


@pytest.mark.asyncio
async def test_transfer_miracl_ru_writes_candidate_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    input_path = tmp_path / "miracl.jsonl"
    input_path.write_text('{"id":"q1","query":"Что такое Россия?","title":"Россия"}\n', encoding="utf-8")

    async def fake_snapshot(_settings: Settings) -> CorpusSnapshot:
        return CorpusSnapshot(
            snapshot_id="snapshot",
            index_version="index",
            physical_index="physical",
            read_alias="alias",
            retrieval_profile="test_mock",
            retrieval_profile_hash="profile",
            embedding_alias="mock_embed_default",
            embedding_dimensions=64,
            zim_checksum="zim",
            zim_path=tmp_path / "wiki.zim",
        )

    async def fake_chunks(*_args: object, **_kwargs: object) -> list[CorpusChunk]:
        return [_chunk("Россия", "doc-ru", "chunk-ru")]

    async def fake_aliases(*_args: object, **_kwargs: object) -> list[tuple[str, CorpusChunk]]:
        return []

    monkeypatch.setattr("wikipediarag.eval.external.ARTIFACT_ROOT", tmp_path / "eval")
    monkeypatch.setattr("wikipediarag.eval.external.default_load_corpus_snapshot", fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.external.load_candidate_chunks", fake_chunks)
    monkeypatch.setattr("wikipediarag.eval.external.load_alias_chunks", fake_aliases)

    report = await transfer_miracl_ru(input_path=input_path, settings=Settings())

    jsonl_path = Path(report["jsonl"])
    manifest_path = Path(report["manifest"])
    assert await anyio.Path(jsonl_path).exists()
    assert await anyio.Path(manifest_path).exists()
    row = await anyio.Path(jsonl_path).read_text(encoding="utf-8")
    assert '"binding_status":"EXACT"' in row
    assert '"decision_status":"AUTO_ACCEPT"' in row
    assert '"split":"test"' not in row


def test_build_miracl_local_dataset_records_accept_and_rejection_reasons(tmp_path: Path) -> None:
    shard = tmp_path / "docs-0.jsonl.gz"
    _write_gzip_jsonl(
        shard,
        [
            {"docid": "doc-exact", "title": "Россия", "text": "Москва столица России федерация"},
            {"docid": "doc-low", "title": "Канада", "text": "Оттава столица Канады"},
            {"docid": "doc-missing-title", "title": "Нет такой статьи", "text": "локальный текст"},
            {"docid": "doc-ambiguous", "title": "Город", "text": "город населенный пункт"},
        ],
    )
    chunks = [
        _chunk_with_content("Россия", "local-ru", "chunk-ru", "Москва столица России федерация"),
        _chunk_with_content("Канада", "local-ca", "chunk-ca", "совершенно другой локальный фрагмент"),
        _chunk_with_content("Город", "local-city-1", "chunk-city-1", "город населенный пункт"),
        _chunk_with_content("Город", "local-city-2", "chunk-city-2", "город населенный пункт"),
    ]

    build = build_miracl_local_dataset(
        [
            MiraclQuestion(qid="q0", query="Без релевантных документов", positive_docids=[]),
            MiraclQuestion(qid="q1", query="Что является столицей России?", positive_docids=["doc-exact"]),
            MiraclQuestion(qid="q2", query="Что является столицей Канады?", positive_docids=["doc-low"]),
            MiraclQuestion(qid="q3", query="Где нет статьи?", positive_docids=["doc-missing-title"]),
            MiraclQuestion(qid="q4", query="Что такое город?", positive_docids=["doc-ambiguous"]),
            MiraclQuestion(qid="q5", query="Где нет corpus doc?", positive_docids=["doc-not-downloaded"]),
        ],
        corpus_shards=[shard],
        chunks=chunks,
        aliases=[],
        provenance={
            "snapshot_id": "snapshot",
            "index_version": "index",
            "zim_checksum": "zim",
            "retrieval_profile_hash": "profile",
        },
        limit=100,
        output_suite="miracl-test",
        min_text_overlap=0.5,
    )

    assert build["accepted"] == 1
    by_status = build["manifest"]["by_binding_status"]
    assert by_status["EXACT"] == 1
    assert by_status["NO_POSITIVE_QREL"] == 1
    assert by_status["LOW_TEXT_OVERLAP"] == 1
    assert by_status["MISSING"] == 2
    assert by_status["AMBIGUOUS"] == 1
    tasks = read_jsonl(Path(build["dataset_jsonl"]), EvalTask)
    assert tasks[0].tags == ["miracl-ru", "external", "retrieval_only"]
    assert tasks[0].gold_page_ids == ["local-ru"]
    assert tasks[0].gold_section_ids == ["section-chunk-ru"]
    assert tasks[0].gold_chunk_ids == ["chunk-ru"]


@pytest.mark.asyncio
async def test_transfer_miracl_ru_from_huggingface_downloads_cache_and_limits_to_accepted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    writes = 0

    async def fake_download(url: str, path: Path) -> bool:
        nonlocal writes
        async_path = anyio.Path(path)
        if await async_path.exists() and (await async_path.stat()).st_size > 0:
            return False
        writes += 1
        await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
        if "topics" in url:
            await async_path.write_text(
                "\n".join(f"q{index}\tКакой номер у статьи {index}?" for index in range(1, 106)) + "\n",
                encoding="utf-8",
            )
        elif "qrels" in url:
            await async_path.write_text(
                "\n".join(f"q{index} Q0 doc-{index} 1" for index in range(1, 106)) + "\n",
                encoding="utf-8",
            )
        elif path.name == "docs-0.jsonl.gz":
            _write_gzip_jsonl(
                path,
                [
                    {
                        "docid": f"doc-{index}",
                        "title": f"Статья {index}",
                        "text": f"номер статьи {index} общий проверочный текст",
                    }
                    for index in range(1, 106)
                ],
            )
        else:
            _write_gzip_jsonl(path, [])
        return True

    async def fake_chunks(*_args: object, **_kwargs: object) -> list[CorpusChunk]:
        return [
            _chunk_with_content(
                f"Статья {index}",
                f"local-{index}",
                f"chunk-{index}",
                f"номер статьи {index} общий проверочный текст",
            )
            for index in range(1, 106)
        ]

    async def fake_aliases(*_args: object, **_kwargs: object) -> list[tuple[str, CorpusChunk]]:
        return []

    monkeypatch.setattr("wikipediarag.eval.external.ARTIFACT_ROOT", tmp_path / "eval")
    monkeypatch.setattr("wikipediarag.eval.external.default_load_corpus_snapshot", _fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.external.load_candidate_chunks", fake_chunks)
    monkeypatch.setattr("wikipediarag.eval.external.load_alias_chunks", fake_aliases)
    monkeypatch.setattr("wikipediarag.eval.external._download_url_to_path", fake_download)

    first = await transfer_miracl_ru_from_huggingface(
        split="dev",
        limit=100,
        output_suite="miracl-ru-local-v1",
        cache_dir=tmp_path / "cache",
        min_text_overlap=0.5,
        settings=Settings(),
    )
    writes_after_first = writes
    second = await transfer_miracl_ru_from_huggingface(
        split="dev",
        limit=100,
        output_suite="miracl-ru-local-v1",
        cache_dir=tmp_path / "cache",
        min_text_overlap=0.5,
        settings=Settings(),
    )

    assert first["accepted"] == 100
    assert second["accepted"] == 100
    assert writes == writes_after_first
    tasks = read_jsonl(Path(first["dataset_jsonl"]), EvalTask)
    assert len(tasks) == 100
    assert tasks[-1].task_id.startswith("miracl-ru-")
    assert first["by_binding_status"] == {"EXACT": 100}


@pytest.mark.asyncio
async def test_transfer_miracl_ru_from_huggingface_accepts_redirect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_download(url: str, path: Path) -> bool:
        async_path = anyio.Path(path)
        if await async_path.exists() and (await async_path.stat()).st_size > 0:
            return False
        await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
        if "topics" in url:
            await async_path.write_text("q1\tЧто такое РФ?\n", encoding="utf-8")
        elif "qrels" in url:
            await async_path.write_text("q1 Q0 doc-rf 1\n", encoding="utf-8")
        elif path.name == "docs-0.jsonl.gz":
            _write_gzip_jsonl(path, [{"docid": "doc-rf", "title": "РФ", "text": "Россия федерация государство"}])
        else:
            _write_gzip_jsonl(path, [])
        return True

    local_chunk = _chunk_with_content("Россия", "local-ru", "chunk-ru", "Россия федерация государство")

    async def fake_chunks(*_args: object, **_kwargs: object) -> list[CorpusChunk]:
        return [local_chunk]

    async def fake_aliases(*_args: object, **_kwargs: object) -> list[tuple[str, CorpusChunk]]:
        return [("РФ", local_chunk)]

    monkeypatch.setattr("wikipediarag.eval.external.ARTIFACT_ROOT", tmp_path / "eval")
    monkeypatch.setattr("wikipediarag.eval.external.default_load_corpus_snapshot", _fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.external.load_candidate_chunks", fake_chunks)
    monkeypatch.setattr("wikipediarag.eval.external.load_alias_chunks", fake_aliases)
    monkeypatch.setattr("wikipediarag.eval.external._download_url_to_path", fake_download)

    result = await transfer_miracl_ru_from_huggingface(
        split="dev",
        limit=1,
        output_suite="miracl-ru-local-v1",
        cache_dir=tmp_path / "cache",
        min_text_overlap=0.5,
        settings=Settings(),
    )

    assert result["accepted"] == 1
    assert result["by_binding_status"] == {"REDIRECT": 1}
