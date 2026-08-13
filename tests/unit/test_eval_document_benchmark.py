from __future__ import annotations

import argparse
import asyncio
import csv
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from wikipediarag.cli import _safe_cli_failure, run_eval_document_run
from wikipediarag.eval.api_client import HttpEvalApiClient, _client_request_id
from wikipediarag.eval.artifacts import read_json, read_jsonl, write_json
from wikipediarag.eval.document_benchmark import (
    RRNcBError,
    RRNcBPaths,
    _ingestion_item_failure_code,
    prepare_rrncb,
    rrncb_paths,
    run_rrncb,
)
from wikipediarag.eval.metrics import rouge_l, score_task
from wikipediarag.eval.schemas import CandidateRef, EvalConfig, EvalTask, EvalTaskResult


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


def test_eval_chat_idempotency_is_stable_within_a_run_and_isolated_across_runs() -> None:
    shared: dict[str, Any] = {
        "question": "Что такое Россия?",
        "retrieval_profile": "upload_sota_mvp",
        "retrieval_overrides": {},
        "mode": "normal",
        "knowledge_base_ids": ["kb-1"],
    }

    first = _client_request_id(**shared, request_namespace="rrncb-run-a")
    resumed = _client_request_id(**shared, request_namespace="rrncb-run-a")
    next_run = _client_request_id(**shared, request_namespace="rrncb-run-b")

    assert first == resumed
    assert first != next_run


def _rrncb_run_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, object], EvalConfig]:
    import wikipediarag.eval.document_benchmark as benchmark

    documents, csv_path = _write_fixture(tmp_path)
    suite = "rrncb-split-unit"
    prepare_rrncb(documents_dir=documents, csv_path=csv_path, suite=suite, artifacts_dir=tmp_path)
    paths = rrncb_paths(suite, tmp_path)
    manifest = read_json(paths.manifest)
    ingestion_base = paths.base / "ingestions" / "ingestion-unit"
    write_json(
        ingestion_base / "ingestion-status.json",
        {
            "status": "completed",
            "knowledge_base_id": "kb-rrncb",
            "run_id": "ingestion-unit",
            "dataset_hash": manifest["dataset_hash"],
        },
    )
    write_json(
        ingestion_base / "document-mapping.json",
        {
            "knowledge_base_id": "kb-rrncb",
            "dataset_hash": manifest["dataset_hash"],
            "documents": {
                f"doc-{index:02d}.pdf": {
                    "document_id": f"document-{index:02d}",
                    "document_version_id": f"version-{index:02d}",
                    "sha256": f"hash-{index:02d}",
                }
                for index in range(65)
            },
        },
    )
    config = EvalConfig(
        config_id="rrncb_upload_sota_mvp",
        retrieval_profile="upload_sota_mvp",
        retrieval_overrides={},
        mode="normal",
        config_hash="config-unit",
        model_aliases={"generator": "generator_main"},
    )

    async def fake_preflight(**kwargs: object) -> dict[str, object]:
        report: dict[str, object] = {
            "passed": True,
            "knowledge_base_id": "kb-rrncb",
            "profile": "upload_sota_mvp",
            "config_hash": config.config_hash,
            "dataset_hash": manifest["dataset_hash"],
            "index_contract_ids": ["index-unit"],
        }
        run_paths = cast(RRNcBPaths, kwargs["paths"])
        write_json(run_paths.base / "preflight.json", report)
        return report

    monkeypatch.setattr(benchmark, "_rrncb_preflight", fake_preflight)
    monkeypatch.setattr(benchmark, "_rrncb_config", lambda *_args: config)
    monkeypatch.setattr(benchmark, "_require_ready", lambda _api: {"status": "ok"})
    monkeypatch.setattr(benchmark, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(
        benchmark,
        "get_retrieval_profile",
        lambda *_args: SimpleNamespace(model_dump=lambda **_kwargs: {"name": "upload_sota_mvp"}),
    )
    monkeypatch.setattr(
        benchmark,
        "HttpEvalApiClient",
        SimpleNamespace(from_settings=lambda *_args, **_kwargs: SimpleNamespace(close=lambda: None)),
    )
    return paths.base, manifest, config


def test_rrncb_runs_dev_then_test_without_repeating_dev(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import wikipediarag.eval.document_benchmark as benchmark

    artifacts_dir, manifest, _config = _rrncb_run_fixture(tmp_path, monkeypatch)
    prepared_tasks = (artifacts_dir / "tasks.jsonl").read_bytes()
    prepared_manifest = (artifacts_dir / "tasks.manifest.json").read_bytes()
    calls: list[str] = []

    async def fake_run_task(task: EvalTask, _config: EvalConfig, **_kwargs: object) -> EvalTaskResult:
        calls.append(task.task_id)
        return EvalTaskResult(
            task_id=task.task_id,
            config_id=_config.config_id,
            config_hash=_config.config_hash,
            dataset_hash=str(manifest["dataset_hash"]),
            status="completed",
            question=task.question,
            server_terminal_event=True,
        )

    monkeypatch.setattr(benchmark, "run_task", fake_run_task)
    dev_report = asyncio.run(
        run_rrncb(
            suite="rrncb-split-unit",
            run_id="baseline-unit",
            ingestion_run_id="ingestion-unit",
            split="dev",
            artifacts_dir=tmp_path,
        )
    )
    assert dev_report["execution"]["status"] == "completed"
    assert dev_report["execution"]["completed_selected"] == 40
    assert len(calls) == 40

    test_report = asyncio.run(
        run_rrncb(
            suite="rrncb-split-unit",
            resume_run_id="baseline-unit",
            ingestion_run_id="ingestion-unit",
            split="test",
            artifacts_dir=tmp_path,
        )
    )
    assert test_report["execution"]["status"] == "completed"
    assert test_report["execution"]["completed_selected"] == 160
    assert len(calls) == 200
    assert len(read_jsonl(artifacts_dir / "runs" / "baseline-unit" / "results.jsonl", EvalTaskResult)) == 200
    assert (artifacts_dir / "tasks.jsonl").read_bytes() == prepared_tasks
    assert (artifacts_dir / "tasks.manifest.json").read_bytes() == prepared_manifest
    contract = read_json(artifacts_dir / "runs" / "baseline-unit" / "run-contract.json")
    assert contract["ingestion_run_id"] == "ingestion-unit"
    assert contract["mapping_hash"]


def test_ingestion_document_timeout_starts_only_after_running() -> None:
    old_timestamp = "2024-01-01T00:00:00+00:00"
    now = 2_000_000_000.0

    assert (
        _ingestion_item_failure_code(
            {"job_status": "received", "job_started_at": old_timestamp},
            now=now,
            document_timeout=900,
            heartbeat_timeout=60,
        )
        is None
    )
    assert (
        _ingestion_item_failure_code(
            {
                "job_status": "running",
                "job_started_at": old_timestamp,
                "job_last_heartbeat_at": "2033-05-18T03:33:20+00:00",
            },
            now=now,
            document_timeout=900,
            heartbeat_timeout=60,
        )
        == "INGESTION_DOCUMENT_TIMEOUT"
    )


def test_rrncb_cli_failure_preserves_only_explicit_safe_code() -> None:
    failure = _safe_cli_failure(
        RRNcBError("provider details must not be printed", safe_code="MODEL_OUTPUT_INVALID"),
        stage="cli",
    )

    assert failure == {
        "code": "MODEL_OUTPUT_INVALID",
        "retryable": False,
        "stage": "cli",
        "message": "operation failed",
    }


def test_rrncb_cli_run_emits_safe_terminal_failure_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import wikipediarag.eval.document_benchmark as benchmark

    async def fail_rrncb(**_kwargs: object) -> dict[str, object]:
        raise RRNcBError("provider details must not be printed", safe_code="MODEL_OUTPUT_INVALID")

    monkeypatch.setattr(benchmark, "run_rrncb", fail_rrncb)
    args = argparse.Namespace(
        suite="rrncb-unit",
        api="http://api",
        retrieval_profile="upload_sota_mvp",
        batch_size=1,
        question_timeout=300,
        suite_timeout=28800,
        rerun_failed=False,
        run_id="run-unit",
        resume_run_id=None,
        ingestion_run_id="ingestion-unit",
        split="dev",
        artifacts_dir="artifacts/eval",
    )

    with pytest.raises(SystemExit) as exit_code:
        run_eval_document_run(args)

    assert exit_code.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "failure": {
            "code": "MODEL_OUTPUT_INVALID",
            "retryable": False,
            "stage": "eval_document_run",
            "message": "operation failed",
        },
    }


def test_rrncb_stops_on_invalid_output_and_rejects_test_after_failed_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wikipediarag.eval.document_benchmark as benchmark

    artifacts_dir, manifest, _config = _rrncb_run_fixture(tmp_path, monkeypatch)
    calls: list[str] = []

    async def fake_invalid_task(task: EvalTask, _config: EvalConfig, **_kwargs: object) -> EvalTaskResult:
        calls.append(task.task_id)
        return EvalTaskResult(
            task_id=task.task_id,
            config_id=_config.config_id,
            config_hash=_config.config_hash,
            dataset_hash=str(manifest["dataset_hash"]),
            status="failed",
            question=task.question,
            failure_code="MODEL_OUTPUT_INVALID",
            server_terminal_event=True,
        )

    monkeypatch.setattr(benchmark, "run_task", fake_invalid_task)
    with pytest.raises(RRNcBError, match="MODEL_OUTPUT_INVALID"):
        asyncio.run(
            run_rrncb(
                suite="rrncb-split-unit",
                run_id="invalid-unit",
                ingestion_run_id="ingestion-unit",
                split="dev",
                artifacts_dir=tmp_path,
            )
        )
    assert len(calls) == 1
    status = read_json(artifacts_dir / "runs" / "invalid-unit" / "latest-status.json")
    assert status["status"] == "failed"
    assert status["failure"]["code"] == "MODEL_OUTPUT_INVALID"
    with pytest.raises(RRNcBError, match="successful completed dev"):
        asyncio.run(
            run_rrncb(
                suite="rrncb-split-unit",
                resume_run_id="invalid-unit",
                ingestion_run_id="ingestion-unit",
                split="test",
                artifacts_dir=tmp_path,
            )
        )


def test_rrncb_rejects_resume_when_the_run_contract_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import wikipediarag.eval.document_benchmark as benchmark

    artifacts_dir, manifest, _config = _rrncb_run_fixture(tmp_path, monkeypatch)

    async def fake_run_task(task: EvalTask, _config: EvalConfig, **_kwargs: object) -> EvalTaskResult:
        return EvalTaskResult(
            task_id=task.task_id,
            config_id=_config.config_id,
            config_hash=_config.config_hash,
            dataset_hash=str(manifest["dataset_hash"]),
            status="completed",
            question=task.question,
            server_terminal_event=True,
        )

    monkeypatch.setattr(benchmark, "run_task", fake_run_task)
    asyncio.run(
        run_rrncb(
            suite="rrncb-split-unit",
            run_id="contract-unit",
            ingestion_run_id="ingestion-unit",
            split="dev",
            artifacts_dir=tmp_path,
        )
    )
    contract_path = artifacts_dir / "runs" / "contract-unit" / "run-contract.json"
    contract = read_json(contract_path)
    contract["knowledge_base_id"] = "different-kb"
    write_json(contract_path, contract)
    with pytest.raises(RRNcBError, match="run contract mismatch"):
        asyncio.run(
            run_rrncb(
                suite="rrncb-split-unit",
                resume_run_id="contract-unit",
                ingestion_run_id="ingestion-unit",
                split="test",
                artifacts_dir=tmp_path,
            )
        )
