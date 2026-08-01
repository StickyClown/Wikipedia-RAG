from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import httpx

from wikipediarag.document_corpus import (
    DocumentCorpusItem,
    corpus_summary,
    load_manifest_corpus,
    materialize_corpus_item,
    synthetic_document_corpus,
)
from wikipediarag.eval.schemas import TaskFamily


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wikipediarag")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-wiki")
    import_parser.add_argument("--limit", type=int, default=None)
    import_parser.add_argument("--full", action="store_true")
    import_parser.add_argument("--wait", action="store_true")
    import_parser.add_argument("--api", default="http://localhost:8000")

    import_zim_parser = subparsers.add_parser("import-zim")
    import_zim_parser.add_argument("--limit", type=int, default=10000)
    import_zim_parser.add_argument("--zim-path", default=None)
    import_zim_parser.add_argument("--zim-filename", default=None)
    import_zim_parser.add_argument("--wait", action="store_true")
    import_zim_parser.add_argument("--api", default="http://localhost:8000")

    smoke_parser = subparsers.add_parser("smoke")
    smoke_parser.add_argument("--api", default="http://localhost:8000")

    eval_parser = subparsers.add_parser("eval")
    eval_parser.add_argument("--api", default="http://localhost:8000")

    eval_smoke_parser = subparsers.add_parser("eval-smoke")
    eval_smoke_parser.add_argument("--count", type=int, default=10)
    eval_smoke_parser.add_argument("--api", default="http://localhost:8000")

    eval_generate_parser = subparsers.add_parser("eval-generate")
    eval_generate_parser.add_argument("--count", type=int, default=None)
    _add_eval_generate_arguments(eval_generate_parser)

    eval_generate_status_parser = subparsers.add_parser("eval-generate-status")
    status_group = eval_generate_status_parser.add_mutually_exclusive_group(required=True)
    status_group.add_argument("--run-id", default=None)
    status_group.add_argument("--latest", action="store_true")
    eval_generate_status_parser.add_argument("--json", action="store_true")

    eval_run_parser = subparsers.add_parser("eval-run")
    eval_run_parser.add_argument("--suite", default="generated-wikipedia-v1")
    eval_run_parser.add_argument("--api", default="http://localhost:8000")
    eval_run_parser.add_argument("--batch-size", type=int, default=6)

    eval_report_parser = subparsers.add_parser("eval-report")
    eval_report_parser.add_argument("--latest", action="store_true")

    eval_retrieval_run_parser = subparsers.add_parser("eval-retrieval-run")
    eval_retrieval_run_parser.add_argument("--suite", default="generated-wikipedia-v1")
    eval_retrieval_run_parser.add_argument("--api", default="http://localhost:8000")
    eval_retrieval_run_parser.add_argument("--batch-size", type=int, default=10)
    eval_retrieval_run_parser.add_argument("--run-id", default=None)
    eval_retrieval_run_parser.add_argument("--resume-run-id", default=None)
    eval_retrieval_run_parser.add_argument("--rerun-failed", action="store_true")

    eval_retrieval_status_parser = subparsers.add_parser("eval-retrieval-status")
    retrieval_status_group = eval_retrieval_status_parser.add_mutually_exclusive_group(required=True)
    retrieval_status_group.add_argument("--run-id", default=None)
    retrieval_status_group.add_argument("--latest", action="store_true")
    eval_retrieval_status_parser.add_argument("--json", action="store_true")

    eval_retrieval_report_parser = subparsers.add_parser("eval-retrieval-report")
    eval_retrieval_report_parser.add_argument("--latest", action="store_true")

    subparsers.add_parser("eval-trusted-catalog")

    eval_trusted_generate_parser = subparsers.add_parser("eval-trusted-generate")
    eval_trusted_generate_parser.add_argument("--count", type=int, default=None)
    eval_trusted_generate_parser.add_argument("--concurrency", type=int, default=None)
    eval_trusted_generate_parser.add_argument("--rejection-budget", type=int, default=30)
    eval_trusted_generate_parser.add_argument("--generator-alias", default=None)
    eval_trusted_generate_parser.add_argument("--verifier-alias", default=None)
    eval_trusted_generate_parser.add_argument(
        "--family-weight",
        action="append",
        default=[],
        help="repeatable, format trusted_family=weight",
    )
    eval_trusted_generate_parser.add_argument("--run-id", default=None)
    eval_trusted_generate_parser.add_argument("--resume-run-id", default=None)
    eval_trusted_generate_parser.add_argument("--takeover-stale-run", action="store_true")

    eval_trusted_status_parser = subparsers.add_parser("eval-trusted-status")
    trusted_status_group = eval_trusted_status_parser.add_mutually_exclusive_group(required=True)
    trusted_status_group.add_argument("--run-id", default=None)
    trusted_status_group.add_argument("--latest", action="store_true")
    eval_trusted_status_parser.add_argument("--json", action="store_true")

    eval_trusted_pool_parser = subparsers.add_parser("eval-trusted-pool")
    eval_trusted_pool_parser.add_argument("--suite", default="trusted-wikipedia-v2")
    eval_trusted_pool_parser.add_argument("--api", default="http://localhost:8000")
    eval_trusted_pool_parser.add_argument("--batch-size", type=int, default=10)
    eval_trusted_pool_parser.add_argument("--run-id", default=None)
    eval_trusted_pool_parser.add_argument("--resume-run-id", default=None)
    eval_trusted_pool_parser.add_argument("--rerun-failed", action="store_true")

    eval_trusted_report_parser = subparsers.add_parser("eval-trusted-report")
    eval_trusted_report_parser.add_argument("--suite", default="trusted-wikipedia-v2")

    eval_miracl_parser = subparsers.add_parser("eval-miracl-map")
    eval_miracl_parser.add_argument("--input", default=None)
    eval_miracl_parser.add_argument("--from-huggingface", action="store_true")
    eval_miracl_parser.add_argument("--split", choices=["dev", "train"], default="dev")
    eval_miracl_parser.add_argument("--limit", type=int, default=100)
    eval_miracl_parser.add_argument("--output-suite", default="miracl-ru-local-v1")
    eval_miracl_parser.add_argument("--cache-dir", default="artifacts/eval/external/miracl-ru/cache")
    eval_miracl_parser.add_argument("--min-text-overlap", type=float, default=0.08)

    eval_review_parser = subparsers.add_parser("eval-review-candidates")
    eval_review_parser.add_argument("--input", required=True)
    eval_review_parser.add_argument("--output-suite", required=True)

    eval_freeze_parser = subparsers.add_parser("eval-freeze-reviewed")
    eval_freeze_parser.add_argument("--suite", required=True)
    eval_freeze_parser.add_argument("--dev-count", type=int, required=True)
    eval_freeze_parser.add_argument("--test-count", type=int, required=True)

    eval_release_gate_parser = subparsers.add_parser("eval-release-gate")
    eval_release_gate_parser.add_argument("--suite", required=True)
    eval_release_gate_parser.add_argument("--api", default="http://localhost:8000")

    eval_release_gate_status_parser = subparsers.add_parser("eval-release-gate-status")
    eval_release_gate_status_parser.add_argument("--suite", required=True)
    eval_release_gate_status_parser.add_argument("--report-id", default=None)
    eval_release_gate_status_parser.add_argument("--json", action="store_true")

    eval_task_diagnostics_parser = subparsers.add_parser("eval-task-diagnostics")
    eval_task_diagnostics_parser.add_argument("--suite", required=True)
    eval_task_diagnostics_parser.add_argument("--split", choices=["dev", "test"], default="test")
    eval_task_diagnostics_parser.add_argument("--config-id", default="sota_mvp_normal")
    eval_task_diagnostics_parser.add_argument("--task-id", action="append", required=True)
    eval_task_diagnostics_parser.add_argument("--json", action="store_true")

    eval_reviewed_short_parser = subparsers.add_parser("eval-reviewed-short")
    eval_reviewed_short_parser.add_argument("--suite", required=True)
    eval_reviewed_short_parser.add_argument("--split", choices=["dev", "test"], default="test")
    eval_reviewed_short_parser.add_argument("--api", default="http://localhost:8000")
    eval_reviewed_short_parser.add_argument("--config-id", default="sota_mvp_normal")
    eval_reviewed_short_parser.add_argument("--task-id", action="append", required=True)
    eval_reviewed_short_parser.add_argument("--batch-size", type=int, default=6)
    eval_reviewed_short_parser.add_argument("--retrieval-batch-size", type=int, default=10)
    eval_reviewed_short_parser.add_argument("--skip-answer", action="store_true")
    eval_reviewed_short_parser.add_argument("--skip-retrieval", action="store_true")

    eval_profile_retrieval_parser = subparsers.add_parser("eval-profile-retrieval")
    eval_profile_retrieval_parser.add_argument("--suite", default="reviewed-wikipedia-smoke-v1")
    eval_profile_retrieval_parser.add_argument("--split", choices=["dev", "test"], default="dev")
    eval_profile_retrieval_parser.add_argument("--api", default="http://localhost:8000")
    eval_profile_retrieval_parser.add_argument("--config-id", default="sota_mvp_normal")
    eval_profile_retrieval_parser.add_argument("--task-id", action="append", default=[])
    eval_profile_retrieval_parser.add_argument("--limit", type=int, default=5)
    eval_profile_retrieval_parser.add_argument("--warmup-iterations", type=int, default=1)
    eval_profile_retrieval_parser.add_argument("--measured-iterations", type=int, default=1)
    eval_profile_retrieval_parser.add_argument("--batch-size", type=int, default=5)

    eval_full_parser = subparsers.add_parser("eval-full")
    eval_full_parser.add_argument("--count", type=int, default=None)
    eval_full_parser.add_argument("--api", default="http://localhost:8000")
    _add_eval_generate_arguments(eval_full_parser)

    models_parser = subparsers.add_parser("smoke-models")
    models_parser.add_argument("--provider", default="mock")
    models_parser.add_argument("--gateway", default="http://localhost:8081")

    gate_parser = subparsers.add_parser("release-gate")
    gate_parser.add_argument("--api", default="http://localhost:8000")

    demo_gate_parser = subparsers.add_parser("demo-release-gate")
    demo_gate_parser.add_argument("--api", default="http://localhost:8000")
    demo_gate_parser.add_argument(
        "--job-id",
        default=os.environ.get("ZIM_IMPORT_JOB_ID"),
        help="validate an existing completed ZIM import instead of creating another one",
    )

    verify_upload_parser = subparsers.add_parser("verify-document-upload")
    verify_upload_parser.add_argument("--api", default="http://localhost:8000")
    verify_upload_parser.add_argument("--xberg", default=os.environ.get("XBERG_PUBLIC_URL", "http://localhost:8091"))
    verify_upload_parser.add_argument(
        "--docling",
        default=os.environ.get("DOCLING_PUBLIC_URL", "http://localhost:8092"),
    )
    verify_upload_parser.add_argument(
        "--metadata-service",
        default=os.environ.get("METADATA_SERVICE_PUBLIC_URL", "http://localhost:8090"),
    )
    verify_upload_parser.add_argument(
        "--admin-username",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_USERNAME", "admin"),
        help="local or hybrid auth platform-admin username",
    )
    verify_upload_parser.add_argument(
        "--admin-secret",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_PASSWORD", "admin"),  # noqa: S106
        help="platform-admin local login secret; defaults to the local bootstrap admin password",
    )
    verify_upload_parser.add_argument(
        "--admin-secret-file",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_PASSWORD_FILE"),
        help="file containing the platform-admin local login secret, or WIKIPEDIARAG_ADMIN_PASSWORD_FILE",
    )
    verify_upload_parser.add_argument("--skip-compose", action="store_true")
    verify_upload_parser.add_argument("--down-after", action="store_true")

    verify_corpus_parser = subparsers.add_parser("verify-document-corpus")
    verify_corpus_parser.add_argument("--api", default="http://localhost:8000")
    verify_corpus_parser.add_argument("--xberg", default=os.environ.get("XBERG_PUBLIC_URL", "http://localhost:8091"))
    verify_corpus_parser.add_argument(
        "--docling",
        default=os.environ.get("DOCLING_PUBLIC_URL", "http://localhost:8092"),
    )
    verify_corpus_parser.add_argument(
        "--metadata-service",
        default=os.environ.get("METADATA_SERVICE_PUBLIC_URL", "http://localhost:8090"),
    )
    verify_corpus_parser.add_argument("--fixture-set", choices=["smoke", "standard", "full"], default="standard")
    verify_corpus_parser.add_argument("--skip-negative", action="store_true")
    verify_corpus_parser.add_argument("--include-external", action="store_true")
    verify_corpus_parser.add_argument("--include-disabled-external", action="store_true")
    verify_corpus_parser.add_argument("--manifest", default="config/document_corpus_manifest.json")
    verify_corpus_parser.add_argument("--cache-dir", default="artifacts/corpora/document-corpus")
    verify_corpus_parser.add_argument("--max-documents", type=int, default=None)
    verify_corpus_parser.add_argument("--skip-compose", action="store_true")
    verify_corpus_parser.add_argument("--down-after", action="store_true")

    hardening_parser = subparsers.add_parser("verify-cross-tenant-hardening")
    hardening_parser.add_argument("--api", default="http://localhost:8000")
    hardening_parser.add_argument(
        "--admin-username",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_USERNAME", "admin"),
        help="local or hybrid auth platform-admin username",
    )
    hardening_parser.add_argument(
        "--admin-secret",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_PASSWORD", "admin"),  # noqa: S106
        help="platform-admin local login secret; defaults to the local bootstrap admin password",
    )
    hardening_parser.add_argument(
        "--admin-secret-file",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_PASSWORD_FILE"),
        help="file containing the platform-admin local login secret, or WIKIPEDIARAG_ADMIN_PASSWORD_FILE",
    )
    hardening_parser.add_argument("--skip-compose", action="store_true")
    hardening_parser.add_argument("--down-after", action="store_true")
    return parser


def main() -> None:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "import-wiki":
        import_wiki(args)
    elif args.command == "import-zim":
        import_zim(args)
    elif args.command == "smoke":
        smoke(args.api)
    elif args.command == "eval":
        run_eval(args.api)
    elif args.command == "eval-smoke":
        run_eval_smoke(args.count, args.api)
    elif args.command == "eval-generate":
        run_eval_generate(args)
    elif args.command == "eval-generate-status":
        run_eval_generate_status(args.run_id, args.latest, args.json)
    elif args.command == "eval-run":
        run_eval_run(args.suite, args.api, args.batch_size)
    elif args.command == "eval-report":
        run_eval_report(args.latest)
    elif args.command == "eval-retrieval-run":
        run_eval_retrieval(args)
    elif args.command == "eval-retrieval-status":
        run_eval_retrieval_status(args.run_id, args.latest, args.json)
    elif args.command == "eval-retrieval-report":
        run_eval_retrieval_report(args.latest)
    elif args.command == "eval-trusted-catalog":
        run_eval_trusted_catalog()
    elif args.command == "eval-trusted-generate":
        run_eval_trusted_generate(args)
    elif args.command == "eval-trusted-status":
        run_eval_trusted_status(args.run_id, args.latest, args.json)
    elif args.command == "eval-trusted-pool":
        run_eval_trusted_pool(args)
    elif args.command == "eval-trusted-report":
        run_eval_trusted_report(args.suite)
    elif args.command == "eval-miracl-map":
        run_eval_miracl_map(args)
    elif args.command == "eval-review-candidates":
        run_eval_review_candidates(args.input, args.output_suite)
    elif args.command == "eval-freeze-reviewed":
        run_eval_freeze_reviewed(args.suite, args.dev_count, args.test_count)
    elif args.command == "eval-release-gate":
        run_eval_release_gate(args.suite, args.api)
    elif args.command == "eval-release-gate-status":
        run_eval_release_gate_status(args.suite, args.report_id, args.json)
    elif args.command == "eval-task-diagnostics":
        run_eval_task_diagnostics(args.suite, args.split, args.config_id, list(args.task_id), args.json)
    elif args.command == "eval-reviewed-short":
        run_eval_reviewed_short(args)
    elif args.command == "eval-profile-retrieval":
        run_eval_profile_retrieval(args)
    elif args.command == "eval-full":
        run_eval_full(args)
    elif args.command == "smoke-models":
        smoke_models(args.gateway, args.provider)
    elif args.command == "release-gate":
        run_eval(args.api)
        print("release gate passed")
    elif args.command == "demo-release-gate":
        demo_release_gate(args.api, args.job_id)
    elif args.command == "verify-document-upload":
        verify_document_upload(args)
    elif args.command == "verify-document-corpus":
        verify_document_corpus(args)
    elif args.command == "verify-cross-tenant-hardening":
        verify_cross_tenant_hardening(args)


def _add_eval_generate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--generator-alias", default=None)
    parser.add_argument("--verifier-alias", default=None)
    parser.add_argument(
        "--family-weight",
        action="append",
        default=[],
        help="repeatable, format family=weight",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--resume-run-id", default=None)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def _parse_family_weight_specs(specs: list[str]) -> dict[TaskFamily, float] | None:
    if not specs:
        return None
    from wikipediarag.eval.generate_runs import FAMILY_ORDER

    known = set(FAMILY_ORDER)
    weights: dict[TaskFamily, float] = {family: 0.0 for family in FAMILY_ORDER}
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"invalid --family-weight value: {spec}")
        family, raw_weight = spec.split("=", 1)
        name = family.strip()
        if name not in known:
            raise ValueError(f"unknown task family in --family-weight: {name}")
        try:
            weight = float(raw_weight.strip())
        except ValueError as exc:
            raise ValueError(f"invalid weight for family {name}: {raw_weight}") from exc
        if weight < 0:
            raise ValueError(f"family weight must be >= 0 for {name}")
        weights[name] = weight
    return weights


def import_wiki(args: argparse.Namespace) -> None:
    limit = None if args.full else args.limit
    if limit is None and not args.full:
        limit = 10000
    with httpx.Client(timeout=30) as client:
        response = client.post(f"{args.api}/api/v1/wikipedia/imports", json={"limit": limit})
        response.raise_for_status()
        job_id = response.json()["job_id"]
        print(f"created wiki import job {job_id}")
        if args.wait:
            wait_for_job(client, args.api, job_id)


def import_zim(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {"limit": args.limit}
    if args.zim_path:
        payload["zim_path"] = args.zim_path
    if args.zim_filename:
        payload["zim_filename"] = args.zim_filename
    with httpx.Client(timeout=30) as client:
        response = client.post(f"{args.api}/api/v1/wikipedia/zim-imports", json=payload)
        response.raise_for_status()
        job_id = response.json()["job_id"]
        print(f"created ZIM import job {job_id}")
        if args.wait:
            wait_for_job(client, args.api, job_id)


def wait_for_job(client: httpx.Client, api: str, job_id: str) -> None:
    while True:
        response = client.get(f"{api}/api/v1/ingestion-jobs/{job_id}")
        response.raise_for_status()
        job = response.json()
        progress = job.get("progress", {})
        print(
            "status={status} pages_imported={pages} chunks_indexed={chunks}".format(
                status=job["status"],
                pages=progress.get("pages_imported", 0),
                chunks=progress.get("chunks_indexed", 0),
            )
        )
        if job["status"] in {"completed", "failed", "cancelled"}:
            if job["status"] != "completed":
                raise SystemExit(f"job finished with status {job['status']}: {job.get('error_message')}")
            return
        time.sleep(3)


def smoke(api: str) -> None:
    with httpx.Client(timeout=60) as client:
        health = client.get(f"{api}/health")
        health.raise_for_status()
        ready = client.get(f"{api}/ready")
        ready.raise_for_status()
        with client.stream(
            "POST",
            f"{api}/api/v1/chat",
            json={"message": "Что такое Россия?", "mode": "normal", "stream": True},
        ) as response:
            response.raise_for_status()
            events = list(_iter_sse(response.iter_lines()))
        names = [event["event"] for event in events]
        required = {"run.started", "message.delta", "usage.updated"}
        if not required.issubset(set(names)):
            raise SystemExit(f"smoke failed, got events {names}")
    print(json.dumps({"health": health.json(), "ready": ready.json(), "events": names}, ensure_ascii=False))


def run_eval(api: str) -> None:
    questions = ["Что такое Россия?", "Что известно о Литве?"]
    report: dict[str, Any] = {"dataset": "wiki-mini", "cases": []}
    with httpx.Client(timeout=60) as client:
        for question in questions:
            response = client.post(f"{api}/api/v1/search:debug", json={"message": question})
            if response.status_code >= 500:
                raise SystemExit(f"eval failed for {question}: {response.text}")
            payload = response.json()
            report["cases"].append(
                {
                    "question": question,
                    "evidence_count": len(payload.get("evidence", [])),
                    "insufficient_evidence": payload.get("insufficient_evidence", True),
                }
            )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_smoke(count: int, api: str) -> None:
    from wikipediarag.eval.commands import eval_smoke

    report = asyncio.run(eval_smoke(count=count, api=api))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit("eval-smoke failed; generation was not started")


def run_eval_generate(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_generate
    from wikipediarag.eval.progress import EvalGenerateCliReporter

    family_weights = _parse_family_weight_specs(list(args.family_weight))
    manifest = asyncio.run(
        eval_generate(
            args.count,
            concurrency=args.concurrency,
            generator_alias=args.generator_alias,
            verifier_alias=args.verifier_alias,
            family_weights=family_weights,
            run_id=args.run_id,
            resume_run_id=args.resume_run_id,
            progress_callback=EvalGenerateCliReporter(),
        )
    )
    print(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2))


def run_eval_generate_status(run_id: str | None, latest: bool, json_mode: bool) -> None:
    from wikipediarag.eval.commands import eval_generate_status
    from wikipediarag.eval.progress import format_generate_status

    status = eval_generate_status(run_id=run_id, latest=latest)
    if json_mode:
        print(json.dumps(status.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    print(format_generate_status(status))


def run_eval_run(suite: str, api: str, batch_size: int) -> None:
    from wikipediarag.eval.commands import eval_run
    from wikipediarag.eval.runner import EvalRunCliReporter

    report = asyncio.run(eval_run(suite=suite, api=api, batch_size=batch_size, progress_callback=EvalRunCliReporter()))
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_report(latest: bool) -> None:
    from wikipediarag.eval.commands import eval_report_latest

    if not latest:
        raise SystemExit("eval-report currently requires --latest")
    report = eval_report_latest()
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_retrieval(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_retrieval_run
    from wikipediarag.eval.retrieval_runner import RetrievalEvalCliReporter

    report = asyncio.run(
        eval_retrieval_run(
            suite=args.suite,
            api=args.api,
            batch_size=args.batch_size,
            run_id=args.run_id,
            resume_run_id=args.resume_run_id,
            rerun_failed=args.rerun_failed,
            progress_callback=RetrievalEvalCliReporter(),
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_retrieval_status(run_id: str | None, latest: bool, json_mode: bool) -> None:
    from wikipediarag.eval.commands import eval_retrieval_status
    from wikipediarag.eval.retrieval_runner import format_retrieval_status

    status = eval_retrieval_status(run_id=run_id, latest=latest)
    if json_mode:
        print(json.dumps(status.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    print(format_retrieval_status(status))


def run_eval_retrieval_report(latest: bool) -> None:
    from wikipediarag.eval.commands import eval_retrieval_report_latest

    if not latest:
        raise SystemExit("eval-retrieval-report currently requires --latest")
    report = eval_retrieval_report_latest()
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_trusted_catalog() -> None:
    from wikipediarag.eval.commands import eval_trusted_catalog

    report = asyncio.run(eval_trusted_catalog())
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_trusted_generate(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_trusted_generate
    from wikipediarag.eval.trusted import TrustedGenerateCliReporter, parse_trusted_family_weight_specs

    family_weights = parse_trusted_family_weight_specs(list(args.family_weight))
    manifest = asyncio.run(
        eval_trusted_generate(
            count=args.count,
            concurrency=args.concurrency,
            rejection_budget=args.rejection_budget,
            generator_alias=args.generator_alias,
            verifier_alias=args.verifier_alias,
            family_weights=family_weights,
            run_id=args.run_id,
            resume_run_id=args.resume_run_id,
            takeover_stale_run=args.takeover_stale_run,
            progress_callback=TrustedGenerateCliReporter(),
        )
    )
    print(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2))


def run_eval_trusted_status(run_id: str | None, latest: bool, json_mode: bool) -> None:
    from wikipediarag.eval.commands import eval_trusted_status
    from wikipediarag.eval.trusted import format_trusted_status

    status = eval_trusted_status(run_id=run_id, latest=latest)
    if json_mode:
        print(json.dumps(status.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    print(format_trusted_status(status))


def run_eval_trusted_pool(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_trusted_pool
    from wikipediarag.eval.retrieval_runner import RetrievalEvalCliReporter

    report = asyncio.run(
        eval_trusted_pool(
            suite=args.suite,
            api=args.api,
            batch_size=args.batch_size,
            run_id=args.run_id,
            resume_run_id=args.resume_run_id,
            rerun_failed=args.rerun_failed,
            progress_callback=RetrievalEvalCliReporter(),
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_trusted_report(suite: str) -> None:
    from wikipediarag.eval.commands import eval_trusted_report

    report = eval_trusted_report(suite=suite)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_miracl_map(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_miracl_map

    report = asyncio.run(
        eval_miracl_map(
            input_path=Path(args.input) if args.input else None,
            from_huggingface=bool(args.from_huggingface),
            split=str(args.split),
            limit=int(args.limit),
            output_suite=str(args.output_suite),
            cache_dir=Path(args.cache_dir) if args.cache_dir else None,
            min_text_overlap=float(args.min_text_overlap),
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_review_candidates(input_path: str, output_suite: str) -> None:
    from wikipediarag.eval.commands import eval_review_candidates

    report = eval_review_candidates(input_path=Path(input_path), output_suite=output_suite)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_freeze_reviewed(suite: str, dev_count: int, test_count: int) -> None:
    from wikipediarag.eval.commands import eval_freeze_reviewed

    report = eval_freeze_reviewed(suite=suite, dev_count=dev_count, test_count=test_count)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_release_gate(suite: str, api: str) -> None:
    from wikipediarag.eval.commands import eval_release_gate
    from wikipediarag.eval.review import ReleaseGateCliReporter

    _require_api_ready(api)
    _require_release_gate_provider_smoke()
    report = asyncio.run(eval_release_gate(suite=suite, api=api, progress_callback=ReleaseGateCliReporter()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get("passed"):
        raise SystemExit("eval-release-gate failed")


def _require_release_gate_provider_smoke() -> None:
    from wikipediarag.config import get_settings
    from wikipediarag.eval.settings import adapt_eval_settings

    settings = adapt_eval_settings(get_settings())
    if settings.model_provider != "openrouter":
        return
    smoke_models(settings.model_gateway_url.rstrip("/"), "openrouter")


def run_eval_release_gate_status(suite: str, report_id: str | None, json_mode: bool) -> None:
    from wikipediarag.eval.commands import eval_release_gate_status
    from wikipediarag.eval.review import format_release_gate_status

    status = eval_release_gate_status(suite=suite, report_id=report_id)
    if json_mode:
        print(json.dumps(status.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    print(format_release_gate_status(status))


def run_eval_task_diagnostics(
    suite: str,
    split: str,
    config_id: str,
    task_ids: list[str],
    json_mode: bool,
) -> None:
    from wikipediarag.eval.commands import eval_task_diagnostics

    report = eval_task_diagnostics(suite=suite, split=split, task_ids=task_ids, config_id=config_id)
    if json_mode:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_reviewed_short(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_reviewed_short

    _require_api_ready(args.api)
    report = asyncio.run(
        eval_reviewed_short(
            suite=args.suite,
            split=args.split,
            task_ids=list(args.task_id),
            api=args.api,
            config_id=args.config_id,
            batch_size=args.batch_size,
            retrieval_batch_size=args.retrieval_batch_size,
            run_answer=not args.skip_answer,
            run_retrieval=not args.skip_retrieval,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_profile_retrieval(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_profile_retrieval

    _require_api_ready(args.api)
    report = asyncio.run(
        eval_profile_retrieval(
            suite=args.suite,
            split=args.split,
            api=args.api,
            config_id=args.config_id,
            task_ids=list(args.task_id) or None,
            limit=args.limit,
            warmup_iterations=args.warmup_iterations,
            measured_iterations=args.measured_iterations,
            batch_size=args.batch_size,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_full(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_full
    from wikipediarag.eval.progress import EvalGenerateCliReporter

    family_weights = _parse_family_weight_specs(list(args.family_weight))
    report = asyncio.run(
        eval_full(
            count=args.count,
            api=args.api,
            concurrency=args.concurrency,
            generator_alias=args.generator_alias,
            verifier_alias=args.verifier_alias,
            family_weights=family_weights,
            run_id=args.run_id,
            resume_run_id=args.resume_run_id,
            progress_callback=EvalGenerateCliReporter(),
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get("passed"):
        raise SystemExit("eval-full stopped before completion")


def smoke_models(gateway: str, provider: str) -> None:
    if provider == "openrouter":
        aliases = {
            "embed": "embed_default",
            "generator_fast": "generator_fast",
            "generator_main": "generator_main",
            "verifier": "verifier",
            "rerank": "rerank_default",
        }
        dimensions = 1024
    elif provider == "mock":
        aliases = {
            "embed": "mock_embed_default",
            "generator_fast": "mock_generator_fast",
            "generator_main": "mock_generator_main",
            "verifier": "mock_verifier",
            "rerank": "mock_rerank_default",
        }
        dimensions = 64
    else:
        raise SystemExit(f"unsupported provider for smoke-models: {provider}")
    with httpx.Client(timeout=120) as client:
        models = client.get(f"{gateway}/v1/models")
        models.raise_for_status()
        model_payload = models.json()
        required = set(aliases.values())
        available = {item["id"] for item in model_payload.get("data", [])}
        missing = sorted(required - available)
        if missing:
            raise SystemExit(f"missing model aliases: {missing}")
        healthy = {item["id"] for item in model_payload.get("data", []) if item.get("healthy")}
        unhealthy = sorted(required - healthy)
        if unhealthy:
            raise SystemExit(f"model gateway aliases are unhealthy: {unhealthy}")
        embedding = client.post(
            f"{gateway}/v1/embeddings",
            json={"model": aliases["embed"], "input": ["Россия - государство"], "dimensions": dimensions},
        )
        embedding.raise_for_status()
        vector = embedding.json()["data"][0]["embedding"]
        if len(vector) != dimensions:
            raise SystemExit(f"embedding dimension mismatch: {len(vector)}")
        typed = client.post(
            f"{gateway}/v1/chat/completions",
            json={
                "model": aliases["generator_fast"],
                "messages": [{"role": "user", "content": 'Верни JSON {"ok": true}'}],
                "response_format": {"type": "json_object"},
                "stream": False,
            },
        )
        typed.raise_for_status()
        content = typed.json()["choices"][0]["message"]["content"]
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"generator_fast did not return valid JSON: {content[:200]}") from exc
        rerank = client.post(
            f"{gateway}/v1/rerank",
            json={
                "model": aliases["rerank"],
                "query": "столица Франции",
                "documents": ["Париж - столица Франции.", "Берлин - столица Германии."],
                "top_n": 2,
            },
        )
        rerank.raise_for_status()
        results = rerank.json().get("results", [])
        if len(results) != 2 or results[0]["relevance_score"] < results[-1]["relevance_score"]:
            raise SystemExit("rerank endpoint did not return ordered results")
        print(
            json.dumps(
                {
                    "aliases": sorted(available),
                    "provider": provider,
                    "embedding_dimensions": len(vector),
                    "typed_json": json.loads(content),
                    "rerank_results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def demo_release_gate(api: str, job_id: str | None = None) -> None:
    zim_dir = Path(os.environ.get("ZIM_DIR", "zim"))
    zim_files = sorted(zim_dir.glob("*.zim"))
    if not zim_files:
        raise SystemExit(f"demo release gate requires a real {zim_dir}/*.zim archive")
    kiwix_base_url = os.environ.get("KIWIX_PUBLIC_BASE_URL", "http://localhost:8083").rstrip("/")
    kiwix_probe_url = os.environ.get("KIWIX_INTERNAL_BASE_URL", kiwix_base_url).rstrip("/")
    gateway_url = os.environ.get("MODEL_GATEWAY_URL", "http://model-gateway:8080").rstrip("/")
    with httpx.Client(timeout=120) as client:
        ready = _require_api_ready(api, client=client)
        models = client.get(f"{gateway_url}/v1/models")
        models.raise_for_status()
        required_aliases = {"embed_default", "generator_fast", "generator_main", "verifier", "rerank_default"}
        healthy_aliases = {item["id"] for item in models.json().get("data", []) if item.get("healthy")}
        missing_aliases = sorted(required_aliases - healthy_aliases)
        if missing_aliases:
            raise SystemExit(f"model gateway aliases are unavailable: {missing_aliases}")
        kiwix = client.get(kiwix_probe_url)
        if kiwix.status_code >= 400:
            raise SystemExit(f"Kiwix is not reachable at {kiwix_probe_url}: HTTP {kiwix.status_code}")
        if job_id is None:
            job_response = client.post(f"{api}/api/v1/wikipedia/zim-imports", json={"limit": 10000})
            if job_response.status_code >= 400:
                raise SystemExit(f"ZIM import could not be created: {job_response.text}")
            job_id = str(job_response.json()["job_id"])
        while True:
            job = client.get(f"{api}/api/v1/ingestion-jobs/{job_id}")
            job.raise_for_status()
            payload = job.json()
            if payload["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(5)
        progress = payload.get("progress", {})
        if payload["status"] != "completed" or int(progress.get("pages_imported") or 0) != 10000:
            raise SystemExit(f"ZIM demo import did not complete exactly 10000 pages: {payload}")
        print(
            json.dumps(
                {
                    "ready": ready.json(),
                    "model_gateway": {"url": gateway_url, "aliases": sorted(required_aliases)},
                    "kiwix": {
                        "public_url": kiwix_base_url,
                        "probe_url": kiwix_probe_url,
                        "status": kiwix.status_code,
                    },
                    "zim_job": payload,
                    "zim_file": str(zim_files[0]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def verify_document_upload(args: argparse.Namespace) -> None:
    api = str(args.api).rstrip("/")
    xberg = str(args.xberg).rstrip("/")
    docling = str(args.docling).rstrip("/")
    metadata_service = str(args.metadata_service).rstrip("/")
    report_dir = Path("artifacts/validation/document-upload") / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "passed": False,
        "api": api,
        "xberg": xberg,
        "docling": docling,
        "metadata_service": metadata_service,
        "report_dir": str(report_dir),
        "checks": [],
        "uploads": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    exit_code = 0
    try:
        if not args.skip_compose:
            _compose_up_document_upload_stack()
            _record_check(report, "compose_up", True)
        with httpx.Client(timeout=180) as client:
            _wait_json_ready(client, f"{metadata_service}/health", "metadata-service")
            _record_check(report, "metadata_service_health", True)
            _smoke_metadata_service(client, metadata_service)
            _record_check(report, "metadata_service_extract", True)
            _wait_json_ready(client, f"{xberg}/health", "xberg")
            _record_check(report, "xberg_health", True)
            _smoke_xberg(client, xberg)
            _record_check(report, "xberg_extract", True)
            _wait_json_ready(client, f"{docling}/health", "docling")
            _record_check(report, "docling_health", True)
            _smoke_docling(client, docling)
            _record_check(report, "docling_convert", True)
            _wait_json_ready(client, f"{api}/ready", "api", require_ok=True)
            _record_check(report, "api_ready", True)
            _authenticate_smoke_session(
                client,
                api,
                username=str(args.admin_username),
                admin_secret=_resolve_hardening_admin_secret(args),
            )
            _record_check(report, "platform_admin_login", True)
            kb_id = _create_verify_knowledge_base(client, api)
            report["knowledge_base_id"] = kb_id
            for fixture in _document_upload_fixtures():
                upload_result = _upload_verify_fixture(client, api, kb_id, fixture)
                job_payload = _wait_job_terminal(client, api, str(upload_result["job_id"]))
                if job_payload.get("status") != "completed":
                    raise RuntimeError(f"upload job did not complete: {job_payload}")
                document = _get_json(client, f"{api}/api/v1/documents/{upload_result['document_id']}")
                versions = _get_json(client, f"{api}/api/v1/documents/{upload_result['document_id']}/versions")
                _assert_public_payload_is_safe(document)
                _assert_public_payload_is_safe(versions)
                public_metadata = document.get("public_metadata") if isinstance(document, dict) else {}
                if not isinstance(public_metadata, dict) or not public_metadata.get("detected_language"):
                    raise RuntimeError(f"document metadata is missing detected language: {document}")
                report["uploads"].append(
                    {
                        "filename": fixture["filename"],
                        "document_id": upload_result["document_id"],
                        "document_version_id": upload_result["document_version_id"],
                        "job": job_payload,
                        "document": document,
                        "versions_count": len(versions.get("versions", [])) if isinstance(versions, dict) else 0,
                    }
                )
            _verify_uploaded_retrieval(client, api, kb_id)
            _record_check(report, "retrieval_published_chunks", True)
        report["passed"] = True
    except Exception as exc:
        exit_code = 1
        report["error"] = {"code": type(exc).__name__, "message": str(exc)[:1000]}
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_document_upload_reports(report_dir, report)
        if args.down_after:
            subprocess.run([_docker_executable(), "compose", "down"], check=False)  # noqa: S603
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def verify_document_corpus(args: argparse.Namespace) -> None:
    api = str(args.api).rstrip("/")
    xberg = str(args.xberg).rstrip("/")
    docling = str(args.docling).rstrip("/")
    metadata_service = str(args.metadata_service).rstrip("/")
    report_dir = Path("artifacts/validation/document-corpus") / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(str(args.cache_dir))
    manifest_path = Path(str(args.manifest))
    report: dict[str, Any] = {
        "passed": False,
        "api": api,
        "xberg": xberg,
        "docling": docling,
        "metadata_service": metadata_service,
        "fixture_set": args.fixture_set,
        "include_external": bool(args.include_external),
        "manifest": str(manifest_path),
        "cache_dir": str(cache_dir),
        "report_dir": str(report_dir),
        "checks": [],
        "items": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    exit_code = 0
    try:
        items = synthetic_document_corpus(
            fixture_set=str(args.fixture_set),
            include_negative=not bool(args.skip_negative),
        )
        with httpx.Client(timeout=180, follow_redirects=True) as client:
            if args.include_external:
                external_items = load_manifest_corpus(
                    manifest_path,
                    include_disabled=bool(args.include_disabled_external),
                )
                items.extend(_materialize_external_corpus(client, external_items, cache_dir))
            if args.max_documents is not None:
                items = items[: max(0, int(args.max_documents))]
            report["corpus_summary"] = corpus_summary(items)
            if not args.skip_compose:
                _compose_up_document_upload_stack()
                _record_check(report, "compose_up", True)
            _wait_json_ready(client, f"{metadata_service}/health", "metadata-service")
            _record_check(report, "metadata_service_health", True)
            _smoke_metadata_service(client, metadata_service)
            _record_check(report, "metadata_service_extract", True)
            _wait_json_ready(client, f"{xberg}/health", "xberg")
            _record_check(report, "xberg_health", True)
            _smoke_xberg(client, xberg)
            _record_check(report, "xberg_extract", True)
            _wait_json_ready(client, f"{docling}/health", "docling")
            _record_check(report, "docling_health", True)
            _smoke_docling(client, docling)
            _record_check(report, "docling_convert", True)
            _wait_json_ready(client, f"{api}/ready", "api", require_ok=True)
            _record_check(report, "api_ready", True)
            _verify_corpus_api_controls(client, api)
            _record_check(report, "upload_api_negative_controls", True)
            kb_id = _create_verify_knowledge_base(client, api)
            report["knowledge_base_id"] = kb_id
            for index, item in enumerate(items, start=1):
                result = _run_corpus_item(client, api, kb_id, item)
                _append_report_item(report, result)
                print(
                    json.dumps(
                        {
                            "corpus_item": item.id,
                            "processed": index,
                            "total": len(items),
                            "passed": result.get("passed"),
                            "outcome": result.get("outcome"),
                            "job_status": result.get("job_status"),
                        },
                        ensure_ascii=False,
                    )
                )
            failures = [item for item in report["items"] if isinstance(item, dict) and not item.get("passed")]
            if failures:
                raise RuntimeError(f"document corpus verification failed for {len(failures)} item(s)")
        report["passed"] = True
    except Exception as exc:
        exit_code = 1
        report["error"] = {"code": type(exc).__name__, "message": str(exc)[:1000]}
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_document_corpus_reports(report_dir, report)
        if args.down_after:
            subprocess.run([_docker_executable(), "compose", "down"], check=False)  # noqa: S603
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def verify_cross_tenant_hardening(args: argparse.Namespace) -> None:
    api = str(args.api).rstrip("/")
    admin_secret = _resolve_hardening_admin_secret(args)
    report_dir = Path("artifacts/validation/cross-tenant-hardening") / time.strftime(
        "%Y%m%dT%H%M%SZ",
        time.gmtime(),
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "passed": False,
        "api": api,
        "report_dir": str(report_dir),
        "checks": [],
        "negative_probes": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    exit_code = 0
    try:
        if not admin_secret:
            raise RuntimeError(
                "verify-cross-tenant-hardening resolved an empty admin secret; configure --admin-secret-file, "
                "WIKIPEDIARAG_ADMIN_PASSWORD_FILE, --admin-secret or WIKIPEDIARAG_ADMIN_PASSWORD"
            )
        if not args.skip_compose:
            _compose_up_cross_tenant_hardening_stack()
            _record_check(report, "compose_up", True)
        with httpx.Client(timeout=180, follow_redirects=True) as client:
            _wait_json_ready(client, f"{api}/ready", "api", require_ok=True)
            _record_check(report, "api_ready", True)
            _login_for_hardening(client, api, str(args.admin_username), admin_secret)
            _record_check(report, "platform_admin_login", True)

            suffix = time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
            tenant_a = _create_hardening_tenant(client, api, suffix, "a")
            tenant_b = _create_hardening_tenant(client, api, suffix, "b")
            report["tenant_a_id"] = tenant_a
            report["tenant_b_id"] = tenant_b
            _record_check(report, "two_real_tenants_created", True)

            _select_hardening_tenant(client, api, tenant_a)
            kb_a = _create_verify_knowledge_base(client, api)
            report["tenant_a_kb_id"] = kb_a
            upload_result = _upload_hardening_fixture(client, api, kb_a, tenant_a)
            _assert_presigned_url_uses_tenant_kb_path(upload_result["upload_session"], tenant_a, kb_a)
            job_payload = _wait_job_terminal(client, api, str(upload_result["job_id"]))
            if job_payload.get("status") != "completed":
                raise RuntimeError(f"tenant A upload job did not complete: {job_payload}")
            document = _get_json(client, f"{api}/api/v1/documents/{upload_result['document_id']}")
            versions = _get_json(client, f"{api}/api/v1/documents/{upload_result['document_id']}/versions")
            _assert_public_payload_is_safe(document)
            _assert_public_payload_is_safe(versions)
            _verify_uploaded_retrieval(client, api, kb_a)
            query_run_id = _run_hardening_chat(client, api, kb_a)
            _record_check(report, "tenant_a_upload_chat_retrieval", True)

            _select_hardening_tenant(client, api, tenant_b)
            kb_b = _create_verify_knowledge_base(client, api)
            report["tenant_b_kb_id"] = kb_b
            _record_check(report, "tenant_b_context_selected", True)

            probes = [
                _negative_probe(client, "GET", f"{api}/api/v1/knowledge-bases/{kb_a}", "tenant_b_get_tenant_a_kb"),
                _negative_probe(
                    client,
                    "POST",
                    f"{api}/api/v1/uploads/sessions",
                    "tenant_b_create_upload_in_tenant_a_kb",
                    json_payload=_hardening_upload_session_payload(kb_a),
                ),
                _negative_probe(
                    client,
                    "POST",
                    f"{api}/api/v1/uploads/sessions/{upload_result['upload_session_id']}:complete",
                    "tenant_b_complete_tenant_a_upload_session",
                    json_payload={"metadata": {"tenant_id": tenant_b, "object_key": "uploads/evil"}},
                ),
                _negative_probe(
                    client,
                    "GET",
                    f"{api}/api/v1/ingestion-jobs/{upload_result['job_id']}",
                    "tenant_b_get_tenant_a_job",
                ),
                _negative_probe(
                    client,
                    "POST",
                    f"{api}/api/v1/ingestion-jobs/{upload_result['job_id']}:cancel",
                    "tenant_b_cancel_tenant_a_job",
                ),
                _negative_probe(
                    client,
                    "POST",
                    f"{api}/api/v1/ingestion-jobs/{upload_result['job_id']}:resume",
                    "tenant_b_resume_tenant_a_job",
                ),
                _negative_probe(
                    client,
                    "GET",
                    f"{api}/api/v1/documents/{upload_result['document_id']}",
                    "tenant_b_get_tenant_a_document",
                ),
                _negative_probe(
                    client,
                    "GET",
                    f"{api}/api/v1/documents/{upload_result['document_id']}/versions",
                    "tenant_b_get_tenant_a_document_versions",
                ),
                _negative_probe(
                    client,
                    "POST",
                    f"{api}/api/v1/documents/{upload_result['document_id']}:reprocess",
                    "tenant_b_reprocess_tenant_a_document",
                ),
                _negative_probe(
                    client,
                    "GET",
                    f"{api}/api/v1/query-runs/{query_run_id}/retrieval",
                    "tenant_b_get_tenant_a_query_run_retrieval",
                ),
                _negative_probe(
                    client,
                    "POST",
                    f"{api}/api/v1/search:debug",
                    "tenant_b_debug_search_tenant_a_kb",
                    json_payload={
                        "message": "hardening tenant isolation marker",
                        "knowledge_base_ids": [kb_a],
                        "retrieval_profile": "upload_sota_mvp",
                        "tenant_id": tenant_a,
                        "filters": {"tenant_id": tenant_a},
                    },
                    accepted_statuses={403, 404, 409},
                ),
                _negative_probe(
                    client,
                    "POST",
                    f"{api}/api/v1/chat",
                    "tenant_b_chat_tenant_a_kb",
                    json_payload={
                        "message": "hardening tenant isolation marker",
                        "knowledge_base_ids": [kb_a],
                        "retrieval_profile": "upload_sota_mvp",
                        "tenant_id": tenant_a,
                        "stream": True,
                    },
                    accepted_statuses={403, 404, 409},
                ),
            ]
            for probe in probes:
                _append_negative_probe(report, probe)
            failures = [probe for probe in probes if not bool(probe.get("passed"))]
            if failures:
                raise RuntimeError(f"cross-tenant hardening failed for {len(failures)} probe(s)")
        report["passed"] = True
    except Exception as exc:
        exit_code = 1
        report["error"] = {"code": type(exc).__name__, "message": str(exc)[:1000]}
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_cross_tenant_hardening_reports(report_dir, report)
        if args.down_after:
            subprocess.run([_docker_executable(), "compose", "down"], check=False)  # noqa: S603
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def _resolve_hardening_admin_secret(args: argparse.Namespace) -> str:
    raw_path = getattr(args, "admin_secret_file", None)
    if raw_path:
        return Path(str(raw_path)).read_text(encoding="utf-8").strip()
    return str(getattr(args, "admin_secret", "admin") or "admin")


def _login_for_hardening(client: httpx.Client, api: str, username: str, admin_secret: str) -> None:
    response = client.post(
        f"{api}/api/v1/auth/local/login",
        json={"username": username, "password": admin_secret, "remember_me": False},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    user = payload.get("user") if isinstance(payload, dict) else None
    if not isinstance(user, dict) or user.get("platform_role") != "PLATFORM_ADMIN":
        raise RuntimeError("hardening smoke requires a PLATFORM_ADMIN local/hybrid login")
    if user.get("password_change_required"):
        raise RuntimeError("hardening smoke requires password_change_required=false for the admin user")
    session = _get_json(client, f"{api}/api/v1/auth/session")
    csrf_token = session.get("csrf_token")
    if not isinstance(csrf_token, str) or not csrf_token:
        raise RuntimeError(f"authenticated session did not return a CSRF token: {session}")
    client.headers.update({"X-CSRF-Token": csrf_token})


def _authenticate_smoke_session(client: httpx.Client, api: str, *, username: str, admin_secret: str) -> None:
    session_response = client.get(f"{api}/api/v1/auth/session", timeout=30)
    session_response.raise_for_status()
    session = session_response.json()
    if isinstance(session, dict) and session.get("authenticated"):
        csrf_token = session.get("csrf_token")
        if isinstance(csrf_token, str) and csrf_token:
            client.headers.update({"X-CSRF-Token": csrf_token})
        return
    _login_for_hardening(client, api, username, admin_secret)


def _create_hardening_tenant(client: httpx.Client, api: str, suffix: str, label: str) -> str:
    response = client.post(
        f"{api}/api/v1/admin/tenants",
        json={"slug": f"hardening-{label}-{suffix}", "name": f"Hardening Tenant {label.upper()} {suffix}"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    tenant_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(tenant_id, str) or not tenant_id:
        raise RuntimeError(f"tenant creation returned an invalid payload: {payload}")
    return tenant_id


def _select_hardening_tenant(client: httpx.Client, api: str, tenant_id: str) -> None:
    response = client.post(f"{api}/api/v1/auth/session/tenant", json={"tenant_id": tenant_id}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("active_tenant_id") != tenant_id:
        raise RuntimeError(f"tenant selection did not activate {tenant_id}: {payload}")
    session = _get_json(client, f"{api}/api/v1/auth/session")
    csrf_token = session.get("csrf_token")
    if not isinstance(csrf_token, str) or not csrf_token:
        raise RuntimeError(f"tenant-selected session did not return a CSRF token: {session}")
    client.headers.update({"X-CSRF-Token": csrf_token})


def _hardening_upload_session_payload(knowledge_base_id: str) -> dict[str, Any]:
    content = b"hardening tenant isolation marker 2026-07-30"
    return {
        "filename": "hardening-isolation.txt",
        "content_type": "text/plain",
        "size_bytes": len(content),
        "checksum_sha256": hashlib.sha256(content).hexdigest(),
        "knowledge_base_id": knowledge_base_id,
        "parser_profile": "standard",
        "metadata": {
            "tenant_id": "client-supplied-tenant-must-be-ignored",
            "user_id": "client-supplied-user-must-be-ignored",
            "object_key": "uploads/client/supplied/key",
        },
    }


def _upload_hardening_fixture(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    tenant_id: str,
) -> dict[str, Any]:
    content = b"hardening tenant isolation marker 2026-07-30"
    session_response = client.post(
        f"{api}/api/v1/uploads/sessions",
        json=_hardening_upload_session_payload(knowledge_base_id),
        timeout=30,
    )
    session_response.raise_for_status()
    session = session_response.json()
    upload_response = httpx.put(
        session["upload_url"],
        content=content,
        headers=session.get("required_headers") or {},
        timeout=120,
    )
    upload_response.raise_for_status()
    complete_response = client.post(
        f"{api}/api/v1/uploads/sessions/{session['upload_session_id']}:complete",
        json={"metadata": {"tenant_id": tenant_id, "object_key": "uploads/client/supplied/key"}},
        timeout=30,
    )
    complete_response.raise_for_status()
    payload = complete_response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("upload complete returned a non-object JSON payload")
    return {
        **payload,
        "upload_session_id": session["upload_session_id"],
        "upload_session": session,
    }


def _run_hardening_chat(client: httpx.Client, api: str, knowledge_base_id: str) -> str:
    with client.stream(
        "POST",
        f"{api}/api/v1/chat",
        json={
            "message": "hardening tenant isolation marker",
            "knowledge_base_ids": [knowledge_base_id],
            "retrieval_profile": "upload_sota_mvp",
            "stream": True,
            "tenant_id": "client-supplied-tenant-must-be-ignored",
        },
        timeout=120,
    ) as response:
        response.raise_for_status()
        events = list(_iter_sse(response.iter_lines()))
    names = [event["event"] for event in events]
    if "run.completed" not in names:
        raise RuntimeError(f"hardening chat did not complete: {names}")
    for event in events:
        query_run_id = event.get("data", {}).get("query_run_id") or event.get("query_run_id")
        if isinstance(query_run_id, str) and query_run_id:
            return query_run_id
    raise RuntimeError(f"hardening chat did not expose query_run_id: {events}")


def _assert_presigned_url_uses_tenant_kb_path(upload_session: Any, tenant_id: str, knowledge_base_id: str) -> None:
    if not isinstance(upload_session, dict):
        raise RuntimeError("upload session payload is not an object")
    serialized = json.dumps(upload_session, ensure_ascii=False)
    expected = f"uploads/{tenant_id}/{knowledge_base_id}/"
    if expected not in serialized:
        raise RuntimeError("presigned upload URL does not include the server-owned tenant and KB path")
    unexpected = "uploads/client/supplied/key"
    if unexpected in serialized:
        raise RuntimeError("presigned upload URL used a client-supplied object key")


def _negative_probe(
    client: httpx.Client,
    method: str,
    url: str,
    name: str,
    *,
    json_payload: dict[str, Any] | None = None,
    accepted_statuses: set[int] | None = None,
) -> dict[str, Any]:
    accepted = accepted_statuses or {403, 404}
    response = client.request(method, url, json=json_payload, timeout=60)
    body: dict[str, Any] = {}
    try:
        payload = response.json()
        body = payload if isinstance(payload, dict) else {}
    except ValueError:
        body = {}
    safe_payload = _safe_probe_payload(body)
    passed = response.status_code in accepted and not _payload_has_cross_tenant_leak(safe_payload)
    return {
        "name": name,
        "method": method,
        "status_code": response.status_code,
        "accepted_statuses": sorted(accepted),
        "passed": passed,
        "error_code": _extract_error_code(body),
        "safe_payload": safe_payload,
    }


def _payload_has_cross_tenant_leak(payload: Any) -> bool:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    unsafe_tokens = (
        "object_key",
        "original_artifact_key",
        "normalized_artifact_key",
        "server_side_tokens",
        "access_token",
        "refresh_token",
        "hardening tenant isolation marker 2026-07-30",
        "uploads/client/supplied/key",
    )
    return any(token in serialized for token in unsafe_tokens)


def _safe_probe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    if isinstance(error, dict):
        return {
            "error": {
                "code": error.get("code"),
                "message": error.get("message"),
                "details": error.get("details") if isinstance(error.get("details"), dict) else {},
            }
        }
    if "detail" in payload:
        return {"detail": str(payload.get("detail"))[:300]}
    return {}


def _extract_error_code(payload: dict[str, Any]) -> str | None:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else None
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return str(error["code"])
    detail = payload.get("detail")
    return str(detail)[:80] if detail is not None else None


def _append_negative_probe(report: dict[str, Any], item: dict[str, Any]) -> None:
    probes = report.setdefault("negative_probes", [])
    if isinstance(probes, list):
        probes.append(item)


def _write_cross_tenant_hardening_reports(report_dir: Path, report: dict[str, Any]) -> None:
    report_path = report_dir / "cross-tenant-hardening-report.json"
    junit_path = report_dir / "cross-tenant-hardening-junit.xml"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raw_probes = report.get("negative_probes")
    probes: list[Any] = raw_probes if isinstance(raw_probes, list) else []
    failures = [probe for probe in probes if isinstance(probe, dict) and not probe.get("passed")]
    if not failures and report.get("error"):
        failures = [{"name": "verify_cross_tenant_hardening", "error": report["error"]}]
    testcases: list[str] = []
    for probe in probes:
        if not isinstance(probe, dict):
            continue
        failure_xml = ""
        if not probe.get("passed"):
            failure_xml = (
                f'<failure type="{escape(str(probe.get("error_code") or "CrossTenantProbeFailed"))}">'
                f"{escape(json.dumps(probe, ensure_ascii=False))}</failure>"
            )
        testcases.append(
            f'  <testcase classname="wikipediarag.cli" name="{escape(str(probe.get("name") or "probe"))}">'
            f"{failure_xml}</testcase>\n"
        )
    if not testcases and report.get("error"):
        error = report["error"] if isinstance(report["error"], dict) else {}
        testcases.append(
            '  <testcase classname="wikipediarag.cli" name="verify_cross_tenant_hardening">'
            f'<failure type="{escape(str(error.get("code") or "Error"))}">'
            f"{escape(str(error.get('message') or ''))}</failure></testcase>\n"
        )
    junit = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="verify-cross-tenant-hardening" tests="{len(testcases)}" failures="{len(failures)}">\n'
        f"{''.join(testcases)}</testsuite>\n"
    )
    junit_path.write_text(junit, encoding="utf-8")


def _compose_up_cross_tenant_hardening_stack() -> None:
    env = {
        **os.environ,
        "AUTH_MODE": "local",
        "SESSION_COOKIE_SECURE": "false",
        "MODEL_PROVIDER": "mock",
        "RETRIEVAL_PROFILE": "test_mock",
        "DOCUMENT_PARSER_SERVICES_REQUIRED": "true",
        "MINIO_PUBLIC_ENDPOINT": os.environ.get("MINIO_PUBLIC_ENDPOINT", "http://localhost:9000"),
        "API_PUBLIC_BASE_URL": os.environ.get("API_PUBLIC_BASE_URL", "http://localhost:8000"),
        "XBERG_URL": os.environ.get("XBERG_URL", "http://xberg:8000"),
        "DOCLING_URL": os.environ.get("DOCLING_URL", "http://docling:5001"),
        "METADATA_SERVICE_URL": os.environ.get("METADATA_SERVICE_URL", "http://metadata-service:8090"),
    }
    services = [
        "postgres",
        "redis",
        "minio",
        "opensearch",
        "mock-provider",
        "model-gateway",
        "metadata-service",
        "xberg",
        "docling",
        "api",
        "worker",
    ]
    subprocess.run(  # noqa: S603
        [_docker_executable(), "compose", "up", "-d", "--build", "--force-recreate", *services],
        check=True,
        env=env,
    )


def _materialize_external_corpus(
    client: httpx.Client,
    items: list[DocumentCorpusItem],
    cache_dir: Path,
) -> list[DocumentCorpusItem]:
    materialized: list[DocumentCorpusItem] = []
    headers = {"User-Agent": "WikipediaRag document corpus verification contact local@example.invalid"}
    for item in items:
        if not item.url:
            raise RuntimeError(f"external corpus item {item.id} is missing url")
        item_dir = cache_dir / item.source_id
        item_dir.mkdir(parents=True, exist_ok=True)
        local_path = item_dir / f"{item.id}-{item.filename}"
        if local_path.exists():
            data = local_path.read_bytes()
            materialized.append(materialize_corpus_item(item, data=data))
            continue
        response = client.get(item.url, headers=headers, timeout=180)
        response.raise_for_status()
        data = response.content
        materialized_item = materialize_corpus_item(item, data=data)
        local_path.write_bytes(data)
        materialized.append(materialized_item)
    return materialized


def _verify_corpus_api_controls(client: httpx.Client, api: str) -> None:
    checksum = hashlib.sha256(b"control").hexdigest()
    wrong_kb = client.post(
        f"{api}/api/v1/uploads/sessions",
        json={
            "filename": "wrong-kb.txt",
            "content_type": "text/plain",
            "size_bytes": 7,
            "checksum_sha256": checksum,
            "knowledge_base_id": "00000000-0000-4000-8000-000000000000",
        },
        timeout=30,
    )
    if wrong_kb.status_code != 404:
        raise RuntimeError(f"wrong KB upload session was not rejected: {wrong_kb.status_code} {wrong_kb.text[:300]}")
    traversal = client.post(
        f"{api}/api/v1/uploads/sessions",
        json={
            "filename": "../secret.txt",
            "content_type": "text/plain",
            "size_bytes": 7,
            "checksum_sha256": checksum,
        },
        timeout=30,
    )
    if traversal.status_code < 400:
        raise RuntimeError("path traversal upload filename was not rejected")


def _run_corpus_item(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    item: DocumentCorpusItem,
) -> dict[str, Any]:
    result: dict[str, Any] = {**item.report_metadata(), "passed": False}
    try:
        if item.content is None:
            raise RuntimeError(f"corpus item {item.id} has no materialized content")
        upload_result = _upload_corpus_item(client, api, knowledge_base_id, item)
        result.update(upload_result)
        _assert_corpus_item_outcome(client, api, knowledge_base_id, item, result)
        result["passed"] = True
    except Exception as exc:
        result["error"] = {"code": type(exc).__name__, "message": str(exc)[:1000]}
    return result


def _upload_corpus_item(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    item: DocumentCorpusItem,
) -> dict[str, Any]:
    if item.content is None:
        raise RuntimeError(f"corpus item {item.id} is not materialized")
    content = item.content
    metadata = item.metadata or {}
    actual_checksum = hashlib.sha256(content).hexdigest()
    declared_checksum = str(metadata.get("declared_sha256") or actual_checksum)
    declared_size = int(metadata.get("declared_size_bytes") or len(content))
    session_response = client.post(
        f"{api}/api/v1/uploads/sessions",
        json={
            "filename": item.filename,
            "content_type": item.content_type,
            "size_bytes": declared_size,
            "checksum_sha256": declared_checksum,
            "knowledge_base_id": knowledge_base_id,
            "parser_profile": item.parser_profile,
            "metadata": {
                "verify_document_corpus": True,
                "corpus_item_id": item.id,
                "source_id": item.source_id,
                "license": item.license,
            },
        },
        timeout=30,
    )
    if item.expected_outcome == "session_rejected":
        return {
            "outcome": "session_rejected",
            "session_status_code": session_response.status_code,
            "session_rejected": session_response.status_code >= 400,
            "safe_error": session_response.text[:300],
        }
    session_response.raise_for_status()
    session = session_response.json()
    upload_response = client.put(
        session["upload_url"],
        content=content,
        headers=session.get("required_headers") or {},
        timeout=120,
    )
    upload_response.raise_for_status()
    complete_response = client.post(
        f"{api}/api/v1/uploads/sessions/{session['upload_session_id']}:complete",
        json={"metadata": {"verify_document_corpus_completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}},
        timeout=30,
    )
    if item.expected_outcome == "complete_rejected":
        return {
            "outcome": "complete_rejected",
            "upload_session_id": session["upload_session_id"],
            "complete_status_code": complete_response.status_code,
            "complete_rejected": complete_response.status_code >= 400,
            "safe_error": complete_response.text[:300],
        }
    complete_response.raise_for_status()
    completed = complete_response.json()
    job_payload = _wait_job_terminal(client, api, str(completed["job_id"]))
    result = {
        "outcome": "job_terminal",
        "upload_session_id": session["upload_session_id"],
        "document_id": completed["document_id"],
        "document_version_id": completed["document_version_id"],
        "job_id": completed["job_id"],
        "job_status": job_payload.get("status"),
        "job": job_payload,
    }
    if completed.get("document_id"):
        document = _get_json(client, f"{api}/api/v1/documents/{completed['document_id']}")
        versions = _get_json(client, f"{api}/api/v1/documents/{completed['document_id']}/versions")
        result["document"] = document
        result["versions_count"] = len(versions.get("versions", [])) if isinstance(versions, dict) else 0
        _assert_public_payload_is_safe(document)
        _assert_public_payload_is_safe(versions)
    return result


def _assert_corpus_item_outcome(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    item: DocumentCorpusItem,
    result: dict[str, Any],
) -> None:
    if item.expected_outcome == "session_rejected":
        if not result.get("session_rejected"):
            raise RuntimeError(f"expected session rejection for {item.id}")
        return
    if item.expected_outcome == "complete_rejected":
        if not result.get("complete_rejected"):
            raise RuntimeError(f"expected upload completion rejection for {item.id}")
        _assert_expected_error_text(item, str(result.get("safe_error") or ""))
        return
    job = result.get("job")
    if not isinstance(job, dict):
        raise RuntimeError(f"missing job payload for {item.id}")
    if item.expected_outcome == "failed":
        if job.get("status") != "failed":
            raise RuntimeError(f"expected failed job for {item.id}: {job}")
        progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
        safe_error_code = progress.get("safe_error_code") if isinstance(progress, dict) else None
        if item.expected_error_code and safe_error_code != item.expected_error_code:
            raise RuntimeError(f"expected error {item.expected_error_code} for {item.id}, got {safe_error_code}: {job}")
        return
    if job.get("status") != "completed":
        raise RuntimeError(f"expected completed job for {item.id}: {job}")
    document = result.get("document")
    if not isinstance(document, dict):
        raise RuntimeError(f"missing document metadata for {item.id}")
    if document.get("status") != "published":
        raise RuntimeError(f"document was not published for {item.id}: {document}")
    raw_public_metadata = document.get("public_metadata")
    public_metadata = raw_public_metadata if isinstance(raw_public_metadata, dict) else {}
    if item.expected_language and public_metadata.get("detected_language") != item.expected_language:
        raise RuntimeError(
            f"expected language {item.expected_language} for {item.id}, got {public_metadata.get('detected_language')}"
        )
    if item.expected_document_date and public_metadata.get("document_date") != item.expected_document_date:
        raise RuntimeError(
            f"expected date {item.expected_document_date} for {item.id}, got {public_metadata.get('document_date')}"
        )
    if item.expected_parser_route and public_metadata.get("parser_route") != item.expected_parser_route:
        raise RuntimeError(
            f"expected parser route {item.expected_parser_route} for {item.id}, "
            f"got {public_metadata.get('parser_route')}"
        )
    if item.content is not None and document.get("content_hash") != hashlib.sha256(item.content).hexdigest():
        raise RuntimeError(f"document content hash mismatch for {item.id}")
    _assert_timestamp_order(document, ("uploaded_at", "upload_completed_at", "ingested_at", "published_at"))
    if item.retrieval_query:
        _verify_corpus_retrieval(client, api, knowledge_base_id, item, str(result.get("document_version_id")))


def _assert_expected_error_text(item: DocumentCorpusItem, value: str) -> None:
    if item.expected_error_code and item.expected_error_code not in value:
        raise RuntimeError(f"expected error text {item.expected_error_code} for {item.id}, got {value[:300]}")


def _assert_timestamp_order(payload: dict[str, Any], fields: tuple[str, ...]) -> None:
    values = [str(payload[field]) for field in fields if payload.get(field)]
    if values != sorted(values):
        raise RuntimeError(f"document timestamps are not monotonic: {values}")


def _verify_corpus_retrieval(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    item: DocumentCorpusItem,
    document_version_id: str,
) -> None:
    response = client.post(
        f"{api}/api/v1/search:debug",
        json={
            "message": item.retrieval_query,
            "top_k": 5,
            "knowledge_base_ids": [knowledge_base_id],
            "retrieval_profile": "upload_sota_mvp",
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    evidence = payload.get("evidence") if isinstance(payload, dict) else None
    if not isinstance(evidence, list) or not evidence:
        raise RuntimeError(f"corpus item {item.id} is not retrievable: {payload}")
    matching = []
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        metadata = entry.get("metadata")
        if isinstance(metadata, dict) and metadata.get("document_version_id") == document_version_id:
            matching.append(entry)
    if not matching:
        raise RuntimeError(
            f"corpus item {item.id} retrieval returned no evidence for version {document_version_id}: {payload}"
        )
    metadata = matching[0].get("metadata") if isinstance(matching[0].get("metadata"), dict) else {}
    if not isinstance(metadata, dict) or not metadata.get("locator"):
        raise RuntimeError(f"corpus item {item.id} evidence is missing locator metadata: {matching[0]}")
    if metadata.get("publication_status") != "published":
        raise RuntimeError(f"corpus item {item.id} retrieval returned non-published evidence: {matching[0]}")


def _append_report_item(report: dict[str, Any], item: dict[str, Any]) -> None:
    items = report.setdefault("items", [])
    if isinstance(items, list):
        items.append(item)


def _write_document_corpus_reports(report_dir: Path, report: dict[str, Any]) -> None:
    report_path = report_dir / "document-corpus-report.json"
    junit_path = report_dir / "document-corpus-junit.xml"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raw_items = report.get("items")
    items: list[Any] = raw_items if isinstance(raw_items, list) else []
    failures = [item for item in items if isinstance(item, dict) and not item.get("passed")]
    failure_count = len(failures) + (1 if not failures and report.get("error") else 0)
    testcases: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        failure_xml = ""
        error = item.get("error")
        if isinstance(error, dict):
            failure_xml = (
                f'<failure type="{escape(str(error.get("code") or "Error"))}">'
                f"{escape(str(error.get('message') or ''))}</failure>"
            )
        testcases.append(
            f'  <testcase classname="wikipediarag.cli" name="{escape(str(item.get("id") or "item"))}">'
            f"{failure_xml}</testcase>\n"
        )
    if not testcases and report.get("error"):
        error = report["error"] if isinstance(report["error"], dict) else {}
        testcases.append(
            '  <testcase classname="wikipediarag.cli" name="verify_document_corpus">'
            f'<failure type="{escape(str(error.get("code") or "Error"))}">'
            f"{escape(str(error.get('message') or ''))}</failure></testcase>\n"
        )
    junit = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="verify-document-corpus" tests="{len(testcases)}" failures="{failure_count}">\n'
        f"{''.join(testcases)}</testsuite>\n"
    )
    junit_path.write_text(junit, encoding="utf-8")


def _compose_up_document_upload_stack() -> None:
    env = {
        **os.environ,
        "MODEL_PROVIDER": "mock",
        "RETRIEVAL_PROFILE": "test_mock",
        "DOCUMENT_PARSER_SERVICES_REQUIRED": "true",
        "MINIO_PUBLIC_ENDPOINT": os.environ.get("MINIO_PUBLIC_ENDPOINT", "http://localhost:9000"),
        "API_PUBLIC_BASE_URL": os.environ.get("API_PUBLIC_BASE_URL", "http://localhost:8000"),
        "XBERG_URL": os.environ.get("XBERG_URL", "http://xberg:8000"),
        "DOCLING_URL": os.environ.get("DOCLING_URL", "http://docling:5001"),
        "METADATA_SERVICE_URL": os.environ.get("METADATA_SERVICE_URL", "http://metadata-service:8090"),
    }
    services = [
        "postgres",
        "redis",
        "minio",
        "opensearch",
        "mock-provider",
        "model-gateway",
        "metadata-service",
        "xberg",
        "docling",
        "api",
        "worker",
    ]
    subprocess.run(  # noqa: S603
        [_docker_executable(), "compose", "up", "-d", "--build", "--force-recreate", *services],
        check=True,
        env=env,
    )


def _docker_executable() -> str:
    return shutil.which("docker") or ("docker.exe" if sys.platform == "win32" else "docker")


def _record_check(report: dict[str, Any], name: str, passed: bool, details: dict[str, Any] | None = None) -> None:
    checks = report.setdefault("checks", [])
    if isinstance(checks, list):
        checks.append({"name": name, "passed": passed, "details": details or {}})


def _wait_json_ready(
    client: httpx.Client,
    url: str,
    name: str,
    *,
    require_ok: bool = False,
    timeout_seconds: int = 240,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = client.get(url, timeout=10)
            if response.status_code < 500:
                payload_json = response.json()
                payload = dict(payload_json) if isinstance(payload_json, dict) else {}
                status = payload.get("status")
                accepted_statuses = {"ok"} if require_ok else {"ok", "healthy", "degraded"}
                if status in accepted_statuses:
                    return payload
                last_error = response.text[:300]
        except Exception as exc:
            last_error = str(exc)
        time.sleep(3)
    raise RuntimeError(f"{name} did not become ready at {url}: {last_error}")


def _smoke_metadata_service(client: httpx.Client, base_url: str) -> None:
    response = client.post(
        f"{base_url}/v1/metadata:extract",
        json={"filename": "smoke.txt", "text": "Проверочный документ от 29.07.2026."},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("detected_language") != "ru" or payload.get("document_date") != "2026-07-29":
        raise RuntimeError(f"metadata service smoke returned unexpected metadata: {payload}")


def _smoke_xberg(client: httpx.Client, base_url: str) -> None:
    response = client.post(
        f"{base_url}/extract",
        files={"files": ("smoke.pdf", _verify_pdf_bytes(), "application/pdf")},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if not _payload_contains(payload, "Verify document"):
        raise RuntimeError("xberg smoke did not return expected fixture text")


def _smoke_docling(client: httpx.Client, base_url: str) -> None:
    response = client.post(
        f"{base_url}/v1/convert/file",
        data={"to_formats": "md", "target_type": "inbody", "do_ocr": "false", "table_mode": "fast"},
        files={"files": ("smoke.pdf", _verify_pdf_bytes(), "application/pdf")},
        timeout=180,
    )
    response.raise_for_status()
    payload = response.json()
    if not _payload_contains(payload, "Verify document"):
        raise RuntimeError("docling smoke did not return expected fixture text")


def _verify_pdf_bytes() -> bytes:
    objects = [
        b"1 0 obj\n<</Type /Catalog /Pages 2 0 R>>\nendobj\n",
        b"2 0 obj\n<</Type /Pages /Kids [3 0 R] /Count 1>>\nendobj\n",
        (
            b"3 0 obj\n<</Type /Page /Parent 2 0 R /MediaBox [0 0 320 144] "
            b"/Resources <</Font <</F1 5 0 R>>>> /Contents 4 0 R>>\nendobj\n"
        ),
        (
            b"4 0 obj\n<</Length 57>>\nstream\n"
            b"BT /F1 12 Tf 40 100 Td (Verify document 2026-07-29) Tj ET\nendstream\nendobj\n"
        ),
        b"5 0 obj\n<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>\nendobj\n",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for item in objects:
        offsets.append(len(content))
        content.extend(item)
    xref_offset = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    content.extend(
        (f"trailer\n<</Size {len(objects) + 1} /Root 1 0 R>>\nstartxref\n{xref_offset}\n%%EOF\n").encode("ascii")
    )
    return bytes(content)


def _payload_contains(payload: Any, needle: str) -> bool:
    if isinstance(payload, str):
        return needle in payload
    if isinstance(payload, dict):
        return any(_payload_contains(item, needle) for item in payload.values())
    if isinstance(payload, list):
        return any(_payload_contains(item, needle) for item in payload)
    return False


def _create_verify_knowledge_base(client: httpx.Client, api: str) -> str:
    response = client.post(
        f"{api}/api/v1/knowledge-bases",
        json={"name": f"Document Upload Verify {time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"},
        timeout=30,
    )
    response.raise_for_status()
    return str(response.json()["id"])


def _document_upload_fixtures() -> list[dict[str, Any]]:
    csv_content = (
        "title,document_date,language,note\nПроверочный документ,2026-07-29,ru,локальная таблица для проверки цитат\n"
    ).encode()
    return [
        {
            "filename": "verify-document.pdf",
            "content": _verify_pdf_bytes(),
            "content_type": "application/pdf",
            "parser_profile": "standard",
        },
        {
            "filename": "verify-metadata.csv",
            "content": csv_content,
            "content_type": "text/csv",
            "parser_profile": "standard",
        },
    ]


def _upload_verify_fixture(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    content = bytes(fixture["content"])
    checksum = hashlib.sha256(content).hexdigest()
    session_response = client.post(
        f"{api}/api/v1/uploads/sessions",
        json={
            "filename": fixture["filename"],
            "content_type": fixture["content_type"],
            "size_bytes": len(content),
            "checksum_sha256": checksum,
            "knowledge_base_id": knowledge_base_id,
            "parser_profile": fixture["parser_profile"],
            "metadata": {"verify_document_upload": True},
        },
        timeout=30,
    )
    session_response.raise_for_status()
    session = session_response.json()
    upload_response = client.put(
        session["upload_url"],
        content=content,
        headers=session.get("required_headers") or {},
        timeout=120,
    )
    upload_response.raise_for_status()
    complete_response = client.post(
        f"{api}/api/v1/uploads/sessions/{session['upload_session_id']}:complete",
        json={"metadata": {"verify_uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}},
        timeout=30,
    )
    complete_response.raise_for_status()
    payload = complete_response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("upload complete returned a non-object JSON payload")
    return dict(payload)


def _wait_job_terminal(client: httpx.Client, api: str, job_id: str, *, timeout_seconds: int = 360) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = _get_json(client, f"{api}/api/v1/ingestion-jobs/{job_id}")
        last_payload = payload if isinstance(payload, dict) else {}
        if last_payload.get("status") in {"completed", "failed", "cancelled"}:
            return last_payload
        progress = last_payload.get("progress") if isinstance(last_payload, dict) else {}
        print(
            json.dumps(
                {"job_id": job_id, "status": last_payload.get("status"), "progress": progress},
                ensure_ascii=False,
            )
        )
        time.sleep(3)
    raise RuntimeError(f"job did not reach terminal state: {last_payload}")


def _get_json(client: httpx.Client, url: str) -> dict[str, Any]:
    response = client.get(url, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected JSON object from {url}")
    return payload


def _assert_public_payload_is_safe(payload: Any) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    unsafe_tokens = ("object_key", "original_artifact_key", "normalized_artifact_key", "s3://", "parser_stderr")
    found = [token for token in unsafe_tokens if token in serialized]
    if found:
        raise RuntimeError(f"public metadata leaks private storage or parser fields: {found}")


def _verify_uploaded_retrieval(client: httpx.Client, api: str, knowledge_base_id: str) -> None:
    response = client.post(
        f"{api}/api/v1/search:debug",
        json={
            "message": "Проверочный документ 2026-07-29",
            "top_k": 5,
            "knowledge_base_ids": [knowledge_base_id],
            "retrieval_profile": "upload_sota_mvp",
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    evidence = payload.get("evidence") if isinstance(payload, dict) else None
    if not isinstance(evidence, list) or not evidence:
        raise RuntimeError(f"uploaded chunks are not retrievable: {payload}")
    first = evidence[0]
    metadata = first.get("metadata") if isinstance(first, dict) else {}
    if not isinstance(metadata, dict) or not metadata.get("locator"):
        raise RuntimeError(f"retrieved evidence is missing locator metadata: {first}")


def _write_document_upload_reports(report_dir: Path, report: dict[str, Any]) -> None:
    report_path = report_dir / "document-upload-report.json"
    junit_path = report_dir / "document-upload-junit.xml"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    failure = report.get("error") if not report.get("passed") else None
    failure_xml = ""
    if isinstance(failure, dict):
        failure_xml = (
            f'<failure type="{escape(str(failure.get("code") or "Error"))}">'
            f"{escape(str(failure.get('message') or ''))}</failure>"
        )
    junit = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="verify-document-upload" tests="1" failures="{0 if report.get("passed") else 1}">\n'
        f'  <testcase classname="wikipediarag.cli" name="verify_document_upload">{failure_xml}</testcase>\n'
        "</testsuite>\n"
    )
    junit_path.write_text(junit, encoding="utf-8")


def _require_api_ready(api: str, *, client: httpx.Client | None = None) -> httpx.Response:
    if client is None:
        with httpx.Client(timeout=30) as owned_client:
            return _require_api_ready(api, client=owned_client)
    ready = client.get(f"{api}/ready")
    ready.raise_for_status()
    if ready.json().get("status") != "ok":
        raise SystemExit(f"API is not ready: {ready.text}")
    return ready


def _iter_sse(lines: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current_event: str | None = None
    current_data: str | None = None
    for line in lines:
        if not line:
            if current_event and current_data:
                events.append({"event": current_event, "data": json.loads(current_data)})
            current_event = None
            current_data = None
            continue
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            current_data = line.removeprefix("data: ")
    return events


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        message = str(exc) or type(exc).__name__
        print(f"HTTP error: {message}", file=sys.stderr)
        raise SystemExit(1) from exc
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
