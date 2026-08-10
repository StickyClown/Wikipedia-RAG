from __future__ import annotations

import csv
import json
from pathlib import Path

import httpx
import pytest

from wikipediarag.eval.api_client import HttpEvalApiClient
from wikipediarag.eval.artifacts import read_json, read_jsonl
from wikipediarag.eval.document_benchmark import RRNcBError, prepare_rrncb, rrncb_paths
from wikipediarag.eval.metrics import rouge_l, score_task
from wikipediarag.eval.schemas import CandidateRef, EvalTask


def _write_fixture(root: Path, *, rows: int = 200, pdfs: int = 65) -> tuple[Path, Path]:
    documents = root / "documents"
    documents.mkdir()
    for index in range(pdfs):
        (documents / f"doc-{index:02d}.pdf").write_bytes(f"PDF {index}".encode())
    csv_path = root / "rrncb.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["question", "answer", "document"])
        writer.writeheader()
        for index in range(rows):
            writer.writerow(
                {
                    "question": f"Вопрос {index}",
                    "answer": "В данном тексте не содержится информация для ответа на вопрос."
                    if index < 15
                    else f"Ответ {index}",
                    "document": f"doc-{index % 53:02d}.pdf",
                }
            )
    return documents, csv_path


def test_prepare_rrncb_validates_manifest_and_stable_split(tmp_path: Path) -> None:
    documents, csv_path = _write_fixture(tmp_path)

    first = prepare_rrncb(documents_dir=documents, csv_path=csv_path, suite="rrncb-unit")
    second = prepare_rrncb(documents_dir=documents, csv_path=csv_path, suite="rrncb-unit")

    assert first["dataset_hash"] == second["dataset_hash"]
    paths = rrncb_paths("rrncb-unit")
    tasks = read_jsonl(paths.tasks, EvalTask)
    assert len(tasks) == 200
    assert sum(task.split == "dev" for task in tasks) == 40
    assert sum(task.unanswerable for task in tasks) == 15
    assert all(task.evaluation_granularity == "document" for task in tasks)
    assert read_json(paths.source_manifest)["documents"][0]["sha256"]


def test_prepare_rrncb_rejects_missing_pdf(tmp_path: Path) -> None:
    documents, csv_path = _write_fixture(tmp_path)
    (documents / "doc-00.pdf").unlink()

    with pytest.raises(RRNcBError, match="exactly 65"):
        prepare_rrncb(documents_dir=documents, csv_path=csv_path, suite="rrncb-missing")


def test_document_metrics_and_rouge_l() -> None:
    task = EvalTask(
        task_id="rrncb-1",
        question="Что?",
        task_family="single_hop_factual",
        reference_answer="Москва столица России",
        accepted_answers=["Москва столица России"],
        unanswerable=False,
        expected_mode="normal_sufficient",
        gold_page_ids=[],
        gold_section_ids=[],
        gold_chunk_ids=[],
        gold_evidence=[],
        reasoning_path=[],
        generator_alias="generator_main",
        verifier_alias="verifier",
        zim_checksum="",
        snapshot_id="",
        index_version="upload",
        retrieval_profile_hash="",
        gold_document_ids=["doc-1"],
        evaluation_granularity="document",
    )
    candidates = [
        CandidateRef(chunk_id="c1", document_id="doc-noise", rank=1, stage="rerank"),
        CandidateRef(chunk_id="c2", document_id="doc-1", rank=2, stage="rerank"),
    ]
    scores = score_task(
        task,
        answer="Москва столица России",
        reranked=candidates,
        prefusion=candidates,
        cited_chunk_ids=[],
        cited_document_ids=["doc-1"],
        kiwix_url_ok=True,
    )
    assert scores.document_recall["5"] == 1.0
    assert scores.document_mrr_at_10 == 0.5
    assert scores.document_citation_precision == 1.0
    assert scores.rouge_l == 1.0
    assert rouge_l("Москва столица России", ["Москва столица России"]) == 1.0


def test_eval_api_client_scopes_chat_and_debug_to_knowledge_base(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.content
        seen.append((request.url.path, json.loads(payload)))
        if request.url.path.endswith("/chat"):
            body = (
                'event: run.started\ndata: {"query_run_id":"q1"}\n\n'
                'event: run.completed\ndata: {"data":{"answer":"ok"}}\n\n'
            )
            return httpx.Response(200, text=body)
        return httpx.Response(200, json={"evidence": []})

    client = HttpEvalApiClient()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    client.run_chat(
        "Что?",
        api="http://api",
        retrieval_profile="upload_sota_mvp",
        retrieval_overrides={},
        mode="normal",
        knowledge_base_ids=["kb-1"],
    )
    client.run_search_debug(
        "Что?",
        api="http://api",
        top_k=10,
        retrieval_profile="upload_sota_mvp",
        retrieval_overrides={},
        knowledge_base_ids=["kb-1"],
    )
    assert [payload["knowledge_base_ids"] for _, payload in seen] == [["kb-1"], ["kb-1"]]
