from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

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
    eval_miracl_parser.add_argument("--input", required=True)

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
    eval_release_gate_status_parser.add_argument("--json", action="store_true")

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
    return parser


def main() -> None:
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
        run_eval_run(args.suite, args.api)
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
        run_eval_miracl_map(args.input)
    elif args.command == "eval-review-candidates":
        run_eval_review_candidates(args.input, args.output_suite)
    elif args.command == "eval-freeze-reviewed":
        run_eval_freeze_reviewed(args.suite, args.dev_count, args.test_count)
    elif args.command == "eval-release-gate":
        run_eval_release_gate(args.suite, args.api)
    elif args.command == "eval-release-gate-status":
        run_eval_release_gate_status(args.suite, args.json)
    elif args.command == "eval-full":
        run_eval_full(args)
    elif args.command == "smoke-models":
        smoke_models(args.gateway, args.provider)
    elif args.command == "release-gate":
        run_eval(args.api)
        print("release gate passed")
    elif args.command == "demo-release-gate":
        demo_release_gate(args.api, args.job_id)


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


def run_eval_run(suite: str, api: str) -> None:
    from wikipediarag.eval.commands import eval_run
    from wikipediarag.eval.runner import EvalRunCliReporter

    report = asyncio.run(eval_run(suite=suite, api=api, progress_callback=EvalRunCliReporter()))
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


def run_eval_miracl_map(input_path: str) -> None:
    from wikipediarag.eval.commands import eval_miracl_map

    report = asyncio.run(eval_miracl_map(input_path=Path(input_path)))
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

    report = asyncio.run(eval_release_gate(suite=suite, api=api, progress_callback=ReleaseGateCliReporter()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get("passed"):
        raise SystemExit("eval-release-gate failed")


def run_eval_release_gate_status(suite: str, json_mode: bool) -> None:
    from wikipediarag.eval.commands import eval_release_gate_status
    from wikipediarag.eval.review import format_release_gate_status

    status = eval_release_gate_status(suite=suite)
    if json_mode:
        print(json.dumps(status.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    print(format_release_gate_status(status))


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
        ready = client.get(f"{api}/ready")
        ready.raise_for_status()
        if ready.json().get("status") != "ok":
            raise SystemExit(f"API is not ready: {ready.text}")
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
