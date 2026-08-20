from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
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
from wikipediarag.reliability import safe_failure_from_exception
from wikipediarag.research_tool_registry import DEFAULT_RESEARCH_TOOL_MODE, TOOL_MODE_ALLOWLISTS

DEEP_RESEARCH_GATE_COMPOSE_FILE = Path("compose.deep-research-gate.yaml")
DOCUMENT_UPLOAD_COMPOSE_SERVICES = (
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
)
DEEP_RESEARCH_GATE_COMPOSE_SERVICES = (
    "postgres",
    "redis",
    "minio",
    "opensearch",
    "mock-provider",
    "model-gateway",
    "api",
    "worker",
)
DEEP_RESEARCH_GATE_COMPOSE_START_ATTEMPTS = 3
DEEP_RESEARCH_GATE_RUN_RESERVE_SECONDS = 90
DEEP_RESEARCH_GATE_MIN_RUN_SECONDS = 120


class DeepResearchGateInfrastructureError(RuntimeError):
    safe_code = "DEEP_RESEARCH_GATE_COMPOSE_START_FAILED"


class DeepResearchRunTerminalTimeoutError(RuntimeError):
    safe_code = "DEEP_RESEARCH_RUN_TERMINAL_TIMEOUT"


class DeepResearchSuiteDeadlineExceededError(RuntimeError):
    safe_code = "DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED"


class ReliabilitySmokeError(RuntimeError):
    def __init__(self, safe_code: str) -> None:
        super().__init__(safe_code)
        self.safe_code = safe_code


class OperationalGateBlocked(RuntimeError):
    """A required operational environment capability is unavailable."""


def _safe_cli_failure(exc: BaseException, *, stage: str) -> dict[str, Any]:
    """Return a content-free CLI artifact failure without exception details."""
    failure = safe_failure_from_exception(exc, stage=stage)
    return {
        "code": failure.error_code,
        "retryable": failure.retryable,
        "stage": stage,
        "message": "operation failed",
    }


@dataclass(frozen=True)
class DeepResearchRuntime:
    api: str
    database_url: str | None = None
    compose_project: str | None = None
    api_port: int | None = None
    minio_port: int | None = None
    attempt: int | None = None
    openrouter_api_key_source: str = ""
    isolated: bool = False

    def public_details(self) -> dict[str, Any]:
        if not self.isolated:
            return {"mode": "external_api"}
        details: dict[str, Any] = {
            "mode": "isolated_compose",
            "compose_project": self.compose_project,
            "api": self.api,
            "minio_public_endpoint": f"http://127.0.0.1:{self.minio_port}",
            "database_host": "127.0.0.1",
            "database_port": self.database_url_port(),
            "startup_attempt": self.attempt,
        }
        if self.openrouter_api_key_source:
            details["openrouter_api_key_source"] = self.openrouter_api_key_source
        return details

    def database_url_port(self) -> int | None:
        if not self.database_url:
            return None
        try:
            return int(self.database_url.rsplit(":", 1)[1].split("/", 1)[0])
        except (IndexError, ValueError):
            return None


def _add_deep_research_runtime_arguments(
    parser: argparse.ArgumentParser,
    *,
    fixture_path: str,
    retrieval_profile: str,
    timeout_seconds: int,
    compose_model_provider: str,
    include_tool_mode: bool = True,
) -> None:
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--fixture-path", default=fixture_path)
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--max-tasks", type=int, default=None)
    parser.add_argument("--retrieval-profile", default=retrieval_profile)
    parser.add_argument(
        "--declared-context-tokens",
        type=int,
        default=80000,
        help="planner context window used by Deep Research evaluation (default: 80000)",
    )
    parser.add_argument("--timeout-seconds", type=int, default=timeout_seconds)
    parser.add_argument(
        "--compose-model-provider",
        choices=["mock", "openrouter"],
        default=compose_model_provider,
        help="model provider injected into Docker Compose for the smoke stack",
    )
    parser.add_argument("--context-productive-target", type=float, default=None)
    parser.add_argument("--context-soft-limit", type=float, default=None)
    parser.add_argument("--context-hard-input-limit", type=float, default=None)
    if include_tool_mode:
        parser.add_argument(
            "--tool-mode",
            choices=sorted(TOOL_MODE_ALLOWLISTS),
            default=DEFAULT_RESEARCH_TOOL_MODE,
            help="server-owned Deep Research tool allowlist mode for runtime runs",
        )
    parser.add_argument(
        "--admin-username",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_USERNAME", "admin"),
        help="local or hybrid auth platform-admin username",
    )
    parser.add_argument(
        "--admin-secret",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_PASSWORD", "admin"),  # noqa: S106
        help="platform-admin local login secret; defaults to the local bootstrap admin password",
    )
    parser.add_argument(
        "--admin-secret-file",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_PASSWORD_FILE"),
        help="file containing the platform-admin local login secret, or WIKIPEDIARAG_ADMIN_PASSWORD_FILE",
    )
    parser.add_argument("--skip-compose", action="store_true")
    parser.add_argument("--down-after", action="store_true")


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

    eval_document_prepare_parser = subparsers.add_parser("eval-document-prepare")
    eval_document_prepare_parser.add_argument("--dataset", choices=["rrncb-public"], default="rrncb-public")
    eval_document_prepare_parser.add_argument("--documents-dir", required=True)
    eval_document_prepare_parser.add_argument("--csv", dest="csv_path", default=None)
    eval_document_prepare_parser.add_argument(
        "--reviewed-translations",
        default=None,
        help="reviewed JSONL matrix for ru/en/uk/de/ko query variants",
    )
    eval_document_prepare_parser.add_argument("--output-suite", default="rrncb-public-v3")
    eval_document_prepare_parser.add_argument("--artifacts-dir", default="artifacts/eval")
    eval_document_prepare_parser.add_argument("--json", action="store_true")

    eval_document_translate_parser = subparsers.add_parser("eval-document-translate")
    eval_document_translate_parser.add_argument("--csv", dest="csv_path", required=True)
    eval_document_translate_parser.add_argument("--output", required=True)
    eval_document_translate_parser.add_argument("--gateway", default="http://localhost:8081")
    eval_document_translate_parser.add_argument("--batch-size", type=int, default=10)
    eval_document_translate_parser.add_argument("--model-alias", default="generator_main")

    eval_document_ingest_parser = subparsers.add_parser("eval-document-ingest")
    eval_document_ingest_parser.add_argument("--suite", default="rrncb-public-v3")
    eval_document_ingest_parser.add_argument("--api", default="http://localhost:8000")
    eval_document_ingest_parser.add_argument("--batch-size", type=int, default=5)
    eval_document_ingest_parser.add_argument("--upload-concurrency", type=int, default=2)
    eval_document_ingest_parser.add_argument("--document-timeout", type=int, default=900)
    eval_document_ingest_parser.add_argument("--batch-timeout", type=int, default=1800)
    eval_document_ingest_parser.add_argument("--suite-timeout", type=int, default=21600)
    eval_document_ingest_parser.add_argument("--run-id", default=None)
    eval_document_ingest_parser.add_argument("--resume-run-id", default=None)
    eval_document_ingest_parser.add_argument("--rerun-failed", action="store_true")
    eval_document_ingest_parser.add_argument("--artifacts-dir", default="artifacts/eval")
    eval_document_ingest_parser.add_argument("--json", action="store_true")

    eval_document_run_parser = subparsers.add_parser("eval-document-run")
    eval_document_run_parser.add_argument("--suite", default="rrncb-public-v3")
    eval_document_run_parser.add_argument("--api", default="http://localhost:8000")
    eval_document_run_parser.add_argument("--retrieval-profile", default="upload_sota_mvp")
    eval_document_run_parser.add_argument("--batch-size", type=int, default=1)
    eval_document_run_parser.add_argument("--question-timeout", type=int, default=300)
    eval_document_run_parser.add_argument("--suite-timeout", type=int, default=28800)
    eval_document_run_parser.add_argument("--run-id", default=None)
    eval_document_run_parser.add_argument("--resume-run-id", default=None)
    eval_document_run_parser.add_argument("--ingestion-run-id", required=True)
    eval_document_run_parser.add_argument("--split", choices=("dev", "test"), default="dev")
    eval_document_run_parser.add_argument("--rerun-failed", action="store_true")
    eval_document_run_parser.add_argument("--artifacts-dir", default="artifacts/eval")
    eval_document_run_parser.add_argument("--json", action="store_true")

    eval_document_retrieval_parser = subparsers.add_parser("eval-document-retrieval-run")
    eval_document_retrieval_parser.add_argument("--suite", default="rrncb-public-v3")
    eval_document_retrieval_parser.add_argument("--api", default="http://localhost:8000")
    eval_document_retrieval_parser.add_argument("--retrieval-profile", default="upload_sota_mvp")
    eval_document_retrieval_parser.add_argument("--batch-size", type=int, default=5)
    eval_document_retrieval_parser.add_argument("--run-id", default=None)
    eval_document_retrieval_parser.add_argument("--resume-run-id", default=None)
    eval_document_retrieval_parser.add_argument("--ingestion-run-id", required=True)
    eval_document_retrieval_parser.add_argument("--split", choices=("dev", "test"), default="dev")
    eval_document_retrieval_parser.add_argument("--rerun-failed", action="store_true")
    eval_document_retrieval_parser.add_argument("--artifacts-dir", default="artifacts/eval")
    eval_document_retrieval_parser.add_argument("--json", action="store_true")

    eval_document_status_parser = subparsers.add_parser("eval-document-status")
    eval_document_status_parser.add_argument("--suite", default="rrncb-public-v3")
    eval_document_status_parser.add_argument("--latest", action="store_true")
    eval_document_status_parser.add_argument("--json", action="store_true")
    eval_document_status_parser.add_argument("--artifacts-dir", default="artifacts/eval")

    eval_quality_prepare_parser = subparsers.add_parser("eval-quality-prepare")
    eval_quality_prepare_parser.add_argument("--corpus-dir", default="eval-corpus/p0-search-quality-v1")
    eval_quality_prepare_parser.add_argument("--allow-incomplete", action="store_true")

    eval_quality_scaffold_parser = subparsers.add_parser("eval-quality-scaffold")
    eval_quality_scaffold_parser.add_argument("--corpus-dir", default="eval-corpus/p0-search-quality-v1")
    eval_quality_scaffold_parser.add_argument("--overwrite", action="store_true")

    eval_quality_review_parser = subparsers.add_parser("eval-quality-review")
    eval_quality_review_parser.add_argument("--corpus-dir", default="eval-corpus/p0-search-quality-v1")
    eval_quality_review_parser.add_argument("--decisions", default=None)

    eval_quality_freeze_parser = subparsers.add_parser("eval-quality-freeze")
    eval_quality_freeze_parser.add_argument("--corpus-dir", default="eval-corpus/p0-search-quality-v1")

    eval_quality_run_parser = subparsers.add_parser("eval-quality-run")
    eval_quality_run_parser.add_argument("--corpus-dir", default="eval-corpus/p0-search-quality-v1")
    eval_quality_run_parser.add_argument("--api", default="http://localhost:8000")
    eval_quality_run_parser.add_argument("--split", choices=["dev", "test"], default="dev")
    eval_quality_run_parser.add_argument("--run-id", default=None)
    eval_quality_run_parser.add_argument("--resume-run-id", default=None)
    eval_quality_run_parser.add_argument("--rerun-failed", action="store_true")

    eval_quality_ingest_parser = subparsers.add_parser("eval-quality-ingest")
    eval_quality_ingest_parser.add_argument("--corpus-dir", default="eval-corpus/p0-search-quality-v1")
    eval_quality_ingest_parser.add_argument("--api", default="http://localhost:8000")
    eval_quality_ingest_parser.add_argument("--batch-size", type=int, default=5)
    eval_quality_ingest_parser.add_argument("--upload-concurrency", type=int, default=2)
    eval_quality_ingest_parser.add_argument("--timeout-seconds", type=int, default=900)
    eval_quality_ingest_parser.add_argument("--run-id", default=None)
    eval_quality_ingest_parser.add_argument("--resume-run-id", default=None)
    eval_quality_ingest_parser.add_argument("--rerun-failed", action="store_true")

    eval_quality_status_parser = subparsers.add_parser("eval-quality-status")
    eval_quality_status_parser.add_argument("--corpus-dir", default="eval-corpus/p0-search-quality-v1")
    eval_quality_status_parser.add_argument("--run-id", default=None)

    eval_quality_report_parser = subparsers.add_parser("eval-quality-report")
    eval_quality_report_parser.add_argument("--corpus-dir", default="eval-corpus/p0-search-quality-v1")
    eval_quality_report_parser.add_argument("--results", default=None)

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

    reliability_smoke_parser = subparsers.add_parser("reliability-smoke")
    reliability_smoke_parser.add_argument("--api", default="http://localhost:8000")
    reliability_smoke_parser.add_argument("--timeout-seconds", type=int, default=420)
    reliability_smoke_parser.add_argument(
        "--admin-username",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_USERNAME", "admin"),
    )
    reliability_smoke_parser.add_argument(
        "--admin-secret",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_PASSWORD", "admin"),  # noqa: S106
    )
    reliability_smoke_parser.add_argument(
        "--admin-secret-file", default=os.environ.get("WIKIPEDIARAG_ADMIN_PASSWORD_FILE")
    )
    reliability_smoke_parser.add_argument("--skip-compose", action="store_true")
    reliability_smoke_parser.add_argument("--down-after", action="store_true")

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

    authorization_matrix_parser = subparsers.add_parser("verify-live-http-authorization-matrix")
    _add_operational_gate_arguments(authorization_matrix_parser)

    acl_revocation_parser = subparsers.add_parser("verify-provider-acl-revocation")
    _add_operational_gate_arguments(acl_revocation_parser, provider_required=True)

    deep_research_parser = subparsers.add_parser("deep-research-smoke")
    _add_deep_research_runtime_arguments(
        deep_research_parser,
        fixture_path="tests/fixtures/deep_research/research_tasks.json",
        retrieval_profile="upload_mock",
        timeout_seconds=480,
        compose_model_provider="mock",
    )

    deep_research_hard_parser = subparsers.add_parser("deep-research-hard-gate")
    _add_deep_research_runtime_arguments(
        deep_research_hard_parser,
        fixture_path="tests/fixtures/deep_research/research_tasks_hard.json",
        retrieval_profile="upload_sota_mvp",
        timeout_seconds=900,
        compose_model_provider="openrouter",
    )

    deep_research_matrix_parser = subparsers.add_parser("deep-research-matrix")
    deep_research_matrix_parser.add_argument(
        "--fixture-path",
        default="tests/fixtures/deep_research/research_tasks.json",
    )
    deep_research_matrix_parser.add_argument("--task-id", action="append", default=[])
    deep_research_matrix_parser.add_argument("--max-tasks", type=int, default=None)
    deep_research_matrix_parser.add_argument(
        "--declared-context-tokens",
        type=int,
        default=80000,
        help="planner context window used by the offline matrix (default: 80000)",
    )
    deep_research_matrix_parser.add_argument(
        "--output-dir",
        default=None,
        help="optional output directory; defaults to artifacts/validation/deep-research-matrix/<timestamp>",
    )
    deep_research_tool_matrix_parser = subparsers.add_parser("deep-research-tool-matrix")
    _add_deep_research_runtime_arguments(
        deep_research_tool_matrix_parser,
        fixture_path="tests/fixtures/deep_research/research_tasks_tools.json",
        retrieval_profile="upload_sota_mvp",
        timeout_seconds=900,
        compose_model_provider="openrouter",
        include_tool_mode=False,
    )
    deep_research_tool_matrix_parser.add_argument(
        "--output-dir",
        default=None,
        help="optional output directory; defaults to artifacts/validation/deep-research-tool-matrix/<timestamp>",
    )

    workspace_reset_parser = subparsers.add_parser("workspace-reset")
    workspace_reset_parser.add_argument(
        "--apply", action="store_true", help="required; otherwise no database changes occur"
    )
    workspace_reset_parser.add_argument(
        "--all-data-confirmed",
        action="store_true",
        help="operator confirms all WikipediaRag data may be deleted",
    )
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
    elif args.command == "eval-document-prepare":
        run_eval_document_prepare(args)
    elif args.command == "eval-document-translate":
        run_eval_document_translate(args)
    elif args.command == "eval-document-ingest":
        run_eval_document_ingest(args)
    elif args.command == "eval-document-run":
        run_eval_document_run(args)
    elif args.command == "eval-document-retrieval-run":
        run_eval_document_retrieval(args)
    elif args.command == "eval-document-status":
        run_eval_document_status(args)
    elif args.command == "eval-quality-prepare":
        run_eval_quality_prepare(args)
    elif args.command == "eval-quality-scaffold":
        run_eval_quality_scaffold(args)
    elif args.command == "eval-quality-review":
        run_eval_quality_review(args)
    elif args.command == "eval-quality-freeze":
        run_eval_quality_freeze(args)
    elif args.command == "eval-quality-run":
        run_eval_quality_run(args)
    elif args.command == "eval-quality-ingest":
        run_eval_quality_ingest(args)
    elif args.command == "eval-quality-status":
        run_eval_quality_status(args)
    elif args.command == "eval-quality-report":
        run_eval_quality_report(args)
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
    elif args.command == "reliability-smoke":
        verify_reliability_smoke(args)
    elif args.command == "verify-document-corpus":
        verify_document_corpus(args)
    elif args.command == "verify-cross-tenant-hardening":
        verify_cross_tenant_hardening(args)
    elif args.command == "verify-live-http-authorization-matrix":
        verify_live_http_authorization_matrix(args)
    elif args.command == "verify-provider-acl-revocation":
        verify_provider_acl_revocation(args)
    elif args.command == "deep-research-smoke":
        verify_deep_research_smoke(args)
    elif args.command == "deep-research-hard-gate":
        verify_deep_research_smoke(args)
    elif args.command == "deep-research-matrix":
        verify_deep_research_matrix(args)
    elif args.command == "deep-research-tool-matrix":
        verify_deep_research_tool_matrix(args)
    elif args.command == "workspace-reset":
        run_workspace_reset(args)


def run_workspace_reset(args: argparse.Namespace) -> None:
    """Run the explicit clean-slate workspace reset boundary."""
    from wikipediarag.config import get_settings
    from wikipediarag.db import connect, ensure_schema
    from wikipediarag.workspace_reset import WorkspaceResetSafetyError, apply_workspace_reset, preflight_workspace_reset

    if args.apply and not args.all_data_confirmed:
        print("WORKSPACE_RESET_CONFIRMATION_REQUIRED", file=sys.stderr)
        raise SystemExit(2)

    async def execute() -> dict[str, Any]:
        async with connect() as conn:
            settings = get_settings()
            if not args.apply:
                return (await preflight_workspace_reset(conn, settings)).public_report()
            report = await apply_workspace_reset(conn, settings)
        await ensure_schema(settings)
        return report.public_report()

    try:
        report = asyncio.run(execute())
    except WorkspaceResetSafetyError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


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


def _add_operational_gate_arguments(parser: argparse.ArgumentParser, *, provider_required: bool = False) -> None:
    """Arguments shared by live authorization operational gates.

    The database URL is deliberately required and never written to reports. It
    is used only by the test-only identity seeder; every assertion endpoint is
    still exercised through HTTP.
    """
    parser.add_argument("--api", default=os.environ.get("WIKIPEDIARAG_OPERATIONAL_API", "http://localhost:8000"))
    parser.add_argument(
        "--admin-username",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_USERNAME", "admin"),
    )
    parser.add_argument("--admin-secret-file", default=os.environ.get("WIKIPEDIARAG_ADMIN_PASSWORD_FILE"))
    parser.add_argument(
        "--admin-secret",
        default=os.environ.get("WIKIPEDIARAG_ADMIN_PASSWORD", "admin"),  # noqa: S106
    )
    parser.add_argument(
        "--operational-test-database-url",
        default=os.environ.get("WIKIPEDIARAG_OPERATIONAL_TEST_DATABASE_URL"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--keep-fixtures", action="store_true")
    parser.add_argument("--gateway", default=os.environ.get("MODEL_GATEWAY_PUBLIC_URL", "http://localhost:8081"))
    parser.set_defaults(provider_required=provider_required)


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


def run_eval_document_prepare(args: argparse.Namespace) -> None:
    from wikipediarag.eval.document_benchmark import prepare_rrncb

    report = prepare_rrncb(
        documents_dir=Path(args.documents_dir),
        suite=str(args.output_suite),
        csv_path=Path(args.csv_path) if args.csv_path else None,
        translations_path=Path(args.reviewed_translations) if args.reviewed_translations else None,
        artifacts_dir=Path(args.artifacts_dir),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_document_translate(args: argparse.Namespace) -> None:
    from wikipediarag.eval.document_benchmark import generate_rrncb_translations

    report = generate_rrncb_translations(
        csv_path=Path(args.csv_path),
        output_path=Path(args.output),
        gateway_url=str(args.gateway),
        batch_size=int(args.batch_size),
        model_alias=str(args.model_alias),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_document_ingest(args: argparse.Namespace) -> None:
    from wikipediarag.eval.document_benchmark import ingest_rrncb

    report = ingest_rrncb(
        suite=str(args.suite),
        api_url=str(args.api),
        batch_size=int(args.batch_size),
        upload_concurrency=int(args.upload_concurrency),
        document_timeout=int(args.document_timeout),
        batch_timeout=int(args.batch_timeout),
        suite_timeout=int(args.suite_timeout),
        resume=True,
        run_id=str(args.run_id) if args.run_id else None,
        resume_run_id=str(args.resume_run_id) if args.resume_run_id else None,
        rerun_failed=bool(args.rerun_failed),
        artifacts_dir=Path(args.artifacts_dir),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_document_run(args: argparse.Namespace) -> None:
    from wikipediarag.eval.document_benchmark import run_rrncb

    try:
        report = asyncio.run(
            run_rrncb(
                suite=str(args.suite),
                api_url=str(args.api),
                profile_name=str(args.retrieval_profile),
                batch_size=int(args.batch_size),
                question_timeout=int(args.question_timeout),
                suite_timeout=int(args.suite_timeout),
                resume=True,
                rerun_failed=bool(args.rerun_failed),
                run_id=str(args.run_id) if args.run_id else None,
                resume_run_id=str(args.resume_run_id) if args.resume_run_id else None,
                ingestion_run_id=str(args.ingestion_run_id),
                split=cast(Literal["dev", "test"], str(args.split)),
                artifacts_dir=Path(args.artifacts_dir),
            )
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "failure": _safe_cli_failure(exc, stage="eval_document_run")}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_document_retrieval(args: argparse.Namespace) -> None:
    from wikipediarag.eval.document_benchmark import run_rrncb_retrieval

    try:
        report = asyncio.run(
            run_rrncb_retrieval(
                suite=str(args.suite),
                api_url=str(args.api),
                profile_name=str(args.retrieval_profile),
                batch_size=int(args.batch_size),
                rerun_failed=bool(args.rerun_failed),
                run_id=str(args.run_id) if args.run_id else None,
                resume_run_id=str(args.resume_run_id) if args.resume_run_id else None,
                ingestion_run_id=str(args.ingestion_run_id),
                split=cast(Literal["dev", "test"], str(args.split)),
                artifacts_dir=Path(args.artifacts_dir),
            )
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "failure": _safe_cli_failure(exc, stage="eval_document_retrieval")}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_document_status(args: argparse.Namespace) -> None:
    from wikipediarag.eval.document_benchmark import rrncb_status

    report = rrncb_status(suite=str(args.suite), artifacts_dir=Path(args.artifacts_dir))
    if args.json or not args.latest:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(
        "\n".join(
            [
                f"suite={report.get('suite')} status={report.get('status')}",
                f"processed={report.get('completed', 0)}/{report.get('total', 0)} failed={report.get('failed', 0)}",
                f"updated_at={report.get('updated_at', '')}",
                f"report={report.get('report', '')}",
            ]
        )
    )


def run_eval_quality_prepare(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_quality_prepare

    report = eval_quality_prepare(
        corpus_dir=Path(args.corpus_dir),
        strict_counts=not bool(args.allow_incomplete),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_quality_scaffold(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_quality_scaffold

    report = eval_quality_scaffold(corpus_dir=Path(args.corpus_dir), overwrite=bool(args.overwrite))
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_quality_review(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_quality_review

    report = eval_quality_review(
        corpus_dir=Path(args.corpus_dir),
        decisions_path=Path(args.decisions) if args.decisions else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_quality_freeze(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_quality_freeze

    report = eval_quality_freeze(corpus_dir=Path(args.corpus_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_quality_run(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_quality_run

    try:
        report = asyncio.run(
            eval_quality_run(
                corpus_dir=Path(args.corpus_dir),
                api=str(args.api),
                split=str(args.split),
                run_id=str(args.run_id) if args.run_id else None,
                resume_run_id=str(args.resume_run_id) if args.resume_run_id else None,
                rerun_failed=bool(args.rerun_failed),
            )
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "failure": _safe_cli_failure(exc, stage="eval_quality_run")}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_quality_ingest(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_quality_ingest

    try:
        report = eval_quality_ingest(
            corpus_dir=Path(args.corpus_dir),
            api=str(args.api),
            batch_size=int(args.batch_size),
            upload_concurrency=int(args.upload_concurrency),
            timeout_seconds=int(args.timeout_seconds),
            run_id=str(args.run_id) if args.run_id else None,
            resume_run_id=str(args.resume_run_id) if args.resume_run_id else None,
            rerun_failed=bool(args.rerun_failed),
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "failure": _safe_cli_failure(exc, stage="eval_quality_ingest")}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_quality_status(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_quality_status

    report = eval_quality_status(corpus_dir=Path(args.corpus_dir), run_id=str(args.run_id) if args.run_id else None)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_eval_quality_report(args: argparse.Namespace) -> None:
    from wikipediarag.eval.commands import eval_quality_report

    report = eval_quality_report(
        corpus_dir=Path(args.corpus_dir),
        results_path=Path(args.results) if args.results else None,
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
                "thinking": {"mode": "off", "effort": "none", "return_reasoning": False},
                "max_output_tokens": 4096,
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
        report["error"] = _safe_cli_failure(exc, stage="document_corpus")
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_document_upload_reports(report_dir, report)
        if args.down_after:
            subprocess.run([_docker_executable(), "compose", "down"], check=False)  # noqa: S603
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def verify_reliability_smoke(args: argparse.Namespace) -> None:
    """Exercise durable upload, worker restart, timeout and chat idempotency.

    The report intentionally stores only IDs, terminal statuses and safe error
    codes. It is a disposable isolated Compose stack, never the user's corpus.
    """

    report_dir = Path("artifacts/validation/reliability") / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report_dir.mkdir(parents=True, exist_ok=True)
    api = str(args.api).rstrip("/")
    report: dict[str, Any] = {
        "passed": False,
        "api": api,
        "report_dir": str(report_dir),
        "checks": [],
        "documents": [],
        "questions": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    runtime: DeepResearchRuntime | None = None
    exit_code = 0
    try:
        if not args.skip_compose:
            runtime = _compose_up_isolated_reliability_smoke()
            api = runtime.api
            report["api"] = api
            report["runtime"] = runtime.public_details()
            _record_check(report, "isolated_compose_up", True, runtime.public_details())
        else:
            report["runtime"] = {"mode": "external_api"}
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            _wait_json_ready(client, f"{api}/ready", "api", require_ok=True, timeout_seconds=int(args.timeout_seconds))
            _record_check(report, "api_ready", True)
            _authenticate_smoke_session(
                client,
                api,
                username=str(args.admin_username),
                admin_secret=_resolve_hardening_admin_secret(args),
            )
            knowledge_base_id = _create_verify_knowledge_base(client, api)
            report["knowledge_base_id"] = knowledge_base_id
            if runtime is not None:
                _reliability_compose(runtime, "stop", "worker")
                _record_check(report, "worker_stopped_before_upload_complete", True)
            uploads = [
                _upload_reliability_fixture(client, api, knowledge_base_id, fixture, index)
                for index, fixture in enumerate(_document_upload_fixtures(), start=1)
            ]
            report["documents"] = uploads
            if runtime is not None:
                _reliability_compose(runtime, "restart", "worker")
                _record_check(report, "worker_restarted", True)
            for upload in uploads:
                job = _wait_job_terminal(client, api, str(upload["job_id"]), timeout_seconds=int(args.timeout_seconds))
                if job.get("status") != "completed":
                    raise RuntimeError("reliability smoke upload did not complete")
                upload["job_status"] = str(job.get("status"))
            _record_check(report, "two_uploads_terminal_without_duplicates", True)

            # The isolated mock delays exactly three generation calls. With two
            # API attempts per call this opens the Gateway circuit without ever
            # invoking a real provider. After its cooldown, calls 3 and 4 pass.
            questions = [
                ("timeout-1", "Какая дата указана в проверочном документе?", "failed"),
                ("timeout-2", "Какая дата указана в проверочном документе?", "failed"),
                ("success-1", "Как называется проверочный документ?", "completed"),
                ("success-2", "Какой язык указан в проверочном документе?", "completed"),
            ]
            for index, (key_suffix, question, expected_status) in enumerate(questions, start=1):
                if index == 3:
                    time.sleep(16)
                result = _run_reliability_chat(client, api, knowledge_base_id, key_suffix, question)
                report["questions"].append(result)
                if result["terminal_status"] != expected_status:
                    raise RuntimeError("reliability smoke question did not reach expected terminal state")
            replay = _run_reliability_chat(client, api, knowledge_base_id, "success-2", questions[-1][1])
            original = cast(dict[str, Any], report["questions"][-1])
            if replay["query_run_id"] != original["query_run_id"] or replay["terminal_status"] != "completed":
                raise RuntimeError("idempotent chat replay created a different terminal run")
            _record_check(report, "chat_idempotency_replay", True, {"query_run_id": replay["query_run_id"]})
            _record_check(report, "four_questions_terminal", True)
        report["passed"] = True
    except Exception as exc:
        exit_code = 1
        safe_code = getattr(exc, "safe_code", "")
        if not safe_code and isinstance(exc, httpx.HTTPStatusError):
            safe_code = f"HTTP_{exc.response.status_code}"
        report["error"] = {"code": safe_code or safe_failure_from_exception(exc, stage="reliability_smoke").error_code}
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (report_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if runtime is not None and args.down_after:
            _compose_down_isolated_deep_research_hard_gate(runtime)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def _upload_reliability_fixture(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    fixture: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    content = bytes(fixture["content"])
    # Scope smoke idempotency keys to the disposable KB.  A previous smoke run
    # must not make a new run look like a fingerprint conflict merely because
    # it uses the same two deterministic fixtures.
    key = f"reliability-upload-{knowledge_base_id}-{index:02d}-{hashlib.sha256(content).hexdigest()[:16]}"
    request = {
        "filename": fixture["filename"],
        "content_type": fixture["content_type"],
        "size_bytes": len(content),
        "checksum_sha256": hashlib.sha256(content).hexdigest(),
        "knowledge_base_id": knowledge_base_id,
        "parser_profile": fixture["parser_profile"],
        "metadata": {"reliability_smoke": True},
    }
    headers = {"Idempotency-Key": key}
    first_session = client.post(f"{api}/api/v1/uploads/sessions", json=request, headers=headers, timeout=30)
    first_session.raise_for_status()
    second_session = client.post(f"{api}/api/v1/uploads/sessions", json=request, headers=headers, timeout=30)
    second_session.raise_for_status()
    session = dict(first_session.json())
    if str(second_session.json().get("upload_session_id") or "") != str(session.get("upload_session_id") or ""):
        raise RuntimeError("idempotent upload session replay returned a different session")
    upload_response = client.put(
        str(session["upload_url"]),
        content=content,
        headers=dict(session.get("required_headers") or {}),
        timeout=120,
    )
    upload_response.raise_for_status()
    complete_key = f"{key}-complete"
    complete_url = f"{api}/api/v1/uploads/sessions/{session['upload_session_id']}:complete"
    complete_payload = {"metadata": {"reliability_smoke": True}}
    first_complete = client.post(
        complete_url, json=complete_payload, headers={"Idempotency-Key": complete_key}, timeout=30
    )
    first_complete.raise_for_status()
    second_complete = client.post(
        complete_url, json=complete_payload, headers={"Idempotency-Key": complete_key}, timeout=30
    )
    second_complete.raise_for_status()
    completed = dict(first_complete.json())
    repeated = dict(second_complete.json())
    for field in ("document_id", "document_version_id", "job_id"):
        if str(repeated.get(field) or "") != str(completed.get(field) or ""):
            raise RuntimeError("idempotent upload completion replay returned a duplicate resource")
    return {
        "document_id": str(completed["document_id"]),
        "document_version_id": str(completed["document_version_id"]),
        "job_id": str(completed["job_id"]),
    }


def _run_reliability_chat(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    key_suffix: str,
    question: str,
) -> dict[str, str]:
    # The same logical question is replayed inside one smoke run, while a
    # later run gets a fresh key because its KB is different.
    key = f"reliability-chat-{knowledge_base_id}-{key_suffix}-f5c7b966"
    response = client.post(
        f"{api}/api/v1/chat",
        json={
            "message": question,
            "mode": "normal",
            "stream": True,
            "knowledge_base_ids": [knowledge_base_id],
            "retrieval_profile": "upload_mock",
            "client_request_id": key,
        },
        headers={"Idempotency-Key": key},
        timeout=120,
    )
    if response.is_error:
        safe_code = f"HTTP_{response.status_code}"
        try:
            error_payload = response.json()
            root_error = error_payload.get("error") if isinstance(error_payload, dict) else None
            if isinstance(root_error, dict) and isinstance(root_error.get("code"), str):
                safe_code = str(root_error["code"])
            detail = error_payload.get("detail") if isinstance(error_payload, dict) else None
            if isinstance(detail, dict):
                error = detail.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    safe_code = str(error["code"])
            elif detail == "idempotent chat record is missing query run id":
                safe_code = "IDEMPOTENCY_RECORD_INCOMPLETE"
            elif detail == "idempotent chat query run is unavailable":
                safe_code = "IDEMPOTENCY_QUERY_RUN_UNAVAILABLE"
        except (ValueError, AttributeError):
            pass
        raise ReliabilitySmokeError(safe_code)
    events = _iter_sse(response.iter_lines())
    sequences = [int(event["data"].get("sequence") or 0) for event in events]
    if sequences != list(range(1, len(sequences) + 1)):
        raise RuntimeError("chat SSE sequence is not continuous")
    terminals = [event for event in events if event["event"] in {"run.completed", "run.failed"}]
    if len(terminals) != 1:
        raise RuntimeError("chat SSE did not contain exactly one terminal event")
    terminal = terminals[0]
    payload = terminal["data"]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "query_run_id": str(payload.get("query_run_id") or ""),
        "terminal_status": "completed" if terminal["event"] == "run.completed" else "failed",
        "error_code": str(data.get("code") or data.get("error_code") or ""),
    }


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
        report["error"] = _safe_cli_failure(exc, stage="cross_tenant_hardening")
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
        report["error"] = _safe_cli_failure(exc, stage="cross_tenant_hardening")
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_cross_tenant_hardening_reports(report_dir, report)
        if args.down_after:
            subprocess.run([_docker_executable(), "compose", "down"], check=False)  # noqa: S603
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def verify_live_http_authorization_matrix(args: argparse.Namespace) -> None:
    """Audit the deployed HTTP surface against its executable route contracts.

    This command intentionally does not start Compose.  Operational environments
    must provide an already-ready API and an explicit test database URL for the
    identity seeder; an unmet prerequisite is recorded as BLOCKED and exits
    non-zero instead of being represented as a passed check.
    """
    from wikipediarag.operational_authorization import (
        public_route_contracts,
        route_requires_cross_tenant_replay,
        safe_contract_report,
        tenant_denial_probe,
    )

    api = str(args.api).rstrip("/")
    report_dir = Path("artifacts/validation/live-http-authorization-matrix") / time.strftime(
        "%Y%m%dT%H%M%SZ", time.gmtime()
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    routes = public_route_contracts()
    report: dict[str, Any] = {
        "passed": False,
        "api": api,
        "report_dir": str(report_dir),
        "routes": safe_contract_report(routes),
        "route_count": len(routes),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": [],
    }
    exit_code = 0
    try:
        if not args.operational_test_database_url:
            raise OperationalGateBlocked("OPERATIONAL_TEST_DATABASE_URL_REQUIRED")
        admin_secret = _resolve_hardening_admin_secret(args)
        if not admin_secret:
            raise OperationalGateBlocked("ADMIN_SECRET_REQUIRED")
        with httpx.Client(timeout=max(30, int(args.timeout_seconds)), follow_redirects=True) as client:
            try:
                _wait_json_ready(client, f"{api}/ready", "api", require_ok=True)
            except RuntimeError as exc:
                raise OperationalGateBlocked("API_READINESS_REQUIRED") from exc
            _record_check(report, "api_ready", True)
            schema = _get_json(client, f"{api}/openapi.json")
            _verify_live_openapi_authorization_contracts(schema, routes)
            _record_check(report, "openapi_route_contracts", True, {"route_count": len(routes)})
            fixtures = _build_live_authorization_matrix_fixtures(
                client,
                api=api,
                admin_secret=admin_secret,
                admin_username=str(args.admin_username),
                database_url=str(args.operational_test_database_url),
                timeout_seconds=int(args.timeout_seconds),
            )
            _record_check(report, "two_tenant_non_bypassing_identities", True)
            states: list[dict[str, Any]] = []
            try:
                for route in routes:
                    authorized_client = (
                        fixtures.admin if route.path.startswith("/api/v1/admin/") else fixtures.manager_a
                    )
                    if route.path in {"/health", "/ready"} or route.path in {
                        "/api/v1/auth/local/login",
                        "/api/v1/auth/oidc/start",
                        "/api/v1/auth/oidc/callback",
                    }:
                        authorized_client = fixtures.public
                    authorized = _live_matrix_request(authorized_client, api, route, fixtures, foreign=False)
                    authorized_summary = _live_authorized_probe(authorized, public=authorized_client is fixtures.public)
                    state: dict[str, Any] = {
                        "method": route.method,
                        "path": route.path,
                        "authorized": authorized_summary,
                    }
                    if route_requires_cross_tenant_replay(route):
                        before = _live_matrix_target_fingerprint(fixtures.manager_a, api, route, fixtures)
                        rejected = _live_matrix_request(fixtures.manager_b, api, route, fixtures, foreign=True)
                        denial = tenant_denial_probe(rejected, forbidden_values=fixtures.foreign_identifiers())
                        after = _live_matrix_target_fingerprint(fixtures.manager_a, api, route, fixtures)
                        denial["side_effect_free"] = before == after
                        denial["passed"] = bool(denial["passed"]) and bool(denial["side_effect_free"])
                        state["rejection"] = denial
                    elif route.contract.cross_tenant.value == "actor-scoped":
                        scoped = _live_matrix_request(fixtures.manager_b, api, route, fixtures, foreign=True)
                        leak = _response_contains_any(scoped, fixtures.foreign_identifiers())
                        state["rejection"] = {
                            **_safe_http_summary(scoped),
                            "passed": scoped.status_code < 500 and not leak,
                            "leak_detected": leak,
                            "side_effect_free": True,
                        }
                    else:
                        state["rejection"] = {"passed": True, "state": "NOT_APPLICABLE"}
                    state["state"] = (
                        "EXECUTED"
                        if bool(state["authorized"]["passed"]) and bool(state["rejection"]["passed"])
                        else "FAILED"
                    )
                    states.append(state)
            finally:
                fixtures.close()
            report["route_states"] = states
            failed = [item for item in states if item["state"] != "EXECUTED"]
            if failed:
                raise RuntimeError("AUTHORIZATION_ROUTE_MATRIX_FAILED")
            _record_check(report, "every_public_route_executed", True, {"route_count": len(states)})
        report["passed"] = True
    except OperationalGateBlocked as exc:
        exit_code = 2
        report["blocked"] = {"code": str(exc)}
    except Exception as exc:
        exit_code = 1
        report["error"] = _safe_cli_failure(exc, stage="live_http_authorization_matrix")
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_operational_gate_report(report_dir, "live-http-authorization-matrix", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


@dataclass
class _LiveAuthorizationMatrixFixtures:
    """Test-owned HTTP fixtures; reports retain only route/status metadata."""

    public: httpx.Client
    admin: httpx.Client
    manager_a: httpx.Client
    manager_b: httpx.Client
    identifiers: dict[str, str]
    admin_username: str
    admin_secret: str
    manager_a_username: str
    manager_a_password: str

    def foreign_identifiers(self) -> tuple[str, ...]:
        return tuple(value for key, value in self.identifiers.items() if key.endswith("_a"))

    def close(self) -> None:
        for client in (self.public, self.admin, self.manager_a, self.manager_b):
            client.close()


def _build_live_authorization_matrix_fixtures(
    client: httpx.Client,
    *,
    api: str,
    admin_secret: str,
    admin_username: str,
    database_url: str,
    timeout_seconds: int,
) -> _LiveAuthorizationMatrixFixtures:
    """Create only generated tenants, memberships, and HTTP-owned resources."""
    _login_for_hardening(client, api, admin_username, admin_secret)
    suffix = f"auth-matrix-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    tenant_a = _create_hardening_tenant(client, api, suffix, "a")
    tenant_b = _create_hardening_tenant(client, api, suffix, "b")
    manager_a_identity, manager_b_identity = asyncio.run(
        _seed_live_matrix_identities(tenant_a=tenant_a, tenant_b=tenant_b, database_url=database_url)
    )
    manager_a = httpx.Client(timeout=max(30, timeout_seconds), follow_redirects=True)
    manager_b = httpx.Client(timeout=max(30, timeout_seconds), follow_redirects=True)
    _login_deep_research_viewer(manager_a, api, manager_a_identity["username"], manager_a_identity["password"])
    _login_deep_research_viewer(manager_b, api, manager_b_identity["username"], manager_b_identity["password"])
    _select_hardening_tenant(manager_a, api, tenant_a)
    _select_hardening_tenant(manager_b, api, tenant_b)
    kb_a = _create_verify_knowledge_base(manager_a, api)
    kb_b = _create_verify_knowledge_base(manager_b, api)
    kb_delete_a = _create_verify_knowledge_base(manager_a, api)
    group = manager_a.post(
        f"{api}/api/v1/groups", json={"name": f"matrix-group-{suffix}", "group_type": "LOCAL"}, timeout=30
    )
    group.raise_for_status()
    source = manager_a.post(
        f"{api}/api/v1/knowledge-bases/{kb_a}/sources",
        json={"kind": "wikipedia", "name": f"matrix-source-{suffix}", "config": {}},
        timeout=30,
    )
    source.raise_for_status()
    grants = manager_a.get(f"{api}/api/v1/knowledge-bases/{kb_a}/grants", timeout=30)
    grants.raise_for_status()
    grant = manager_a.post(
        f"{api}/api/v1/knowledge-bases/{kb_a}/grants",
        json={"subject_type": "GROUP", "subject_id": str(group.json()["id"]), "role": "MANAGER"},
        timeout=30,
    )
    grant.raise_for_status()
    upload = _upload_operational_acl_fixture(manager_a, api, kb_a, f"matrix-{uuid.uuid4().hex}")
    job = _wait_job_terminal(manager_a, api, upload["job_id"], timeout_seconds=timeout_seconds)
    if job.get("status") != "completed":
        raise OperationalGateBlocked("MATRIX_UPLOAD_FIXTURE_NOT_COMPLETED")
    batch = manager_a.post(
        f"{api}/api/v1/uploads/batches",
        json={
            "knowledge_base_id": kb_a,
            "items": [
                {
                    "filename": "matrix-pending.txt",
                    "content_type": "text/plain",
                    "size_bytes": 1,
                    "checksum_sha256": hashlib.sha256(b"x").hexdigest(),
                }
            ],
        },
        timeout=30,
    )
    batch.raise_for_status()
    sync = manager_a.post(
        f"{api}/api/v1/knowledge-bases/{kb_a}/sources/{source.json()['id']}:sync",
        json={"mode": "incremental"},
        timeout=30,
    )
    sync_payload = sync.json() if sync.is_success else {}
    plan = manager_a.post(
        f"{api}/api/v1/research-plans",
        json={
            "topic": "operational authorization matrix",
            "knowledge_base_id": kb_a,
            "retrieval_profile": "upload_mock",
        },
        timeout=30,
    )
    plan_payload = plan.json() if plan.is_success else {}
    run = manager_a.post(
        f"{api}/api/v1/research-runs",
        json={
            "topic": "operational authorization matrix",
            "knowledge_base_id": kb_a,
            "retrieval_profile": "upload_mock",
        },
        timeout=30,
    )
    run_payload = run.json() if run.is_success else {}
    debug = manager_a.post(
        f"{api}/api/v1/search:debug",
        json={"message": "authorization matrix", "knowledge_base_ids": [kb_a], "retrieval_profile": "upload_mock"},
        timeout=max(30, timeout_seconds),
    )
    debug_payload = debug.json() if debug.is_success else {}
    document_delete = _upload_operational_acl_fixture(manager_a, api, kb_a, f"matrix-delete-{uuid.uuid4().hex}")
    delete_job = _wait_job_terminal(manager_a, api, document_delete["job_id"], timeout_seconds=timeout_seconds)
    if delete_job.get("status") != "completed":
        raise OperationalGateBlocked("MATRIX_DELETE_DOCUMENT_FIXTURE_NOT_COMPLETED")
    identifiers = {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "kb_a": kb_a,
        "kb_b": kb_b,
        "kb_delete_a": kb_delete_a,
        "group_a": str(group.json()["id"]),
        "source_a": str(source.json()["id"]),
        "grant_a": str(grant.json()["id"]),
        "job_a": str(upload["job_id"]),
        "document_a": str(upload["document_id"]),
        "document_delete_a": str(document_delete["document_id"]),
        "upload_session_a": str(upload.get("upload_session_id") or ""),
        "batch_a": str(batch.json()["batch_id"]),
        "source_sync_run_a": str(sync_payload.get("run_id") or uuid.uuid4()),
        "research_plan_a": str(plan_payload.get("plan_id") or uuid.uuid4()),
        "research_run_a": str(run_payload.get("run_id") or uuid.uuid4()),
        "query_run_a": str(debug_payload.get("query_run_id") or uuid.uuid4()),
        "user_a": str(manager_a_identity["user_id"]),
        "connection_a": str(uuid.uuid4()),
        "model_a": str(uuid.uuid4()),
        "revision_a": str(uuid.uuid4()),
    }
    return _LiveAuthorizationMatrixFixtures(
        public=httpx.Client(timeout=max(30, timeout_seconds), follow_redirects=True),
        admin=client,
        manager_a=manager_a,
        manager_b=manager_b,
        identifiers=identifiers,
        admin_username=admin_username,
        admin_secret=admin_secret,
        manager_a_username=manager_a_identity["username"],
        manager_a_password=manager_a_identity["password"],
    )


async def _seed_live_matrix_identities(
    *, tenant_a: str, tenant_b: str, database_url: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Use one event loop: SQLAlchemy's async pool is deliberately loop-bound."""
    manager_a = await _seed_deep_research_viewer_user(tenant_id=tenant_a, database_url=database_url)
    manager_b = await _seed_deep_research_viewer_user(tenant_id=tenant_b, database_url=database_url)
    await _seed_operational_tenant_membership(
        tenant_id=tenant_a, user_id=manager_a["user_id"], database_url=database_url
    )
    await _seed_operational_tenant_membership(
        tenant_id=tenant_b, user_id=manager_b["user_id"], database_url=database_url
    )
    return manager_a, manager_b


def _live_matrix_request(
    client: httpx.Client, api: str, route: Any, fixtures: _LiveAuthorizationMatrixFixtures, *, foreign: bool
) -> httpx.Response:
    path = str(route.path)
    values = fixtures.identifiers
    replacements = {
        "kb_id": values["kb_delete_a"] if route.method == "DELETE" else values["kb_a"],
        "source_id": values["source_a"],
        "grant_id": values["grant_a"],
        "group_id": values["group_a"],
        "job_id": values["job_a"],
        "upload_session_id": values["upload_session_a"],
        "batch_id": values["batch_a"],
        "document_id": values["document_delete_a"] if route.method == "DELETE" else values["document_a"],
        "query_run_id": values["query_run_a"],
        "research_plan_id": values["research_plan_a"],
        "research_run_id": values["research_run_a"],
        "user_id": values["user_a"],
        "tenant_id": values["tenant_a"],
        "connection_id": values["connection_a"],
        "model_id": values["model_a"],
        "revision_id": values["revision_a"],
    }
    for key, value in replacements.items():
        path = path.replace("{" + key + "}", value)
    payload = _live_matrix_payload(route, fixtures, foreign=foreign)
    response = client.request(route.method, f"{api}{path}", json=payload, timeout=180)
    if path == "/api/v1/auth/logout" and client is fixtures.manager_a:
        _login_deep_research_viewer(client, api, fixtures.manager_a_username, fixtures.manager_a_password)
        _select_hardening_tenant(client, api, fixtures.identifiers["tenant_a"])
    return response


def _live_matrix_payload(
    route: Any, fixtures: _LiveAuthorizationMatrixFixtures, *, foreign: bool
) -> dict[str, Any] | None:
    path = str(route.path)
    values = fixtures.identifiers
    foreign_kb = values["kb_a"] if foreign else values["kb_a"]
    if path == "/api/v1/auth/local/login":
        return {"username": fixtures.admin_username, "password": fixtures.admin_secret, "remember_me": False}
    if path == "/api/v1/auth/oidc/callback":
        return None
    if path == "/api/v1/auth/local/password":
        return {"current_password": fixtures.manager_a_password, "new_password": fixtures.manager_a_password}
    if path == "/api/v1/auth/session/tenant":
        return {"tenant_id": values["tenant_b"] if foreign else values["tenant_a"]}
    if path == "/api/v1/groups" and route.method == "POST":
        return {"name": f"matrix-extra-{uuid.uuid4().hex[:8]}", "group_type": "LOCAL", "tenant_id": values["tenant_a"]}
    if path == "/api/v1/groups/{group_id}" and route.method == "PATCH":
        return {"name": f"matrix-patched-{uuid.uuid4().hex[:8]}"}
    if path == "/api/v1/knowledge-bases" and route.method == "POST":
        return {"name": f"matrix-extra-{uuid.uuid4().hex[:8]}", "tenant_id": values["tenant_a"]}
    if path == "/api/v1/knowledge-bases/{kb_id}" and route.method == "PATCH":
        return {"name": f"matrix-patched-{uuid.uuid4().hex[:8]}"}
    if path.endswith("/grants") and route.method == "POST":
        return {"subject_type": "USER", "subject_id": values["user_a"], "role": "MANAGER"}
    if "/grants/{grant_id}" in path and route.method == "PATCH":
        return {"role": "OWNER"}
    if path.endswith("/sources") and route.method == "POST":
        return {"kind": "wikipedia", "name": f"matrix-source-{uuid.uuid4().hex[:8]}", "config": {}}
    if "/sources/{source_id}" in path and route.method == "PATCH":
        return {"name": f"matrix-source-patched-{uuid.uuid4().hex[:8]}"}
    if path.endswith("/access"):
        return {"policy": "kb", "user_ids": [], "group_ids": [], "apply_to_existing": False}
    if path.endswith(":sync"):
        return {"mode": "incremental"}
    if path.endswith("/wikipedia/imports"):
        return {"xml_path": "not-a-permitted-import.xml", "tenant_id": values["tenant_a"]}
    if path.endswith("/zim-imports"):
        return {"zim_filename": "not-a-permitted-import.zim", "tenant_id": values["tenant_a"]}
    if path == "/api/v1/uploads/sessions":
        content = b"x"
        return {
            "filename": "matrix-request.txt",
            "content_type": "text/plain",
            "size_bytes": len(content),
            "checksum_sha256": hashlib.sha256(content).hexdigest(),
            "knowledge_base_id": foreign_kb,
        }
    if path == "/api/v1/uploads/batches":
        return {
            "knowledge_base_id": foreign_kb,
            "items": [
                {
                    "filename": "matrix-batch.txt",
                    "size_bytes": 1,
                    "checksum_sha256": hashlib.sha256(b"x").hexdigest(),
                }
            ],
        }
    if path.endswith(":complete"):
        return {"metadata": {}}
    if path.endswith("/documents/{document_id}/search"):
        return {"query": "authorization matrix"}
    if path == "/api/v1/search":
        return {
            "query": "authorization matrix",
            "knowledge_base_ids": [foreign_kb],
            "ranking_profile": "upload_mock",
        }
    if path == "/api/v1/search:debug":
        return {
            "message": "authorization matrix",
            "knowledge_base_ids": [foreign_kb],
            "retrieval_profile": "upload_mock",
        }
    if path == "/api/v1/chat":
        return {
            "message": "authorization matrix",
            "knowledge_base_ids": [foreign_kb],
            "retrieval_profile": "upload_mock",
            "stream": True,
        }
    if path.endswith("/feedback"):
        return {"rating": "up"}
    if path.endswith("/evaluation"):
        return {"label": "useful"}
    if path == "/api/v1/research-plans" and route.method == "POST":
        return {"topic": "authorization matrix", "knowledge_base_id": foreign_kb, "retrieval_profile": "upload_mock"}
    if path == "/api/v1/research-plans/{research_plan_id}" and route.method == "PATCH":
        return {"notes": "matrix"}
    if path == "/api/v1/research-runs" and route.method == "POST":
        return {"topic": "authorization matrix", "knowledge_base_id": foreign_kb, "retrieval_profile": "upload_mock"}
    return {} if route.method in {"POST", "PATCH", "PUT"} else None


def _live_authorized_probe(response: httpx.Response, *, public: bool) -> dict[str, Any]:
    """A 4xx validation/conflict can prove auth passed; 401/403 and 5xx cannot."""
    summary = _safe_http_summary(response)
    summary["passed"] = response.status_code < 500 and (public or response.status_code not in {401, 403})
    return summary


def _safe_http_summary(response: httpx.Response) -> dict[str, Any]:
    from wikipediarag.operational_authorization import safe_response_summary

    return safe_response_summary(response)


def _response_contains_any(response: httpx.Response, values: tuple[str, ...]) -> bool:
    from wikipediarag.operational_authorization import response_contains_forbidden_values

    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text
    return response_contains_forbidden_values(payload, values)


def _live_matrix_target_fingerprint(
    client: httpx.Client, api: str, route: Any, fixtures: _LiveAuthorizationMatrixFixtures
) -> str | None:
    """Hash only a test-owned target representation to prove rejected writes did not alter it."""
    path = str(route.path)
    target: str | None = None
    if "{document_id}" in path:
        target = f"/api/v1/documents/{fixtures.identifiers['document_a']}"
    elif "{source_id}" in path:
        target = f"/api/v1/knowledge-bases/{fixtures.identifiers['kb_a']}/sources/{fixtures.identifiers['source_a']}"
    elif "{grant_id}" in path:
        target = f"/api/v1/knowledge-bases/{fixtures.identifiers['kb_a']}/grants"
    elif "{group_id}" in path:
        target = "/api/v1/groups"
    elif "{kb_id}" in path:
        target = f"/api/v1/knowledge-bases/{fixtures.identifiers['kb_a']}"
    if target is None:
        return None
    response = client.get(f"{api}{target}", timeout=30)
    if not response.is_success:
        return None
    return hashlib.sha256(response.content).hexdigest()


def verify_provider_acl_revocation(args: argparse.Namespace) -> None:
    """Run the provider-backed current-ACL read-surface revocation gate."""
    from wikipediarag.operational_authorization import exposure_route_contracts, revocation_probe, safe_contract_report

    api = str(args.api).rstrip("/")
    report_dir = Path("artifacts/validation/provider-acl-revocation") / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report_dir.mkdir(parents=True, exist_ok=True)
    routes = exposure_route_contracts()
    report: dict[str, Any] = {
        "passed": False,
        "api": api,
        "report_dir": str(report_dir),
        "exposure_routes": safe_contract_report(routes),
        "checks": [],
        "probes": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    exit_code = 0
    try:
        if not args.operational_test_database_url:
            raise OperationalGateBlocked("OPERATIONAL_TEST_DATABASE_URL_REQUIRED")
        admin_secret = _resolve_hardening_admin_secret(args)
        if not admin_secret:
            raise OperationalGateBlocked("ADMIN_SECRET_REQUIRED")
        with httpx.Client(timeout=180, follow_redirects=True) as admin_client:
            try:
                _wait_json_ready(admin_client, f"{api}/ready", "api", require_ok=True)
            except RuntimeError as exc:
                raise OperationalGateBlocked("API_READINESS_REQUIRED") from exc
            _record_check(report, "api_ready", True)
            smoke_models(str(args.gateway).rstrip("/"), "openrouter")
            _record_check(report, "model_gateway_openrouter_canary", True)
            _login_for_hardening(admin_client, api, str(args.admin_username), admin_secret)
            session = _get_json(admin_client, f"{api}/api/v1/auth/session")
            actor: dict[str, Any] = dict(session.get("user") or {}) if isinstance(session.get("user"), dict) else {}
            admin_user_id = str(actor.get("id") or "")
            if not admin_user_id:
                raise OperationalGateBlocked("ADMIN_SESSION_USER_ID_REQUIRED")
            suffix = time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
            tenant_id = _create_hardening_tenant(admin_client, api, suffix, "acl")
            asyncio.run(
                _seed_operational_tenant_membership(
                    tenant_id=tenant_id,
                    user_id=admin_user_id,
                    database_url=str(args.operational_test_database_url),
                )
            )
            viewer = asyncio.run(
                _seed_deep_research_viewer_user(
                    tenant_id=tenant_id,
                    database_url=str(args.operational_test_database_url),
                )
            )
            _select_hardening_tenant(admin_client, api, tenant_id)
            kb_id = _create_deep_research_knowledge_base(admin_client, api, f"acl-revocation-{suffix}")
            grant = admin_client.post(
                f"{api}/api/v1/knowledge-bases/{kb_id}/grants",
                json={"subject_type": "USER", "subject_id": viewer["user_id"], "role": "EDITOR"},
                timeout=30,
            )
            grant.raise_for_status()
            viewer_client = httpx.Client(timeout=180, follow_redirects=True)
            try:
                _login_deep_research_viewer(viewer_client, api, viewer["username"], viewer["password"])
                marker = f"acl-revocation-{uuid.uuid4().hex}"
                upload = _upload_operational_acl_fixture(admin_client, api, kb_id, marker)
                job = _wait_job_terminal(admin_client, api, upload["job_id"])
                if job.get("status") != "completed":
                    raise RuntimeError("ACL_REVOCATION_UPLOAD_NOT_COMPLETED")
                document_id = str(upload["document_id"])
                baseline = viewer_client.post(
                    f"{api}/api/v1/search",
                    json={"query": marker, "knowledge_base_ids": [kb_id], "ranking_profile": "upload_sota_mvp"},
                    timeout=180,
                )
                baseline.raise_for_status()
                if marker not in baseline.text:
                    raise RuntimeError("ACL_REVOCATION_BASELINE_NOT_VISIBLE")
                run_id = _create_deep_research_run(
                    viewer_client,
                    api,
                    kb_id,
                    marker,
                    retrieval_profile="upload_sota_mvp",
                    tool_mode=DEFAULT_RESEARCH_TOOL_MODE,
                    context_policy_override=None,
                    run_deadline_seconds=max(120, int(args.timeout_seconds)),
                )
                detail = _wait_research_run_terminal(
                    viewer_client, api, run_id, timeout_seconds=int(args.timeout_seconds)
                )
                evidence_raw = detail.get("evidence") if isinstance(detail, dict) else []
                evidence = evidence_raw if isinstance(evidence_raw, list) else []
                linked = [
                    str(item.get("id"))
                    for item in evidence
                    if isinstance(item, dict) and item.get("document_id") == document_id
                ]
                if not linked:
                    raise RuntimeError("ACL_REVOCATION_RESEARCH_EVIDENCE_MISSING")
                debug_baseline = viewer_client.post(
                    f"{api}/api/v1/search:debug",
                    json={"message": marker, "knowledge_base_ids": [kb_id], "retrieval_profile": "upload_sota_mvp"},
                    timeout=180,
                )
                debug_baseline.raise_for_status()
                debug_payload = debug_baseline.json()
                query_run_id = str(debug_payload.get("query_run_id") or "") if isinstance(debug_payload, dict) else ""
                if not query_run_id:
                    raise RuntimeError("ACL_REVOCATION_QUERY_RUN_MISSING")
                revoke = admin_client.patch(
                    f"{api}/api/v1/documents/{document_id}/access",
                    json={"policy": "restricted", "user_ids": [], "group_ids": []},
                    timeout=30,
                )
                revoke.raise_for_status()
                _record_check(report, "document_acl_revoked", True)
                forbidden = [marker, document_id, *linked]
                for route, response in _post_revocation_exposure_requests(
                    viewer_client, api, kb_id, document_id, run_id, marker, query_run_id
                ):
                    item = {
                        "method": route[0],
                        "path": route[1],
                        **revocation_probe(response, forbidden_values=forbidden),
                    }
                    report["probes"].append(item)
                missing = {(item.method, item.path) for item in routes} - {
                    (str(item["method"]), str(item["path"])) for item in report["probes"]
                }
                if missing:
                    raise RuntimeError("ACL_REVOCATION_EXPOSURE_ROUTE_NOT_INVOKED")
                failures = [item for item in report["probes"] if not item["passed"]]
                if failures:
                    raise RuntimeError("ACL_REVOCATION_CONTENT_LEAK")
            finally:
                viewer_client.close()
        report["passed"] = True
    except OperationalGateBlocked as exc:
        exit_code = 2
        report["blocked"] = {"code": str(exc)}
    except Exception as exc:
        exit_code = 1
        report["error"] = _safe_cli_failure(exc, stage="provider_acl_revocation")
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_operational_gate_report(report_dir, "provider-acl-revocation", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


async def _seed_operational_tenant_membership(*, tenant_id: str, user_id: str, database_url: str) -> None:
    """Grant an existing test administrator membership in a generated tenant only."""
    from sqlalchemy import text

    from wikipediarag.config import get_settings
    from wikipediarag.db import connect

    settings = get_settings().model_copy(update={"database_url": database_url})
    async with connect(settings) as conn:
        await conn.execute(
            text(
                """
                INSERT INTO tenant_memberships(tenant_id, user_id, role)
                VALUES (:tenant_id, :user_id, 'TENANT_ADMIN')
                ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role
                """
            ),
            {"tenant_id": tenant_id, "user_id": user_id},
        )


def _upload_operational_acl_fixture(
    client: httpx.Client, api: str, knowledge_base_id: str, marker: str
) -> dict[str, str]:
    content = marker.encode("utf-8")
    checksum = hashlib.sha256(content).hexdigest()
    session_response = client.post(
        f"{api}/api/v1/uploads/sessions",
        json={
            "filename": "acl-revocation.txt",
            "content_type": "text/plain",
            "size_bytes": len(content),
            "checksum_sha256": checksum,
            "knowledge_base_id": knowledge_base_id,
            "parser_profile": "standard",
            "metadata": {},
        },
        timeout=30,
    )
    session_response.raise_for_status()
    session = session_response.json()
    if not isinstance(session, dict):
        raise RuntimeError("ACL_REVOCATION_UPLOAD_SESSION_INVALID")
    upload_response = httpx.put(
        str(session["upload_url"]), content=content, headers=dict(session.get("required_headers") or {}), timeout=120
    )
    upload_response.raise_for_status()
    completion = client.post(
        f"{api}/api/v1/uploads/sessions/{session['upload_session_id']}:complete", json={"metadata": {}}, timeout=30
    )
    completion.raise_for_status()
    payload = completion.json()
    if not isinstance(payload, dict):
        raise RuntimeError("ACL_REVOCATION_UPLOAD_COMPLETION_INVALID")
    required = {name: str(payload.get(name) or "") for name in ("document_id", "job_id")}
    if not all(required.values()):
        raise RuntimeError("ACL_REVOCATION_UPLOAD_IDENTIFIERS_MISSING")
    return required


def _post_revocation_exposure_requests(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    document_id: str,
    research_run_id: str,
    marker: str,
    query_run_id: str = "",
) -> list[tuple[tuple[str, str], httpx.Response]]:
    """Invoke every metadata-tagged current-content route after an ACL revoke."""
    requests: list[tuple[tuple[str, str], httpx.Response]] = []

    def add(method: str, template: str, *, actual: str | None = None, **kwargs: Any) -> None:
        requests.append(
            ((method, template), client.request(method, f"{api}{actual or template}", timeout=180, **kwargs))
        )

    add("GET", "/api/v1/documents/{document_id}", actual=f"/api/v1/documents/{document_id}")
    add("GET", "/api/v1/documents/{document_id}/versions", actual=f"/api/v1/documents/{document_id}/versions")
    add("GET", "/api/v1/documents/{document_id}/structure", actual=f"/api/v1/documents/{document_id}/structure")
    add("GET", "/api/v1/documents/{document_id}/context", actual=f"/api/v1/documents/{document_id}/context")
    add(
        "POST",
        "/api/v1/documents/{document_id}/search",
        actual=f"/api/v1/documents/{document_id}/search",
        json={"query": marker},
    )
    add(
        "POST",
        "/api/v1/search",
        json={"query": marker, "knowledge_base_ids": [knowledge_base_id], "ranking_profile": "upload_sota_mvp"},
    )
    add(
        "POST",
        "/api/v1/search:debug",
        json={"message": marker, "knowledge_base_ids": [knowledge_base_id], "retrieval_profile": "upload_sota_mvp"},
    )
    add(
        "POST",
        "/api/v1/chat",
        json={
            "message": marker,
            "knowledge_base_ids": [knowledge_base_id],
            "retrieval_profile": "upload_sota_mvp",
            "stream": True,
        },
    )
    if query_run_id:
        add("GET", "/api/v1/query-runs/{query_run_id}/retrieval", actual=f"/api/v1/query-runs/{query_run_id}/retrieval")
    add("GET", "/api/v1/research-runs/{research_run_id}", actual=f"/api/v1/research-runs/{research_run_id}")
    add(
        "GET",
        "/api/v1/research-runs/{research_run_id}/events",
        actual=f"/api/v1/research-runs/{research_run_id}/events",
    )
    return requests


def _verify_live_openapi_authorization_contracts(schema: dict[str, Any], routes: list[Any]) -> None:
    from wikipediarag.api.route_contracts import OPENAPI_AUTHORIZATION_EXTENSION

    paths: dict[str, Any] = dict(schema.get("paths") or {}) if isinstance(schema.get("paths"), dict) else {}
    missing: list[str] = []
    for route in routes:
        operations = paths.get(route.path)
        operation = operations.get(route.method.lower()) if isinstance(operations, dict) else None
        if not isinstance(operation, dict) or OPENAPI_AUTHORIZATION_EXTENSION not in operation:
            missing.append(f"{route.method} {route.path}")
    if missing:
        raise RuntimeError("LIVE_OPENAPI_AUTHORIZATION_CONTRACT_MISSING")


def _write_operational_gate_report(report_dir: Path, name: str, report: dict[str, Any]) -> None:
    (report_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = 0 if report.get("passed") else 1
    failure = "" if not failures else '<failure type="OperationalGateFailed"/>'
    (report_dir / "junit.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{escape(name)}" tests="1" failures="{failures}">'
        f'<testcase classname="wikipediarag.cli" name="{escape(name)}">{failure}</testcase></testsuite>\n',
        encoding="utf-8",
    )


def verify_deep_research_smoke(args: argparse.Namespace) -> None:
    from wikipediarag.deep_research_eval import (
        DEFAULT_DEEP_RESEARCH_POLICY_ID,
        build_context_experiment_report,
        load_deep_research_fixtures,
    )

    api = str(args.api).rstrip("/")
    admin_secret = _resolve_hardening_admin_secret(args)
    fixture_path = Path(str(args.fixture_path))
    command_name = str(getattr(args, "command", "deep-research-smoke"))
    report_root = (
        Path("artifacts/validation/deep-research-hard-gate")
        if command_name == "deep-research-hard-gate"
        else Path("artifacts/validation/deep-research")
    )
    report_dir = report_root / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report_dir.mkdir(parents=True, exist_ok=True)
    fixtures = load_deep_research_fixtures(fixture_path)
    context_policy_override = _deep_research_context_policy_override(args)
    compose_model_provider = str(getattr(args, "compose_model_provider", "mock"))
    is_hard_gate = command_name == "deep-research-hard-gate"
    if args.task_id:
        requested = set(str(task_id) for task_id in args.task_id)
        fixtures = [fixture for fixture in fixtures if fixture.task_id in requested]
        missing = sorted(requested - {fixture.task_id for fixture in fixtures})
        if missing:
            raise RuntimeError(f"unknown Deep Research fixture task-id(s): {missing}")
    if args.max_tasks is not None:
        fixtures = fixtures[: max(0, int(args.max_tasks))]
    report: dict[str, Any] = {
        "passed": False,
        "command": command_name,
        "api": api,
        "fixture_path": str(fixture_path),
        "retrieval_profile": str(args.retrieval_profile),
        "tool_mode": str(getattr(args, "tool_mode", DEFAULT_RESEARCH_TOOL_MODE)),
        "compose_model_provider": compose_model_provider,
        "declared_context_tokens": int(args.declared_context_tokens),
        "context_policy_override": context_policy_override or {},
        "runtime": {"mode": "pending"},
        "report_dir": str(report_dir),
        "checks": [],
        "items": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    exit_code = 0
    runtime: DeepResearchRuntime | None = None
    try:
        if not fixtures:
            raise RuntimeError("no Deep Research fixtures selected")
        if not admin_secret:
            raise RuntimeError("deep-research-smoke requires a non-empty admin secret")
        if is_hard_gate and not args.skip_compose:
            runtime = _compose_up_isolated_deep_research_hard_gate(
                model_provider=compose_model_provider,
                retrieval_profile=str(args.retrieval_profile),
            )
            api = runtime.api
            report["api"] = api
            report["runtime"] = runtime.public_details()
            _record_check(
                report,
                "compose_up_isolated",
                True,
                {
                    "model_provider": compose_model_provider,
                    "retrieval_profile": str(args.retrieval_profile),
                    **runtime.public_details(),
                },
            )
        elif not args.skip_compose:
            compose_details = _compose_up_deep_research_stack(
                model_provider=compose_model_provider,
                retrieval_profile=str(args.retrieval_profile),
            )
            runtime = DeepResearchRuntime(api=api)
            report["runtime"] = runtime.public_details()
            _record_check(
                report,
                "compose_up",
                True,
                {
                    "model_provider": compose_model_provider,
                    "retrieval_profile": str(args.retrieval_profile),
                    **compose_details,
                },
            )
        else:
            runtime = DeepResearchRuntime(api=api)
            report["runtime"] = runtime.public_details()
        hard_gate_deadline = time.monotonic() + int(args.timeout_seconds) if is_hard_gate else None
        with httpx.Client(timeout=180, follow_redirects=True) as admin_client:
            _wait_json_ready(admin_client, f"{api}/ready", "api", require_ok=True)
            _record_check(report, "api_ready", True)
            _authenticate_smoke_session(
                admin_client,
                api,
                username=str(args.admin_username),
                admin_secret=admin_secret,
            )
            _record_check(report, "platform_admin_login", True)
            admin_session = _get_json(admin_client, f"{api}/api/v1/auth/session")
            _update_csrf_from_session(admin_client, admin_session)
            tenant_id = str(admin_session.get("active_tenant_id") or "")
            if not tenant_id:
                raise RuntimeError(f"admin session has no active tenant: {admin_session}")
            viewer_credentials: dict[str, str] | None = None
            if any(str(fixture.acl_setup.get("mode") or "") == "mixed_visibility" for fixture in fixtures):
                viewer_credentials = asyncio.run(
                    _seed_deep_research_viewer_user(
                        tenant_id=tenant_id,
                        database_url=runtime.database_url if runtime is not None else None,
                    )
                )
                _record_check(report, "viewer_user_seeded", True, {"user_id": viewer_credentials["user_id"]})
            for index, fixture in enumerate(fixtures, start=1):
                item = _run_deep_research_fixture(
                    admin_client,
                    api,
                    fixture,
                    retrieval_profile=str(args.retrieval_profile),
                    tool_mode=str(getattr(args, "tool_mode", DEFAULT_RESEARCH_TOOL_MODE)),
                    declared_context_tokens=int(args.declared_context_tokens),
                    timeout_seconds=int(args.timeout_seconds),
                    viewer_credentials=viewer_credentials,
                    context_policy_override=context_policy_override,
                    artifact_dir=report_dir,
                    deadline_monotonic=hard_gate_deadline,
                )
                item["policy_id"] = DEFAULT_DEEP_RESEARCH_POLICY_ID
                _append_report_item(report, item)
                print(
                    json.dumps(
                        {
                            "deep_research_task": fixture.task_id,
                            "tool_mode": str(getattr(args, "tool_mode", DEFAULT_RESEARCH_TOOL_MODE)),
                            "processed": index,
                            "total": len(fixtures),
                            "passed": item.get("passed"),
                            "run_status": item.get("metrics", {}).get("run_status") if isinstance(item, dict) else None,
                        },
                        ensure_ascii=False,
                    )
                )
            evaluations = [item for item in report["items"] if isinstance(item, dict)]
            report["context_experiments"] = build_context_experiment_report(
                [
                    {
                        "policy_id": str(item.get("policy_id") or DEFAULT_DEEP_RESEARCH_POLICY_ID),
                        "passed": bool(item.get("passed")),
                        "metrics": item.get("metrics") if isinstance(item.get("metrics"), dict) else {},
                    }
                    for item in evaluations
                ]
            )
            failures = [item for item in evaluations if not item.get("passed")]
            if failures:
                raise RuntimeError(f"deep research smoke failed for {len(failures)} fixture(s)")
        report["passed"] = True
    except Exception as exc:
        exit_code = 1
        report["error"] = _safe_cli_failure(exc, stage="deep_research_smoke")
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_deep_research_reports(report_dir, report)
        if runtime is not None and runtime.isolated and args.down_after:
            _compose_down_isolated_deep_research_hard_gate(runtime)
        elif args.down_after:
            subprocess.run([_docker_executable(), "compose", "down"], check=False)  # noqa: S603
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def verify_deep_research_matrix(args: argparse.Namespace) -> None:
    from wikipediarag.deep_research_eval import (
        build_context_experiment_report,
        load_deep_research_fixtures,
        run_context_policy_experiment_rows,
    )

    fixture_path = Path(str(args.fixture_path))
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report_dir = (
        Path(str(args.output_dir)) if args.output_dir else Path("artifacts/validation/deep-research-matrix") / timestamp
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    fixtures = load_deep_research_fixtures(fixture_path)
    if args.task_id:
        requested = set(str(task_id) for task_id in args.task_id)
        fixtures = [fixture for fixture in fixtures if fixture.task_id in requested]
        missing = sorted(requested - {fixture.task_id for fixture in fixtures})
        if missing:
            raise RuntimeError(f"unknown Deep Research fixture task-id(s): {missing}")
    if args.max_tasks is not None:
        fixtures = fixtures[: max(0, int(args.max_tasks))]
    if not fixtures:
        raise RuntimeError("no Deep Research fixtures selected")
    rows = run_context_policy_experiment_rows(
        fixtures,
        declared_context_tokens=int(args.declared_context_tokens),
    )
    experiment_report = build_context_experiment_report(rows)
    policy_results = experiment_report.get("policy_results")
    report: dict[str, Any] = {
        "passed": True,
        "schema_version": "deep_research_matrix_report_v1",
        "fixture_path": str(fixture_path),
        "fixture_count": len(fixtures),
        "declared_context_tokens": int(args.declared_context_tokens),
        "report_dir": str(report_dir),
        "started_at": timestamp,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "items": policy_results if isinstance(policy_results, list) else [],
        "context_experiments": experiment_report,
        "notes": [
            "Matrix mode is an offline context-packer experiment over synthetic fixture records.",
            "It exercises all 27 target/packing/reflection policies without requiring Qwen or provider calls.",
            "Runtime Deep Research accepts API/CLI context_policy_override for productive, soft and hard limits.",
        ],
    }
    _write_deep_research_matrix_reports(report_dir, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def verify_deep_research_tool_matrix(args: argparse.Namespace) -> None:
    from wikipediarag.deep_research_eval import (
        build_runtime_tool_matrix_report,
        load_deep_research_fixtures,
        runtime_tool_matrix_modes,
    )

    api = str(args.api).rstrip("/")
    admin_secret = _resolve_hardening_admin_secret(args)
    fixture_path = Path(str(args.fixture_path))
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report_dir = (
        Path(str(args.output_dir))
        if args.output_dir
        else Path("artifacts/validation/deep-research-tool-matrix") / timestamp
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    fixtures = load_deep_research_fixtures(fixture_path)
    if args.task_id:
        requested = set(str(task_id) for task_id in args.task_id)
        fixtures = [fixture for fixture in fixtures if fixture.task_id in requested]
        missing = sorted(requested - {fixture.task_id for fixture in fixtures})
        if missing:
            raise RuntimeError(f"unknown Deep Research fixture task-id(s): {missing}")
    if args.max_tasks is not None:
        fixtures = fixtures[: max(0, int(args.max_tasks))]
    if not fixtures:
        raise RuntimeError("no Deep Research fixtures selected")
    context_policy_override = _deep_research_context_policy_override(args)
    compose_model_provider = str(getattr(args, "compose_model_provider", "openrouter"))
    tool_modes = runtime_tool_matrix_modes()
    report: dict[str, Any] = {
        "passed": False,
        "command": "deep-research-tool-matrix",
        "api": api,
        "fixture_path": str(fixture_path),
        "retrieval_profile": str(args.retrieval_profile),
        "compose_model_provider": compose_model_provider,
        "declared_context_tokens": int(args.declared_context_tokens),
        "context_policy_override": context_policy_override or {},
        "tool_modes": tool_modes,
        "gating_policy_id": DEFAULT_RESEARCH_TOOL_MODE,
        "runtime": {"mode": "pending"},
        "report_dir": str(report_dir),
        "items": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    exit_code = 0
    runtime: DeepResearchRuntime | None = None
    try:
        if not admin_secret:
            raise RuntimeError("deep-research-tool-matrix requires a non-empty admin secret")
        if not args.skip_compose:
            runtime = _compose_up_isolated_deep_research_hard_gate(
                model_provider=compose_model_provider,
                retrieval_profile=str(args.retrieval_profile),
            )
            api = runtime.api
            report["api"] = api
            report["runtime"] = runtime.public_details()
        else:
            runtime = DeepResearchRuntime(api=api)
            report["runtime"] = runtime.public_details()
        with httpx.Client(timeout=180, follow_redirects=True) as admin_client:
            _wait_json_ready(admin_client, f"{api}/ready", "api", require_ok=True)
            _authenticate_smoke_session(
                admin_client,
                api,
                username=str(args.admin_username),
                admin_secret=admin_secret,
            )
            admin_session = _get_json(admin_client, f"{api}/api/v1/auth/session")
            _update_csrf_from_session(admin_client, admin_session)
            tenant_id = str(admin_session.get("active_tenant_id") or "")
            if not tenant_id:
                raise RuntimeError(f"admin session has no active tenant: {admin_session}")
            viewer_credentials: dict[str, str] | None = None
            if any(str(fixture.acl_setup.get("mode") or "") == "mixed_visibility" for fixture in fixtures):
                viewer_credentials = asyncio.run(
                    _seed_deep_research_viewer_user(
                        tenant_id=tenant_id,
                        database_url=runtime.database_url if runtime is not None else None,
                    )
                )
            for mode_index, tool_mode in enumerate(tool_modes, start=1):
                mode_artifact_dir = report_dir / tool_mode
                mode_artifact_dir.mkdir(parents=True, exist_ok=True)
                for fixture_index, fixture in enumerate(fixtures, start=1):
                    item = _run_deep_research_fixture(
                        admin_client,
                        api,
                        fixture,
                        retrieval_profile=str(args.retrieval_profile),
                        tool_mode=tool_mode,
                        declared_context_tokens=int(args.declared_context_tokens),
                        timeout_seconds=int(args.timeout_seconds),
                        viewer_credentials=viewer_credentials,
                        context_policy_override=context_policy_override,
                        artifact_dir=mode_artifact_dir,
                    )
                    item["policy_id"] = tool_mode
                    item["tool_mode"] = tool_mode
                    report["items"].append(item)
                    _write_partial_deep_research_report(report_dir, report)
                    print(
                        json.dumps(
                            {
                                "tool_mode": tool_mode,
                                "mode_index": mode_index,
                                "mode_total": len(tool_modes),
                                "deep_research_task": fixture.task_id,
                                "processed": fixture_index,
                                "total": len(fixtures),
                                "passed": item.get("passed"),
                                "run_status": item.get("metrics", {}).get("run_status")
                                if isinstance(item, dict)
                                else None,
                            },
                            ensure_ascii=False,
                        )
                    )
            experiment_rows: list[dict[str, Any]] = [
                {
                    "policy_id": str(item.get("policy_id") or DEFAULT_RESEARCH_TOOL_MODE),
                    "passed": bool(item.get("passed")),
                    "metrics": item.get("metrics") if isinstance(item.get("metrics"), dict) else {},
                    "fixture_task_id": str(item.get("task_id") or ""),
                    "experiment_mode": "runtime_tool_matrix",
                }
                for item in report["items"]
                if isinstance(item, dict)
            ]
            report["tool_matrix"] = build_runtime_tool_matrix_report(experiment_rows)
            policy_results = report["tool_matrix"].get("policy_results")
            default_policy = next(
                (
                    item
                    for item in policy_results
                    if isinstance(item, dict) and item.get("policy_id") == DEFAULT_RESEARCH_TOOL_MODE
                ),
                None,
            )
            if isinstance(default_policy, dict):
                report["passed"] = bool(default_policy.get("passed"))
            else:
                report["passed"] = False
            if not report["passed"]:
                exit_code = 1
    except Exception as exc:
        exit_code = 1
        report["error"] = _safe_cli_failure(exc, stage="deep_research_matrix")
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_deep_research_matrix_reports(report_dir, report)
        if runtime is not None and runtime.isolated:
            _compose_down_isolated_deep_research_hard_gate(runtime)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def _resolve_hardening_admin_secret(args: argparse.Namespace) -> str:
    raw_path = getattr(args, "admin_secret_file", None)
    if raw_path:
        return Path(str(raw_path)).read_text(encoding="utf-8").strip()
    return str(getattr(args, "admin_secret", "admin") or "admin")


def _deep_research_context_policy_override(args: argparse.Namespace) -> dict[str, float] | None:
    payload: dict[str, float] = {}
    for arg_name, field_name in (
        ("context_productive_target", "productive_target"),
        ("context_soft_limit", "soft_limit"),
        ("context_hard_input_limit", "hard_input_limit"),
    ):
        value = getattr(args, arg_name, None)
        if value is not None:
            payload[field_name] = float(value)
    return payload or None


def _run_deep_research_fixture(
    admin_client: httpx.Client,
    api: str,
    fixture: Any,
    *,
    retrieval_profile: str,
    tool_mode: str,
    declared_context_tokens: int,
    timeout_seconds: int,
    viewer_credentials: dict[str, str] | None,
    context_policy_override: dict[str, float] | None,
    artifact_dir: Path | None = None,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    from wikipediarag.deep_research_eval import evaluate_research_detail

    started = time.monotonic()
    result: dict[str, Any] = {
        "task_id": fixture.task_id,
        "quality_tags": list(fixture.quality_tags),
        "tool_mode": tool_mode,
        "passed": False,
        "uploads": [],
        "actions": [],
    }
    uploaded_document_ids: dict[str, str] = {}
    try:
        _remaining_deep_research_timeout(
            deadline_monotonic=deadline_monotonic,
            fallback_timeout_seconds=timeout_seconds,
        )
        knowledge_base_id = _create_deep_research_knowledge_base(admin_client, api, str(fixture.task_id))
        result["knowledge_base_id"] = knowledge_base_id
        viewer_client: httpx.Client | None = None
        runner_client = admin_client
        if str(fixture.acl_setup.get("mode") or "") == "mixed_visibility":
            if viewer_credentials is None:
                raise RuntimeError("ACL fixture requires seeded viewer credentials")
            _grant_deep_research_viewer(admin_client, api, knowledge_base_id, viewer_credentials["user_id"])
            viewer_client = httpx.Client(timeout=180, follow_redirects=True)
            _login_deep_research_viewer(
                viewer_client,
                api,
                viewer_credentials["username"],
                viewer_credentials["password"],
            )
            runner_client = viewer_client
            result["viewer_user_id"] = viewer_credentials["user_id"]
        try:
            for document in fixture.documents:
                upload = _upload_deep_research_fixture_document(
                    admin_client,
                    api,
                    knowledge_base_id,
                    fixture,
                    document,
                    deadline_monotonic=deadline_monotonic,
                )
                result["uploads"].append(upload)
                uploaded_document_ids[str(upload["fixture_document_id"])] = str(upload["document_id"])
                if document.access is not None:
                    _replace_deep_research_document_grants(
                        admin_client,
                        api,
                        str(upload["document_id"]),
                        dict(document.access),
                    )
            if bool(fixture.acl_setup.get("exercise_actions")):
                action_detail = _exercise_deep_research_actions(
                    runner_client,
                    api,
                    knowledge_base_id,
                    str(fixture.topic),
                    retrieval_profile=retrieval_profile,
                    tool_mode=tool_mode,
                    timeout_seconds=timeout_seconds,
                    context_policy_override=context_policy_override,
                    deadline_monotonic=deadline_monotonic,
                    run_deadline_seconds=_deep_research_run_deadline_seconds(
                        deadline_monotonic=deadline_monotonic,
                        fallback_timeout_seconds=timeout_seconds,
                    ),
                )
                result["actions"].extend(action_detail["actions"])
                detail = action_detail["detail"]
            else:
                run_id = _create_deep_research_run(
                    runner_client,
                    api,
                    knowledge_base_id,
                    str(fixture.topic),
                    retrieval_profile=retrieval_profile,
                    tool_mode=tool_mode,
                    context_policy_override=context_policy_override,
                    run_deadline_seconds=_deep_research_run_deadline_seconds(
                        deadline_monotonic=deadline_monotonic,
                        fallback_timeout_seconds=timeout_seconds,
                    ),
                )
                result["run_id"] = run_id
                detail = _wait_research_run_terminal(
                    runner_client,
                    api,
                    run_id,
                    timeout_seconds=timeout_seconds,
                    deadline_monotonic=deadline_monotonic,
                )
            if artifact_dir is not None:
                detail_artifact = artifact_dir / f"{fixture.task_id}-detail.json"
                detail_artifact.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
                result["detail_artifact"] = str(detail_artifact)
            evaluation = evaluate_research_detail(
                fixture,
                detail,
                declared_context_tokens=declared_context_tokens,
                document_id_aliases=uploaded_document_ids or None,
            )
            result.update(evaluation)
            result["latency_seconds"] = round(time.monotonic() - started, 3)
            metrics = result.get("metrics")
            if isinstance(metrics, dict):
                metrics["latency_seconds"] = result["latency_seconds"]
        finally:
            if viewer_client is not None:
                viewer_client.close()
    except Exception as exc:
        result["error"] = _safe_cli_failure(exc, stage="deep_research_fixture")
    return result


async def _seed_deep_research_viewer_user(*, tenant_id: str, database_url: str | None = None) -> dict[str, str]:
    from sqlalchemy import text

    from wikipediarag.auth_service import hash_password
    from wikipediarag.config import get_settings
    from wikipediarag.db import connect
    from wikipediarag.ids import new_uuid

    settings = get_settings()
    resolved_database_url = database_url or _host_reachable_database_url(settings.database_url)
    if resolved_database_url != settings.database_url:
        settings = settings.model_copy(update={"database_url": resolved_database_url})
    suffix = time.strftime("%Y%m%d%H%M%S", time.gmtime()) + uuid.uuid4().hex[:8]
    user_id = str(new_uuid())
    username = f"deep-research-viewer-{suffix}"
    password = f"DeepResearchSmoke-{suffix}-Password-123"
    email = f"{username}@example.test"
    async with connect(settings) as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users(
                  id, email, username, display_name, platform_role,
                  password_hash, password_change_required, is_disabled
                )
                VALUES (
                  :id, :email, :username, :display_name, 'USER',
                  :password_hash, false, false
                )
                """
            ),
            {
                "id": user_id,
                "email": email,
                "username": username,
                "display_name": username,
                "password_hash": hash_password(password),
            },
        )
        await conn.execute(
            text(
                """
                INSERT INTO tenant_memberships(tenant_id, user_id, role)
                VALUES (:tenant_id, :user_id, 'MEMBER')
                ON CONFLICT (tenant_id, user_id) DO UPDATE
                SET role = EXCLUDED.role
                """
            ),
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        await conn.execute(
            text(
                """
                INSERT INTO auth_identities(
                  id, user_id, issuer, subject, identity_key, provider_type, username, email
                )
                VALUES (
                  :id, :user_id, 'local', :subject, :identity_key, 'LOCAL', :username, :email
                )
                """
            ),
            {
                "id": str(new_uuid()),
                "user_id": user_id,
                "subject": username,
                "identity_key": f"local:{username}",
                "username": username,
                "email": email,
            },
        )
    return {"user_id": user_id, "username": username, "password": password}


def _host_reachable_database_url(database_url: str) -> str:
    return database_url.replace("@postgres:", "@localhost:").replace("@postgres/", "@localhost/")


def _create_deep_research_knowledge_base(client: httpx.Client, api: str, task_id: str) -> str:
    response = client.post(
        f"{api}/api/v1/knowledge-bases",
        json={"name": f"Deep Research Smoke {task_id} {time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("id"):
        raise RuntimeError(f"knowledge base creation returned invalid payload: {payload}")
    return str(payload["id"])


def _grant_deep_research_viewer(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    user_id: str,
) -> None:
    response = client.post(
        f"{api}/api/v1/knowledge-bases/{knowledge_base_id}/grants",
        json={"subject_type": "USER", "subject_id": user_id, "role": "VIEWER"},
        timeout=30,
    )
    response.raise_for_status()


def _login_deep_research_viewer(client: httpx.Client, api: str, username: str, password: str) -> None:
    response = client.post(
        f"{api}/api/v1/auth/local/login",
        json={"username": username, "password": password, "remember_me": False},
        timeout=30,
    )
    response.raise_for_status()
    session = _get_json(client, f"{api}/api/v1/auth/session")
    csrf_token = session.get("csrf_token")
    if not isinstance(csrf_token, str) or not csrf_token:
        raise RuntimeError(f"viewer session did not return a CSRF token: {session}")
    client.headers.update({"X-CSRF-Token": csrf_token})


def _update_csrf_from_session(client: httpx.Client, session: dict[str, Any]) -> None:
    csrf_token = session.get("csrf_token")
    if isinstance(csrf_token, str) and csrf_token:
        client.headers.update({"X-CSRF-Token": csrf_token})


def _upload_deep_research_fixture_document(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    fixture: Any,
    document: Any,
    *,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    content = str(document.content).encode("utf-8")
    checksum = hashlib.sha256(content).hexdigest()
    session_response = client.post(
        f"{api}/api/v1/uploads/sessions",
        json={
            "filename": str(document.filename),
            "content_type": str(document.content_type),
            "size_bytes": len(content),
            "checksum_sha256": checksum,
            "knowledge_base_id": knowledge_base_id,
            "parser_profile": str(document.parser_profile),
            "metadata": {
                "deep_research_fixture": True,
                "task_id": str(fixture.task_id),
                "fixture_document_id": str(document.id),
                **dict(document.metadata),
            },
        },
        timeout=_remaining_deep_research_timeout(
            deadline_monotonic=deadline_monotonic,
            fallback_timeout_seconds=30,
        ),
    )
    session_response.raise_for_status()
    session = session_response.json()
    upload_response = client.put(
        session["upload_url"],
        content=content,
        headers=session.get("required_headers") or {},
        timeout=_remaining_deep_research_timeout(
            deadline_monotonic=deadline_monotonic,
            fallback_timeout_seconds=120,
        ),
    )
    upload_response.raise_for_status()
    complete_response = client.post(
        f"{api}/api/v1/uploads/sessions/{session['upload_session_id']}:complete",
        json={"metadata": {"deep_research_fixture_completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}},
        timeout=_remaining_deep_research_timeout(
            deadline_monotonic=deadline_monotonic,
            fallback_timeout_seconds=30,
        ),
    )
    complete_response.raise_for_status()
    completed = complete_response.json()
    if not isinstance(completed, dict):
        raise RuntimeError("upload complete returned a non-object JSON payload")
    job_payload = _wait_job_terminal(
        client,
        api,
        str(completed["job_id"]),
        timeout_seconds=_remaining_deep_research_timeout(
            deadline_monotonic=deadline_monotonic,
            fallback_timeout_seconds=360,
        ),
        deadline_monotonic=deadline_monotonic,
    )
    if job_payload.get("status") != "completed":
        raise RuntimeError("Deep Research fixture upload job did not complete successfully")
    return {
        "fixture_document_id": str(document.id),
        "document_id": str(completed["document_id"]),
        "document_version_id": str(completed["document_version_id"]),
        "job_id": str(completed["job_id"]),
        "job_status": str(job_payload.get("status")),
    }


def _replace_deep_research_document_grants(
    client: httpx.Client,
    api: str,
    document_id: str,
    access: dict[str, Any],
) -> None:
    policy = str(access.get("policy") or "kb")
    if policy not in {"kb", "tenant", "restricted"}:
        raise RuntimeError("deep research fixture has an invalid legacy access policy")
    grants = [
        {"principal_type": "USER", "principal_id": str(user_id), "permission": "READ"}
        for user_id in list(access.get("user_ids") or [])
    ] + [
        {"principal_type": "GROUP", "principal_id": str(group_id), "permission": "READ"}
        for group_id in list(access.get("group_ids") or [])
    ]
    response = client.put(
        f"{api}/api/v1/documents/{document_id}/access-grants",
        json={"access_grants": grants, "inherits_kb_access": policy != "restricted"},
        timeout=60,
    )
    response.raise_for_status()


def _create_deep_research_run(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    topic: str,
    *,
    retrieval_profile: str,
    tool_mode: str,
    context_policy_override: dict[str, float] | None,
    run_deadline_seconds: int | None = None,
) -> str:
    retrieval_overrides: dict[str, Any] = {
        "retrieval": {"top_k": 12},
        "postprocess": {"extended_search": "always"},
    }
    if run_deadline_seconds is not None:
        retrieval_overrides["deep_research"] = {"deadline_seconds": int(run_deadline_seconds)}
    payload: dict[str, Any] = {
        "topic": topic,
        "knowledge_base_id": knowledge_base_id,
        "retrieval_profile": retrieval_profile,
        "tool_mode": tool_mode,
        "retrieval_overrides": retrieval_overrides,
    }
    if context_policy_override:
        payload["context_policy_override"] = context_policy_override
    response = client.post(
        f"{api}/api/v1/research-runs",
        json=payload,
        timeout=30,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = response.text[:1000]
        raise RuntimeError(f"deep research run creation failed: status={response.status_code} body={body}") from exc
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("run_id"):
        raise RuntimeError(f"research run creation returned invalid payload: {payload}")
    return str(payload["run_id"])


def _wait_research_run_terminal(
    client: httpx.Client,
    api: str,
    research_run_id: str,
    *,
    timeout_seconds: int,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = min(
        started + timeout_seconds,
        deadline_monotonic if deadline_monotonic is not None else float("inf"),
    )
    last_payload: dict[str, Any] = {}
    terminal = {"completed", "failed", "cancelled", "paused"}
    while time.monotonic() < deadline:
        payload = _get_json(client, f"{api}/api/v1/research-runs/{research_run_id}")
        last_payload = payload if isinstance(payload, dict) else {}
        run_payload = last_payload.get("run")
        run = run_payload if isinstance(run_payload, dict) else {}
        status = str(run.get("status") or "")
        if status in terminal:
            return last_payload
        print(
            json.dumps(
                {
                    "research_run_id": research_run_id,
                    "status": status,
                    "progress": run.get("progress") if isinstance(run, dict) else {},
                },
                ensure_ascii=False,
            )
        )
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    if deadline_monotonic is not None and deadline <= started + timeout_seconds:
        raise DeepResearchSuiteDeadlineExceededError("deep research hard gate deadline elapsed during run wait")
    raise DeepResearchRunTerminalTimeoutError("research run did not reach a terminal status before the gate deadline")


def _exercise_deep_research_actions(
    client: httpx.Client,
    api: str,
    knowledge_base_id: str,
    topic: str,
    *,
    retrieval_profile: str,
    tool_mode: str,
    timeout_seconds: int,
    context_policy_override: dict[str, float] | None,
    deadline_monotonic: float | None = None,
    run_deadline_seconds: int | None = None,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    pause_run_id = _create_deep_research_run(
        client,
        api,
        knowledge_base_id,
        topic,
        retrieval_profile=retrieval_profile,
        tool_mode=tool_mode,
        context_policy_override=context_policy_override,
        run_deadline_seconds=run_deadline_seconds,
    )
    pause_response = client.post(f"{api}/api/v1/research-runs/{pause_run_id}:pause", timeout=30)
    actions.append({"action": "pause", "status_code": pause_response.status_code})
    pause_response.raise_for_status()
    pause_detail = _wait_research_run_terminal(
        client,
        api,
        pause_run_id,
        timeout_seconds=_remaining_deep_research_timeout(
            deadline_monotonic=deadline_monotonic,
            fallback_timeout_seconds=timeout_seconds,
        ),
        deadline_monotonic=deadline_monotonic,
    )
    pause_status = str(_mapping_from_payload(pause_detail, "run").get("status") or "")
    if pause_status == "paused":
        resume_response = client.post(f"{api}/api/v1/research-runs/{pause_run_id}:resume", timeout=30)
        actions.append({"action": "resume", "status_code": resume_response.status_code})
        resume_response.raise_for_status()
        pause_detail = _wait_research_run_terminal(
            client,
            api,
            pause_run_id,
            timeout_seconds=_remaining_deep_research_timeout(
                deadline_monotonic=deadline_monotonic,
                fallback_timeout_seconds=timeout_seconds,
            ),
            deadline_monotonic=deadline_monotonic,
        )
    cancel_run_id = _create_deep_research_run(
        client,
        api,
        knowledge_base_id,
        topic,
        retrieval_profile=retrieval_profile,
        tool_mode=tool_mode,
        context_policy_override=context_policy_override,
        run_deadline_seconds=run_deadline_seconds,
    )
    cancel_response = client.post(f"{api}/api/v1/research-runs/{cancel_run_id}:cancel", timeout=30)
    actions.append({"action": "cancel", "status_code": cancel_response.status_code})
    cancel_response.raise_for_status()
    cancel_detail = _wait_research_run_terminal(
        client,
        api,
        cancel_run_id,
        timeout_seconds=_remaining_deep_research_timeout(
            deadline_monotonic=deadline_monotonic,
            fallback_timeout_seconds=timeout_seconds,
        ),
        deadline_monotonic=deadline_monotonic,
    )
    actions.append({"action": "cancel_terminal", "status": _mapping_from_payload(cancel_detail, "run").get("status")})
    return {"actions": actions, "detail": pause_detail}


def _remaining_deep_research_timeout(
    *,
    deadline_monotonic: float | None,
    fallback_timeout_seconds: int,
) -> int:
    if deadline_monotonic is None:
        return fallback_timeout_seconds
    remaining_seconds = deadline_monotonic - time.monotonic()
    if remaining_seconds <= 0:
        raise DeepResearchSuiteDeadlineExceededError("deep research hard gate deadline elapsed")
    return min(fallback_timeout_seconds, max(1, int(remaining_seconds) + 1))


def _deep_research_run_deadline_seconds(
    *,
    deadline_monotonic: float | None,
    fallback_timeout_seconds: int,
    reserve_seconds: int = DEEP_RESEARCH_GATE_RUN_RESERVE_SECONDS,
    minimum_seconds: int = DEEP_RESEARCH_GATE_MIN_RUN_SECONDS,
) -> int | None:
    """Bound a run by the remaining hard-gate budget, leaving report time."""
    if deadline_monotonic is None:
        return None
    remaining_seconds = deadline_monotonic - time.monotonic()
    if remaining_seconds <= reserve_seconds + minimum_seconds:
        raise DeepResearchSuiteDeadlineExceededError("insufficient hard-gate time for a new research run")
    return max(
        minimum_seconds,
        min(fallback_timeout_seconds, int(remaining_seconds - reserve_seconds)),
    )


def _mapping_from_payload(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _write_deep_research_reports(report_dir: Path, report: dict[str, Any]) -> None:
    report_path = report_dir / "report.json"
    junit_path = report_dir / "junit.xml"
    suite_name = str(report.get("command") or "deep-research-smoke")
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
        item_failures = item.get("failures") if isinstance(item.get("failures"), list) else []
        error = item.get("error") if isinstance(item.get("error"), dict) else None
        if item_failures or error:
            message = json.dumps({"failures": item_failures, "error": error}, ensure_ascii=False)
            failure_xml = f'<failure type="DeepResearchFixtureFailed">{escape(message)}</failure>'
        testcases.append(
            f'  <testcase classname="wikipediarag.cli" name="{escape(str(item.get("task_id") or "task"))}">'
            f"{failure_xml}</testcase>\n"
        )
    if not testcases and report.get("error"):
        error = report["error"] if isinstance(report["error"], dict) else {}
        testcases.append(
            f'  <testcase classname="wikipediarag.cli" name="{escape(suite_name)}">'
            f'<failure type="{escape(str(error.get("code") or "Error"))}">'
            f"{escape(str(error.get('message') or ''))}</failure></testcase>\n"
        )
    junit = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{escape(suite_name)}" tests="{len(testcases)}" failures="{failure_count}">\n'
        f"{''.join(testcases)}</testsuite>\n"
    )
    junit_path.write_text(junit, encoding="utf-8")


def _write_deep_research_matrix_reports(report_dir: Path, report: dict[str, Any]) -> None:
    report_path = report_dir / "report.json"
    junit_path = report_dir / "junit.xml"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raw_items = report.get("items")
    items: list[Any] = raw_items if isinstance(raw_items, list) else []
    gating_policy_id = str(report.get("gating_policy_id") or "")
    failure_items = [
        item
        for item in items
        if isinstance(item, dict)
        and not bool(item.get("passed", True))
        and (
            not gating_policy_id
            or str(item.get("policy_id") or "") == gating_policy_id
            or item.get("acl_safety") is False
        )
    ]
    testcases: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        failure_xml = ""
        if item in failure_items:
            message = json.dumps({"failures": item.get("failures")}, ensure_ascii=False)
            failure_xml = f'<failure type="DeepResearchMatrixAclFailure">{escape(message)}</failure>'
        testcases.append(
            f'  <testcase classname="wikipediarag.cli" name="{escape(str(item.get("policy_id") or "policy"))}">'
            f"{failure_xml}</testcase>\n"
        )
    junit = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{escape(str(report.get("command") or "deep-research-matrix"))}" '
        f'tests="{len(testcases)}" failures="{len(failure_items)}">\n'
        f"{''.join(testcases)}</testsuite>\n"
    )
    junit_path.write_text(junit, encoding="utf-8")


def _write_partial_deep_research_report(report_dir: Path, report: dict[str, Any]) -> None:
    partial_path = report_dir / "report.partial.json"
    partial_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _compose_up_deep_research_stack(
    *,
    model_provider: str = "mock",
    retrieval_profile: str = "upload_mock",
) -> dict[str, Any]:
    return _compose_up_document_upload_stack(model_provider=model_provider, retrieval_profile=retrieval_profile)


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
        result["error"] = _safe_cli_failure(exc, stage="document_corpus_item")
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


def _compose_up_document_upload_stack(
    *,
    model_provider: str = "mock",
    retrieval_profile: str = "test_mock",
) -> dict[str, Any]:
    env, openrouter_key_source = _deep_research_compose_environment(
        model_provider=model_provider,
        retrieval_profile=retrieval_profile,
    )
    subprocess.run(  # noqa: S603
        [
            _docker_executable(),
            "compose",
            "up",
            "-d",
            "--build",
            "--force-recreate",
            *DOCUMENT_UPLOAD_COMPOSE_SERVICES,
        ],
        check=True,
        env=env,
    )
    return {"openrouter_api_key_source": openrouter_key_source} if openrouter_key_source else {}


def _compose_up_isolated_deep_research_hard_gate(
    *,
    model_provider: str,
    retrieval_profile: str,
) -> DeepResearchRuntime:
    last_port_conflict = False
    attempts_made = 0
    for attempt in range(1, DEEP_RESEARCH_GATE_COMPOSE_START_ATTEMPTS + 1):
        attempts_made = attempt
        runtime = _new_isolated_deep_research_runtime(attempt)
        env, openrouter_key_source = _deep_research_compose_environment(
            model_provider=model_provider,
            retrieval_profile=retrieval_profile,
            runtime=runtime,
        )
        runtime = DeepResearchRuntime(
            api=runtime.api,
            database_url=runtime.database_url,
            compose_project=runtime.compose_project,
            api_port=runtime.api_port,
            minio_port=runtime.minio_port,
            attempt=runtime.attempt,
            openrouter_api_key_source=openrouter_key_source,
            isolated=True,
        )
        command = _isolated_deep_research_compose_command(runtime, "up", "-d", "--build", "--force-recreate")
        try:
            subprocess.run(  # noqa: S603
                [*command, *DEEP_RESEARCH_GATE_COMPOSE_SERVICES],
                check=True,
                env=env,
                stderr=subprocess.PIPE,
                text=True,
            )
            return runtime
        except subprocess.CalledProcessError as exc:
            _compose_down_isolated_deep_research_hard_gate(runtime)
            last_port_conflict = _is_compose_port_conflict(exc.stderr or "")
            if last_port_conflict and attempt < DEEP_RESEARCH_GATE_COMPOSE_START_ATTEMPTS:
                continue
            break
    reason = "port conflict" if last_port_conflict else "compose startup failure"
    raise DeepResearchGateInfrastructureError(
        f"isolated Deep Research hard-gate Compose startup failed after {attempts_made} attempt(s): {reason}"
    )


def _compose_up_isolated_reliability_smoke() -> DeepResearchRuntime:
    for attempt in range(1, DEEP_RESEARCH_GATE_COMPOSE_START_ATTEMPTS + 1):
        runtime = _new_isolated_deep_research_runtime(attempt)
        env, _ = _deep_research_compose_environment(
            model_provider="mock",
            retrieval_profile="upload_mock",
            runtime=runtime,
        )
        env.update(
            {
                "DOCUMENT_PARSER_SERVICES_REQUIRED": "true",
                "METADATA_SERVICE_URL": "http://metadata-service:8090",
                "MODEL_PROVIDER_TIMEOUT_SECONDS": "1",
                "MODEL_CLIENT_CHAT_TIMEOUT_SECONDS": "10",
                "MOCK_PROVIDER_CHAT_DELAY_SECONDS": "2",
                "MOCK_PROVIDER_CHAT_DELAY_REQUESTS": "3",
            }
        )
        command = _isolated_deep_research_compose_command(runtime, "up", "-d", "--build", "--force-recreate")
        try:
            subprocess.run(  # noqa: S603
                [*command, *DOCUMENT_UPLOAD_COMPOSE_SERVICES],
                check=True,
                env=env,
                stderr=subprocess.PIPE,
                text=True,
            )
            return runtime
        except subprocess.CalledProcessError as exc:
            _compose_down_isolated_deep_research_hard_gate(runtime)
            if not _is_compose_port_conflict(exc.stderr or "") or attempt == DEEP_RESEARCH_GATE_COMPOSE_START_ATTEMPTS:
                break
    raise DeepResearchGateInfrastructureError("isolated reliability Compose startup failed")


def _reliability_compose(runtime: DeepResearchRuntime, *arguments: str) -> None:
    database_port = runtime.database_url_port()
    if runtime.api_port is None or runtime.minio_port is None or database_port is None:
        raise DeepResearchGateInfrastructureError("isolated reliability runtime is incomplete")
    env = {
        **os.environ,
        "DEEP_RESEARCH_GATE_API_PORT": str(runtime.api_port),
        "DEEP_RESEARCH_GATE_MINIO_PORT": str(runtime.minio_port),
        "DEEP_RESEARCH_GATE_POSTGRES_PORT": str(database_port),
        "DOCUMENT_PARSER_SERVICES_REQUIRED": "true",
        "METADATA_SERVICE_URL": "http://metadata-service:8090",
        "MODEL_PROVIDER_TIMEOUT_SECONDS": "1",
        "MODEL_CLIENT_CHAT_TIMEOUT_SECONDS": "10",
        "MOCK_PROVIDER_CHAT_DELAY_SECONDS": "2",
        "MOCK_PROVIDER_CHAT_DELAY_REQUESTS": "3",
    }
    subprocess.run(  # noqa: S603
        _isolated_deep_research_compose_command(runtime, *arguments),
        check=True,
        env=env,
    )


def _new_isolated_deep_research_runtime(attempt: int) -> DeepResearchRuntime:
    postgres_port = _allocate_localhost_port()
    minio_port = _allocate_localhost_port(excluded={postgres_port})
    api_port = _allocate_localhost_port(excluded={postgres_port, minio_port})
    project = f"wikipediarag-dr-gate-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    return DeepResearchRuntime(
        api=f"http://127.0.0.1:{api_port}",
        database_url=(f"postgresql+asyncpg://rag:change-me-local-only@127.0.0.1:{postgres_port}/rag"),
        compose_project=project,
        api_port=api_port,
        minio_port=minio_port,
        attempt=attempt,
        isolated=True,
    )


def _allocate_localhost_port(*, excluded: set[int] | None = None) -> int:
    blocked = excluded or set()
    for _ in range(10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        if port not in blocked:
            return port
    raise DeepResearchGateInfrastructureError("could not allocate distinct localhost ports for isolated hard gate")


def _isolated_deep_research_compose_command(runtime: DeepResearchRuntime, *arguments: str) -> list[str]:
    if not runtime.compose_project:
        raise DeepResearchGateInfrastructureError("isolated hard gate is missing Compose project name")
    return [
        _docker_executable(),
        "compose",
        "--project-name",
        runtime.compose_project,
        "--file",
        "compose.yaml",
        "--file",
        str(DEEP_RESEARCH_GATE_COMPOSE_FILE),
        *arguments,
    ]


def _compose_down_isolated_deep_research_hard_gate(runtime: DeepResearchRuntime) -> None:
    if not runtime.isolated:
        return
    database_port = runtime.database_url_port()
    if runtime.api_port is None or runtime.minio_port is None or database_port is None:
        return
    env = {
        **os.environ,
        "DEEP_RESEARCH_GATE_API_PORT": str(runtime.api_port),
        "DEEP_RESEARCH_GATE_MINIO_PORT": str(runtime.minio_port),
        "DEEP_RESEARCH_GATE_POSTGRES_PORT": str(database_port),
    }
    subprocess.run(  # noqa: S603
        _isolated_deep_research_compose_command(runtime, "down", "--remove-orphans"),
        check=False,
        env=env,
    )


def _is_compose_port_conflict(output: str) -> bool:
    normalized = output.lower()
    return "address already in use" in normalized or "port is already allocated" in normalized


def _deep_research_compose_environment(
    *,
    model_provider: str,
    retrieval_profile: str,
    runtime: DeepResearchRuntime | None = None,
) -> tuple[dict[str, str], str]:
    if model_provider not in {"mock", "openrouter"}:
        raise RuntimeError(f"unsupported compose model provider: {model_provider}")
    openrouter_api_key = ""
    openrouter_key_source = ""
    if model_provider == "openrouter":
        openrouter_api_key, openrouter_key_source = _resolve_openrouter_api_key_for_compose()
        if not openrouter_api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is required for --compose-model-provider openrouter; "
                "set OPENROUTER_API_KEY, OPENROUTER_API_KEY_FILE, or OPENROUTER_API_KEY in .env"
            )
    env = {
        **os.environ,
        "MODEL_PROVIDER": model_provider,
        "MODEL_GATEWAY_STARTUP_SMOKE": "required"
        if model_provider == "openrouter"
        else os.environ.get("MODEL_GATEWAY_STARTUP_SMOKE", "warn"),
        "RETRIEVAL_PROFILE": retrieval_profile,
        "DOCUMENT_PARSER_SERVICES_REQUIRED": "true",
        "MINIO_PUBLIC_ENDPOINT": os.environ.get("MINIO_PUBLIC_ENDPOINT", "http://localhost:9000"),
        "API_PUBLIC_BASE_URL": os.environ.get("API_PUBLIC_BASE_URL", "http://localhost:8000"),
        "XBERG_URL": os.environ.get("XBERG_URL", "http://xberg:8000"),
        "DOCLING_URL": os.environ.get("DOCLING_URL", "http://docling:5001"),
        "METADATA_SERVICE_URL": os.environ.get("METADATA_SERVICE_URL", "http://metadata-service:8090"),
    }
    if openrouter_api_key:
        env["OPENROUTER_API_KEY"] = openrouter_api_key
    if model_provider == "openrouter":
        env["MODEL_PROVIDER_TIMEOUT_SECONDS"] = os.environ.get("MODEL_PROVIDER_TIMEOUT_SECONDS", "240")
        env["MODEL_CLIENT_CHAT_TIMEOUT_SECONDS"] = os.environ.get("MODEL_CLIENT_CHAT_TIMEOUT_SECONDS", "360")
        env["MODEL_CLIENT_EMBEDDING_TIMEOUT_SECONDS"] = os.environ.get("MODEL_CLIENT_EMBEDDING_TIMEOUT_SECONDS", "300")
        env["MODEL_CLIENT_RERANK_TIMEOUT_SECONDS"] = os.environ.get("MODEL_CLIENT_RERANK_TIMEOUT_SECONDS", "300")
    if runtime is not None:
        database_port = runtime.database_url_port()
        if not runtime.isolated or runtime.api_port is None or runtime.minio_port is None or database_port is None:
            raise DeepResearchGateInfrastructureError("isolated hard gate runtime is incomplete")
        env.update(
            {
                "DEEP_RESEARCH_GATE_API_PORT": str(runtime.api_port),
                "DEEP_RESEARCH_GATE_MINIO_PORT": str(runtime.minio_port),
                "DEEP_RESEARCH_GATE_POSTGRES_PORT": str(database_port),
                "POSTGRES_DB": "rag",
                "POSTGRES_USER": "rag",
                "POSTGRES_PASSWORD": "change-me-local-only",
                "DATABASE_URL": "postgresql+asyncpg://rag:change-me-local-only@postgres:5432/rag",
                "REDIS_URL": "redis://redis:6379/0",
                "MINIO_ENDPOINT": "http://minio:9000",
                "MINIO_PUBLIC_ENDPOINT": f"http://127.0.0.1:{runtime.minio_port}",
                "OPENSEARCH_URL": "http://opensearch:9200",
                "MODEL_GATEWAY_URL": "http://model-gateway:8080",
                "API_PUBLIC_BASE_URL": runtime.api,
                "XBERG_URL": "http://xberg:8000",
                "DOCLING_URL": "http://docling:5001",
                "METADATA_SERVICE_URL": "http://127.0.0.1:9",
                "DOCUMENT_PARSER_SERVICES_REQUIRED": "false",
            }
        )
    return env, openrouter_key_source


def _resolve_openrouter_api_key_for_compose() -> tuple[str, str]:
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key:
        return env_key, "env:OPENROUTER_API_KEY"
    from wikipediarag.config import Settings, resolve_openrouter_api_key

    settings = cast(Any, Settings)(_env_ignore_empty=True)
    key = resolve_openrouter_api_key(settings)
    if not key:
        return "", ""
    if settings.openrouter_api_key.strip():
        return key, "settings:OPENROUTER_API_KEY"
    if settings.openrouter_api_key_file is not None:
        return key, "settings:OPENROUTER_API_KEY_FILE"
    return key, "settings"


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


def _wait_job_terminal(
    client: httpx.Client,
    api: str,
    job_id: str,
    *,
    timeout_seconds: int = 360,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = min(
        started + timeout_seconds,
        deadline_monotonic if deadline_monotonic is not None else float("inf"),
    )
    last_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = _get_json(client, f"{api}/api/v1/ingestion-jobs/{job_id}")
        last_payload = payload if isinstance(payload, dict) else {}
        if last_payload.get("status") in {"completed", "failed", "cancelled"}:
            return last_payload
        progress = last_payload.get("progress") if isinstance(last_payload, dict) else {}
        safe_progress = {
            key: value
            for key in ("stage", "processed", "total", "completed", "failed", "last_update")
            if isinstance(progress, dict) and isinstance((value := progress.get(key)), (str, int, float, bool))
        }
        print(
            json.dumps(
                {"job_id": job_id, "status": last_payload.get("status"), "progress": safe_progress},
                ensure_ascii=False,
            )
        )
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    if deadline_monotonic is not None and deadline <= started + timeout_seconds:
        raise DeepResearchSuiteDeadlineExceededError("deep research hard gate deadline elapsed during job wait")
    raise RuntimeError("ingestion job did not reach a terminal status before its timeout")


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
        failure = _safe_cli_failure(exc, stage="cli_http")
        print(f"HTTP error: {failure['code']}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        failure = _safe_cli_failure(exc, stage="cli")
        print(failure["code"], file=sys.stderr)
        raise SystemExit(1) from None
