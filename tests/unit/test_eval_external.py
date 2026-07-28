from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from wikipediarag.config import Settings
from wikipediarag.eval.corpus import CorpusChunk, CorpusSnapshot
from wikipediarag.eval.external import (
    bind_external_questions,
    parse_miracl_ru_line,
    transfer_miracl_ru,
)


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
