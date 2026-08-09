from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import cast

import httpx
import pytest

from wikipediarag import cli
from wikipediarag.cli import build_parser
from wikipediarag.deep_research_eval import (
    DEFAULT_DEEP_RESEARCH_POLICY_ID,
    _evidence_recall,
    build_context_experiment_report,
    build_runtime_tool_matrix_report,
    context_experiment_matrix,
    evaluate_research_detail,
    load_deep_research_fixtures,
    run_context_policy_experiment_rows,
)
from wikipediarag.research_tool_registry import DEFAULT_RESEARCH_TOOL_MODE

HARD_FIXTURE_PATH = "tests/fixtures/deep_research/research_tasks_hard.json"
TOOL_FIXTURE_PATH = "tests/fixtures/deep_research/research_tasks_tools.json"


def test_deep_research_fixture_manifest_has_complex_task_coverage() -> None:
    fixtures = load_deep_research_fixtures()
    by_id = {fixture.task_id: fixture for fixture in fixtures}

    assert len(fixtures) >= 10
    assert {
        "single_fact_grounded",
        "multi_hop_bridge",
        "comparison_matrix",
        "temporal_update",
        "conflicting_sources",
        "insufficient_evidence",
        "noisy_distractors",
        "long_context_pressure",
        "acl_mixed_visibility",
        "pause_resume_cancel",
    } <= set(by_id)
    assert by_id["conflicting_sources"].expected_coverage.requires_conflicting is True
    assert by_id["insufficient_evidence"].expected_coverage.requires_missing is True
    assert by_id["acl_mixed_visibility"].hidden_markers == ["DR_ACL_HIDDEN_MARKER"]

    for fixture in fixtures:
        documents = {document.id: document for document in fixture.documents}
        assert fixture.gold_evidence
        assert fixture.expected_questions
        for evidence in fixture.gold_evidence:
            assert evidence.marker in documents[evidence.document_id].content


def test_deep_research_hard_fixture_manifest_targets_tool_loop_gap() -> None:
    fixtures = load_deep_research_fixtures(HARD_FIXTURE_PATH)
    by_id = {fixture.task_id: fixture for fixture in fixtures}

    assert {
        "alias_reformulation_chain",
        "policy_exception_bridge",
        "contradiction_after_bridge",
        "finance_alias_chain",
    } <= set(by_id)
    for fixture in fixtures:
        assert "tool_loop_required" in fixture.quality_tags
        assert "query_reformulation" in fixture.quality_tags
        assert len(fixture.documents) >= 3
        assert len([item for item in fixture.gold_evidence if item.required]) >= 3
        assert fixture.expected_coverage.allow_partial is True
        assert fixture.trajectory_expectations.min_completed_tool_calls >= 3
        assert fixture.trajectory_expectations.min_derived_questions >= 3
        assert fixture.trajectory_expectations.required_tool_names == ["extended_search"]
        assert fixture.trajectory_expectations.require_tool_query_hash is True
        assert fixture.trajectory_expectations.forbid_raw_tool_query is True

        documents = {document.id: document for document in fixture.documents}
        for evidence in fixture.gold_evidence:
            assert evidence.marker in documents[evidence.document_id].content


def test_deep_research_tool_fixture_manifest_targets_document_tools() -> None:
    fixtures = load_deep_research_fixtures(TOOL_FIXTURE_PATH)
    by_id = {fixture.task_id: fixture for fixture in fixtures}

    assert {
        "section_alias_owner_chain",
        "within_doc_exception_clause",
        "csv_budget_reconciliation",
        "metadata_effective_policy",
        "mixed_tool_contradiction",
        "acl_visible_handle_only",
    } <= set(by_id)
    for fixture in fixtures:
        assert "tool_loop_required" in fixture.quality_tags
        assert fixture.trajectory_expectations.min_document_tool_calls >= 1
        assert len(fixture.trajectory_expectations.required_tool_names) >= 2
        documents = {document.id: document for document in fixture.documents}
        for evidence in fixture.gold_evidence:
            assert evidence.marker in documents[evidence.document_id].content


def test_deep_research_evaluator_scores_hard_tool_loop_trajectory() -> None:
    fixture = next(
        item for item in load_deep_research_fixtures(HARD_FIXTURE_PATH) if item.task_id == "alias_reformulation_chain"
    )
    detail = {
        "run": {"status": "completed"},
        "questions": [
            {"id": "q1", "kind": "primary", "question": "Project Lantern incident readiness"},
            {"id": "q2", "kind": "derived", "question": "What is LTN-42?"},
            {"id": "q3", "kind": "derived", "question": "Which service uses runbook RB-17?"},
            {"id": "q4", "kind": "derived", "question": "Who owns Night Harbor?"},
        ],
        "coverage": [
            {"id": "c1", "question_id": "q1", "status": "covered"},
            {"id": "c2", "question_id": "q2", "status": "covered"},
            {"id": "c3", "question_id": "q3", "status": "covered"},
            {"id": "c4", "question_id": "q4", "status": "covered"},
        ],
        "evidence": [
            {"id": "e1", "content_abstract": "DR_HARD_LANTERN_ALIAS Project Lantern maps to LTN-42."},
            {"id": "e2", "content_abstract": "DR_HARD_LANTERN_RUNBOOK RB-17 maps to Night Harbor."},
            {"id": "e3", "content_abstract": "DR_HARD_LANTERN_OWNER Night Harbor is owned by Borealis."},
        ],
        "claims": [{"id": "claim1", "claim_text": "Borealis owns Night Harbor.", "evidence_ids": ["e3"]}],
        "episodes": [
            {"id": "episode1", "episode_index": 1, "context_summary": {"token_estimate": 900}},
            {"id": "episode2", "episode_index": 2, "context_summary": {"token_estimate": 1000}},
            {"id": "episode3", "episode_index": 3, "context_summary": {"token_estimate": 1100}},
        ],
        "tool_calls": [
            {"id": "tc1", "tool_name": "extended_search", "tool_query_hash": "hash1", "status": "completed"},
            {"id": "tc2", "tool_name": "extended_search", "tool_query_hash": "hash2", "status": "completed"},
            {"id": "tc3", "tool_name": "extended_search", "tool_query_hash": "hash3", "status": "completed"},
        ],
        "final_report": {"markdown": "DR_HARD_LANTERN_ALIAS DR_HARD_LANTERN_RUNBOOK DR_HARD_LANTERN_OWNER"},
    }

    result = evaluate_research_detail(fixture, detail, declared_context_tokens=12000)

    assert result["passed"] is True
    assert result["metrics"]["evidence_recall"] == 1.0
    assert result["metrics"]["trajectory"]["completed_tool_call_count"] == 3
    assert result["metrics"]["trajectory"]["derived_question_count"] == 3
    assert result["metrics"]["trajectory"]["required_derived_terms_missing"] == []


def test_evidence_recall_accepts_fixture_to_persisted_document_id_aliases() -> None:
    fixture = next(
        item for item in load_deep_research_fixtures(HARD_FIXTURE_PATH) if item.task_id == "alias_reformulation_chain"
    )

    recall, missing = _evidence_recall(
        fixture,
        [
            {"document_id": "doc-1", "content_abstract": "DR_HARD_LANTERN_ALIAS"},
            {"document_id": "doc-2", "content_abstract": "DR_HARD_LANTERN_RUNBOOK"},
            {"document_id": "doc-3", "content_abstract": "DR_HARD_LANTERN_OWNER"},
        ],
        {},
        document_id_aliases={
            "lantern_summary": "doc-1",
            "runbook_index": "doc-2",
            "night_harbor_ops": "doc-3",
        },
    )

    assert recall == 1.0
    assert missing == []


def test_deep_research_evaluator_accepts_document_tool_trajectory() -> None:
    fixture = next(
        item for item in load_deep_research_fixtures(TOOL_FIXTURE_PATH) if item.task_id == "csv_budget_reconciliation"
    )
    detail = {
        "run": {"status": "completed", "tool_mode": "all_local_tools"},
        "questions": [
            {"id": "q1", "kind": "primary", "question": "Northwind Orchard budget"},
            {"id": "q2", "kind": "derived", "question": "What is NW-314?"},
            {"id": "q3", "kind": "derived", "question": "Find the budget row for NW-314."},
            {"id": "q4", "kind": "derived", "question": "Check partner rebates exclusions."},
        ],
        "coverage": [
            {"id": "c1", "question_id": "q1", "status": "covered"},
            {"id": "c2", "question_id": "q2", "status": "covered"},
            {"id": "c3", "question_id": "q3", "status": "covered"},
            {"id": "c4", "question_id": "q4", "status": "covered"},
        ],
        "evidence": [
            {"id": "e1", "content_abstract": "DR_TOOL_CSV_ALIAS Northwind Orchard maps to NW-314."},
            {"id": "e2", "content_abstract": "DR_TOOL_CSV_ROW NW-314 budget is 1250000 USD."},
            {"id": "e3", "content_abstract": "DR_TOOL_CSV_SCOPE NW-314 excludes partner rebates."},
        ],
        "claims": [{"id": "claim1", "claim_text": "Budget is 1250000 USD.", "evidence_ids": ["e2"]}],
        "episodes": [
            {"id": "episode1", "episode_index": 1, "context_summary": {"token_estimate": 1000}},
            {"id": "episode2", "episode_index": 2, "context_summary": {"token_estimate": 1100}},
            {"id": "episode3", "episode_index": 3, "context_summary": {"token_estimate": 1200}},
        ],
        "tool_calls": [
            {"id": "tc1", "tool_name": "extended_search", "tool_query_hash": "hash1", "status": "completed"},
            {"id": "tc2", "tool_name": "table_csv_lookup", "tool_query_hash": "hash2", "status": "completed"},
            {"id": "tc3", "tool_name": "extended_search", "tool_query_hash": "hash3", "status": "completed"},
        ],
        "final_report": {"markdown": "DR_TOOL_CSV_ALIAS DR_TOOL_CSV_ROW DR_TOOL_CSV_SCOPE"},
    }

    result = evaluate_research_detail(fixture, detail, declared_context_tokens=80000)

    assert result["passed"] is True
    assert result["metrics"]["trajectory"]["completed_document_tool_call_count"] == 1
    assert result["metrics"]["trajectory"]["required_tool_names_found"] == ["extended_search", "table_csv_lookup"]


def test_deep_research_evaluator_fails_public_raw_tool_query_leak() -> None:
    fixture = next(
        item for item in load_deep_research_fixtures(HARD_FIXTURE_PATH) if item.task_id == "finance_alias_chain"
    )
    detail = {
        "run": {"status": "completed"},
        "questions": [
            {"id": "q1", "kind": "primary", "question": "Omega Meridian budget"},
            {"id": "q2", "kind": "derived", "question": "Find CC-204 budget"},
            {"id": "q3", "kind": "derived", "question": "Confirm budget period"},
            {"id": "q4", "kind": "derived", "question": "Clarify scope exclusions"},
        ],
        "coverage": [
            {"id": "c1", "question_id": "q1", "status": "covered"},
            {"id": "c2", "question_id": "q2", "status": "covered"},
            {"id": "c3", "question_id": "q3", "status": "covered"},
            {"id": "c4", "question_id": "q4", "status": "covered"},
        ],
        "evidence": [
            {"id": "e1", "content_abstract": "DR_HARD_OMEGA_ALIAS Omega Meridian maps to CC-204."},
            {"id": "e2", "content_abstract": "DR_HARD_OMEGA_BUDGET CC-204 budget is 480000 EUR."},
            {"id": "e3", "content_abstract": "DR_HARD_OMEGA_SCOPE CC-204 scope excludes reserve."},
        ],
        "claims": [{"id": "claim1", "claim_text": "Budget is 480000 EUR.", "evidence_ids": ["e2"]}],
        "episodes": [{"id": "episode1", "episode_index": 1, "context_summary": {"token_estimate": 900}}],
        "tool_calls": [
            {
                "id": "tc1",
                "tool_name": "extended_search",
                "tool_query": "Omega Meridian CC-204 budget",
                "tool_query_hash": "hash1",
                "status": "completed",
            },
            {"id": "tc2", "tool_name": "extended_search", "tool_query_hash": "hash2", "status": "completed"},
            {"id": "tc3", "tool_name": "extended_search", "tool_query_hash": "hash3", "status": "completed"},
        ],
        "final_report": {"markdown": "DR_HARD_OMEGA_ALIAS DR_HARD_OMEGA_BUDGET DR_HARD_OMEGA_SCOPE"},
    }

    result = evaluate_research_detail(fixture, detail, declared_context_tokens=12000)

    assert result["passed"] is False
    assert "unsafe raw query/provider/storage key leaked in public research detail" in result["failures"]


def test_deep_research_evaluator_scores_supported_visible_detail() -> None:
    fixture = next(item for item in load_deep_research_fixtures() if item.task_id == "single_fact_grounded")
    detail = {
        "run": {"status": "completed"},
        "questions": [{"id": "q1", "question": "Когда запущен проект Alpha Beacon?"}],
        "coverage": [{"id": "c1", "question_id": "q1", "status": "covered"}],
        "evidence": [
            {
                "id": "e1",
                "evidence_ref": "E1",
                "title": "Alpha Beacon",
                "content_abstract": "DR_SINGLE_FACT_MARKER Проект Alpha Beacon запущен 2026-02-14.",
                "source_url": "https://example.test/alpha",
            }
        ],
        "claims": [{"id": "claim1", "claim_text": "Alpha Beacon запущен.", "evidence_ids": ["e1"]}],
        "episodes": [{"id": "episode1", "episode_index": 1, "context_summary": {"token_estimate": 1200}}],
        "final_report": {"markdown": "Отчет с DR_SINGLE_FACT_MARKER и ссылкой E1."},
    }

    result = evaluate_research_detail(fixture, detail, declared_context_tokens=12000)

    assert result["passed"] is True
    assert result["metrics"]["coverage_score"] == 1.0
    assert result["metrics"]["evidence_recall"] == 1.0
    assert result["metrics"]["unsupported_claim_count"] == 0
    assert result["metrics"]["context_efficiency"]["max_context_ratio"] == 0.1


def test_deep_research_evaluator_fails_hidden_marker_and_unsupported_claim() -> None:
    fixture = next(item for item in load_deep_research_fixtures() if item.task_id == "acl_mixed_visibility")
    detail = {
        "run": {"status": "completed"},
        "questions": [{"id": "q1", "question": "Какая часть проекта Iris видима обычному исследователю?"}],
        "coverage": [{"id": "c1", "question_id": "q1", "status": "covered"}],
        "evidence": [
            {
                "id": "e1",
                "content_abstract": "DR_ACL_VISIBLE_MARKER visible. DR_ACL_HIDDEN_MARKER hidden leak.",
            }
        ],
        "claims": [{"id": "claim1", "claim_text": "unsupported", "evidence_ids": ["missing-evidence"]}],
        "episodes": [
            {"id": "episode1", "episode_index": 1, "context_summary": {"token_estimate": 1200}},
            {"id": "episode2", "episode_index": 1, "context_summary": {"token_estimate": 1300}},
        ],
        "final_report": {"markdown": "DR_ACL_HIDDEN_MARKER"},
        "events": [{"payload": {"object_key": "s3://bucket/raw-provider-payload"}}],
    }

    result = evaluate_research_detail(fixture, detail, declared_context_tokens=12000)

    assert result["passed"] is False
    assert "hidden marker leaked: DR_ACL_HIDDEN_MARKER" in result["failures"]
    assert "claims without visible evidence: 1" in result["failures"]
    assert "duplicate episode indexes: ['1']" in result["failures"]
    assert "unsafe public token leaked: object_key" in result["failures"]
    assert "unsafe public token leaked: s3://" in result["failures"]


def test_deep_research_evaluator_counts_allowed_partial_toward_min_covered() -> None:
    fixture = next(item for item in load_deep_research_fixtures() if item.task_id == "noisy_distractors")
    detail = {
        "run": {"status": "completed"},
        "questions": [{"id": "q1", "question": "Найди владельца релиза Mercury-Prime."}],
        "coverage": [{"id": "c1", "question_id": "q1", "status": "partial"}],
        "evidence": [
            {
                "id": "e1",
                "evidence_ref": "E1",
                "title": "Mercury-Prime",
                "content_abstract": "DR_NOISY_RELEVANT Релиз Mercury-Prime принадлежит команде Helios.",
            }
        ],
        "claims": [{"id": "claim1", "claim_text": "Mercury-Prime belongs to Helios.", "evidence_ids": ["e1"]}],
        "episodes": [{"id": "episode1", "episode_index": 1, "context_summary": {"token_estimate": 600}}],
        "final_report": {"markdown": "DR_NOISY_RELEVANT"},
    }

    result = evaluate_research_detail(fixture, detail, declared_context_tokens=12000)

    assert result["passed"] is True
    assert result["metrics"]["coverage"]["partial"] == 1


def test_context_experiment_report_keeps_default_without_measurements() -> None:
    report = build_context_experiment_report([])

    assert report["recommended_policy_id"] == DEFAULT_DEEP_RESEARCH_POLICY_ID
    assert len(report["policies"]) == 27


def test_context_experiment_matrix_covers_budget_and_packing_sweep() -> None:
    policies = context_experiment_matrix()

    assert {policy.productive_target_ratio for policy in policies} == {0.35, 0.45, 0.55}
    assert {policy.packing_mode for policy in policies} == {"abstracts_only", "abstracts_top_raw", "raw_chunks"}
    assert {policy.reflection_mode for policy in policies} == {"none", "short_structured", "long_freeform"}
    assert len({policy.policy_id for policy in policies}) == 27


def test_context_policy_experiment_rows_cover_full_fixture_matrix() -> None:
    fixtures = load_deep_research_fixtures()
    rows = run_context_policy_experiment_rows(fixtures, declared_context_tokens=12000)
    report = build_context_experiment_report(rows)

    assert len(rows) == len(fixtures) * 27
    assert len(report["policy_results"]) == 27
    assert report["recommended_policy_id"] in {policy.policy_id for policy in context_experiment_matrix()}


def test_context_experiment_report_allows_safe_pareto_context_win() -> None:
    rows = [
        {
            "policy_id": DEFAULT_DEEP_RESEARCH_POLICY_ID,
            "passed": True,
            "metrics": {
                "coverage_score": 0.9,
                "evidence_recall": 0.9,
                "unsupported_claim_count": 0,
                "acl_safety": True,
                "context_efficiency": {"avg_context_ratio": 0.5, "max_context_ratio": 0.6},
            },
        },
        {
            "policy_id": "target_35_abstracts_only_short_structured",
            "passed": True,
            "metrics": {
                "coverage_score": 0.9,
                "evidence_recall": 0.9,
                "unsupported_claim_count": 0,
                "acl_safety": True,
                "context_efficiency": {"avg_context_ratio": 0.4, "max_context_ratio": 0.5},
            },
        },
    ]

    report = build_context_experiment_report(rows)

    assert report["recommended_policy_id"] == "target_35_abstracts_only_short_structured"


def test_deep_research_smoke_cli_is_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "deep-research-smoke",
            "--skip-compose",
            "--max-tasks",
            "1",
            "--context-productive-target",
            "0.35",
            "--context-soft-limit",
            "0.45",
            "--context-hard-input-limit",
            "0.60",
        ]
    )

    assert args.command == "deep-research-smoke"
    assert args.max_tasks == 1
    assert args.compose_model_provider == "mock"
    assert args.tool_mode == DEFAULT_RESEARCH_TOOL_MODE
    assert args.context_productive_target == 0.35
    assert args.context_soft_limit == 0.45
    assert args.context_hard_input_limit == 0.60


def test_deep_research_hard_gate_cli_defaults_are_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(["deep-research-hard-gate", "--skip-compose"])

    assert args.command == "deep-research-hard-gate"
    assert args.fixture_path == HARD_FIXTURE_PATH
    assert args.retrieval_profile == "upload_sota_mvp"
    assert args.compose_model_provider == "openrouter"
    assert args.tool_mode == DEFAULT_RESEARCH_TOOL_MODE
    assert args.timeout_seconds == 900


def test_isolated_hard_gate_runtime_uses_distinct_local_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = iter([15432, 19000, 18000, 15433, 19001, 18001])
    monkeypatch.setattr(cli, "_allocate_localhost_port", lambda *, excluded=None: next(ports))

    first = cli._new_isolated_deep_research_runtime(1)
    second = cli._new_isolated_deep_research_runtime(2)

    assert first.isolated is True
    assert first.api == "http://127.0.0.1:18000"
    assert first.database_url == "postgresql+asyncpg://rag:change-me-local-only@127.0.0.1:15432/rag"
    assert first.compose_project != second.compose_project
    public_details = first.public_details()
    assert public_details["database_port"] == 15432
    assert "database_url" not in public_details
    assert "change-me-local-only" not in str(public_details)


def test_isolated_hard_gate_environment_keeps_data_services_internal() -> None:
    runtime = cli.DeepResearchRuntime(
        api="http://127.0.0.1:18000",
        database_url="postgresql+asyncpg://rag:change-me-local-only@127.0.0.1:15432/rag",
        compose_project="wikipediarag-dr-gate-test",
        api_port=18000,
        minio_port=19000,
        attempt=1,
        isolated=True,
    )

    env, key_source = cli._deep_research_compose_environment(
        model_provider="mock",
        retrieval_profile="upload_sota_mvp",
        runtime=runtime,
    )

    assert key_source == ""
    assert env["DATABASE_URL"] == "postgresql+asyncpg://rag:change-me-local-only@postgres:5432/rag"
    assert env["MINIO_ENDPOINT"] == "http://minio:9000"
    assert env["MINIO_PUBLIC_ENDPOINT"] == "http://127.0.0.1:19000"
    assert env["OPENSEARCH_URL"] == "http://opensearch:9200"
    assert env["MODEL_GATEWAY_URL"] == "http://model-gateway:8080"
    assert env["API_PUBLIC_BASE_URL"] == "http://127.0.0.1:18000"
    assert env["DOCUMENT_PARSER_SERVICES_REQUIRED"] == "false"
    assert env["METADATA_SERVICE_URL"] == "http://127.0.0.1:9"
    assert {"docling", "xberg", "metadata-service"}.isdisjoint(cli.DEEP_RESEARCH_GATE_COMPOSE_SERVICES)


def test_isolated_hard_gate_compose_start_retries_only_port_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtimes = iter(
        [
            cli.DeepResearchRuntime(
                api="http://127.0.0.1:18000",
                database_url="postgresql+asyncpg://rag:change-me-local-only@127.0.0.1:15432/rag",
                compose_project="wikipediarag-dr-gate-one",
                api_port=18000,
                minio_port=19000,
                attempt=1,
                isolated=True,
            ),
            cli.DeepResearchRuntime(
                api="http://127.0.0.1:18001",
                database_url="postgresql+asyncpg://rag:change-me-local-only@127.0.0.1:15433/rag",
                compose_project="wikipediarag-dr-gate-two",
                api_port=18001,
                minio_port=19001,
                attempt=2,
                isolated=True,
            ),
        ]
    )
    attempts: list[list[str]] = []
    port_conflict_message = "Bind for 127.0.0.1 failed: port is already allocated"

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "up" in command:
            attempts.append(command)
            if len(attempts) == 1:
                raise subprocess.CalledProcessError(1, command, stderr=port_conflict_message)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli, "_new_isolated_deep_research_runtime", lambda _attempt: next(runtimes))
    monkeypatch.setattr("wikipediarag.cli.subprocess.run", fake_run)

    runtime = cli._compose_up_isolated_deep_research_hard_gate(
        model_provider="mock",
        retrieval_profile="upload_sota_mvp",
    )

    assert runtime.compose_project == "wikipediarag-dr-gate-two"
    assert len(attempts) == 2
    assert "--project-name" in attempts[0]
    assert "compose.deep-research-gate.yaml" in attempts[0]


def test_isolated_hard_gate_compose_start_does_not_retry_other_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = cli.DeepResearchRuntime(
        api="http://127.0.0.1:18000",
        database_url="postgresql+asyncpg://rag:change-me-local-only@127.0.0.1:15432/rag",
        compose_project="wikipediarag-dr-gate-failed",
        api_port=18000,
        minio_port=19000,
        attempt=1,
        isolated=True,
    )
    up_attempts = 0

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal up_attempts
        if "up" in command:
            up_attempts += 1
            raise subprocess.CalledProcessError(1, command, stderr="service image build failed")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli, "_new_isolated_deep_research_runtime", lambda _attempt: runtime)
    monkeypatch.setattr("wikipediarag.cli.subprocess.run", fake_run)

    with pytest.raises(cli.DeepResearchGateInfrastructureError, match="compose startup failure"):
        cli._compose_up_isolated_deep_research_hard_gate(
            model_provider="mock",
            retrieval_profile="upload_sota_mvp",
        )

    assert up_attempts == 1


def test_isolated_openrouter_hard_gate_never_reports_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = cli.DeepResearchRuntime(
        api="http://127.0.0.1:18000",
        database_url="postgresql+asyncpg://rag:change-me-local-only@127.0.0.1:15432/rag",
        compose_project="wikipediarag-dr-gate-openrouter",
        api_port=18000,
        minio_port=19000,
        attempt=1,
        isolated=True,
    )
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli, "_new_isolated_deep_research_runtime", lambda _attempt: runtime)
    monkeypatch.setattr(cli, "_resolve_openrouter_api_key_for_compose", lambda: ("test-openrouter-key", "test"))
    monkeypatch.setattr("wikipediarag.cli.subprocess.run", fake_run)

    result = cli._compose_up_isolated_deep_research_hard_gate(
        model_provider="openrouter",
        retrieval_profile="upload_sota_mvp",
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OPENROUTER_API_KEY"] == "test-openrouter-key"
    assert result.public_details()["openrouter_api_key_source"] == "test"
    assert "test-openrouter-key" not in str(result.public_details())


def test_hard_gate_compose_override_resets_shared_ports() -> None:
    override = Path("compose.deep-research-gate.yaml").read_text(encoding="utf-8")

    assert "ports: !reset []" in override
    assert "ports: !override" in override
    assert "DEEP_RESEARCH_GATE_POSTGRES_PORT" in override
    assert "DEEP_RESEARCH_GATE_MINIO_PORT" in override
    assert "DEEP_RESEARCH_GATE_API_PORT" in override
    assert "model-gateway:\n    ports: !reset []" in override
    assert "depends_on: !override" in override


def test_research_run_wait_respects_shared_hard_gate_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [10.0]
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return now[0]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("wikipediarag.cli.time.monotonic", fake_monotonic)
    monkeypatch.setattr("wikipediarag.cli.time.sleep", fake_sleep)
    monkeypatch.setattr(
        cli,
        "_get_json",
        lambda *_args, **_kwargs: {
            "run": {"status": "running", "topic": "private research topic"},
            "questions": [{"question": "raw tool query must not leak"}],
        },
    )

    with pytest.raises(cli.DeepResearchSuiteDeadlineExceededError, match="deadline elapsed") as exc_info:
        cli._wait_research_run_terminal(
            cast(httpx.Client, object()),
            "http://api.test",
            "run-1",
            timeout_seconds=900,
            deadline_monotonic=12.0,
        )

    assert exc_info.value.safe_code == "DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED"
    assert "private research topic" not in str(exc_info.value)
    assert "raw tool query" not in str(exc_info.value)
    assert sleeps == [2.0]


def test_ingestion_job_wait_respects_shared_hard_gate_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [20.0]
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return now[0]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("wikipediarag.cli.time.monotonic", fake_monotonic)
    monkeypatch.setattr("wikipediarag.cli.time.sleep", fake_sleep)
    monkeypatch.setattr(
        cli,
        "_get_json",
        lambda *_args, **_kwargs: {
            "status": "running",
            "progress": {"object_key": "must-not-leak"},
        },
    )

    with pytest.raises(cli.DeepResearchSuiteDeadlineExceededError, match="deadline elapsed") as exc_info:
        cli._wait_job_terminal(
            cast(httpx.Client, object()),
            "http://api.test",
            "job-1",
            timeout_seconds=360,
            deadline_monotonic=22.0,
        )

    assert exc_info.value.safe_code == "DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED"
    assert "must-not-leak" not in str(exc_info.value)
    assert sleeps == [2.0]


def test_research_action_checks_share_one_hard_gate_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    class Client:
        def post(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    run_ids = iter(["paused-run", "cancelled-run"])
    observed_deadlines: list[float | None] = []
    details: list[dict[str, object]] = [
        {"run": {"status": "paused"}},
        {"run": {"status": "completed"}},
        {"run": {"status": "cancelled"}},
    ]
    deadline = time.monotonic() + 60

    monkeypatch.setattr(cli, "_create_deep_research_run", lambda *_args, **_kwargs: next(run_ids))

    def fake_wait(*_args: object, **kwargs: object) -> dict[str, object]:
        observed = kwargs.get("deadline_monotonic")
        assert observed is None or isinstance(observed, float)
        observed_deadlines.append(observed)
        return details.pop(0)

    monkeypatch.setattr(cli, "_wait_research_run_terminal", fake_wait)

    cli._exercise_deep_research_actions(
        cast(httpx.Client, Client()),
        "http://api.test",
        "kb-1",
        "topic",
        retrieval_profile="upload_sota_mvp",
        tool_mode=DEFAULT_RESEARCH_TOOL_MODE,
        timeout_seconds=900,
        context_policy_override=None,
        deadline_monotonic=deadline,
    )

    assert observed_deadlines == [deadline, deadline, deadline]


def test_hard_gate_run_deadline_leaves_suite_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wikipediarag.cli.time.monotonic", lambda: 100.0)

    deadline = cli._deep_research_run_deadline_seconds(
        deadline_monotonic=700.0,
        fallback_timeout_seconds=900,
    )

    assert deadline == 510
    assert deadline + cli.DEEP_RESEARCH_GATE_RUN_RESERVE_SECONDS <= 600


def test_hard_gate_refuses_run_without_minimum_finalization_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wikipediarag.cli.time.monotonic", lambda: 100.0)

    with pytest.raises(cli.DeepResearchSuiteDeadlineExceededError):
        cli._deep_research_run_deadline_seconds(
            deadline_monotonic=309.0,
            fallback_timeout_seconds=900,
        )


def test_deep_research_run_creation_preserves_overrides_and_adds_deadline() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(201, json={"run_id": "run-1"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")

    assert (
        cli._create_deep_research_run(
            client,
            "http://testserver",
            "kb-1",
            "immutable topic",
            retrieval_profile="upload_sota_mvp",
            tool_mode=DEFAULT_RESEARCH_TOOL_MODE,
            context_policy_override=None,
            run_deadline_seconds=510,
        )
        == "run-1"
    )

    payload = cast(dict[str, object], captured["payload"])
    overrides = cast(dict[str, object], payload["retrieval_overrides"])
    assert overrides["retrieval"] == {"top_k": 12}
    assert overrides["postprocess"] == {"extended_search": "always"}
    assert overrides["deep_research"] == {"deadline_seconds": 510}
    assert payload["topic"] == "immutable topic"


def test_deep_research_run_creation_includes_safe_http_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/research-runs"
        return httpx.Response(
            409,
            json={"detail": {"code": "KB_NOT_READY", "message": "active index contract mismatch"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")

    with pytest.raises(RuntimeError, match="active index contract mismatch"):
        cli._create_deep_research_run(
            client,
            "http://testserver",
            "kb-1",
            "topic",
            retrieval_profile="upload_sota_mvp",
            tool_mode=DEFAULT_RESEARCH_TOOL_MODE,
            context_policy_override=None,
        )


def test_deep_research_hard_gate_openrouter_compose_requires_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_FILE", raising=False)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        cli._compose_up_deep_research_stack(model_provider="openrouter", retrieval_profile="upload_sota_mvp")


def test_deep_research_hard_gate_openrouter_compose_reads_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*_args: object, **kwargs: object) -> object:
        captured["env"] = kwargs["env"]
        return object()

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-openrouter-key\n", encoding="utf-8")
    monkeypatch.setattr("wikipediarag.cli.subprocess.run", fake_run)

    result = cli._compose_up_deep_research_stack(model_provider="openrouter", retrieval_profile="upload_sota_mvp")

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OPENROUTER_API_KEY"] == "test-openrouter-key"
    assert env["MODEL_PROVIDER"] == "openrouter"
    assert result["openrouter_api_key_source"] == "settings:OPENROUTER_API_KEY"


def test_deep_research_hard_gate_openrouter_compose_reads_key_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*_args: object, **kwargs: object) -> object:
        captured["env"] = kwargs["env"]
        return object()

    key_file = tmp_path / "openrouter.key"
    key_file.write_text("test-openrouter-key-from-file\n", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(key_file))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("wikipediarag.cli.subprocess.run", fake_run)

    result = cli._compose_up_deep_research_stack(model_provider="openrouter", retrieval_profile="upload_sota_mvp")

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OPENROUTER_API_KEY"] == "test-openrouter-key-from-file"
    assert result["openrouter_api_key_source"] == "settings:OPENROUTER_API_KEY_FILE"


def test_deep_research_matrix_cli_is_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(["deep-research-matrix", "--max-tasks", "2"])

    assert args.command == "deep-research-matrix"
    assert args.max_tasks == 2


def test_deep_research_tool_matrix_cli_is_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(["deep-research-tool-matrix", "--max-tasks", "2"])

    assert args.command == "deep-research-tool-matrix"
    assert args.fixture_path == TOOL_FIXTURE_PATH
    assert args.max_tasks == 2


def test_runtime_tool_matrix_report_keeps_default_without_safe_winner() -> None:
    report = build_runtime_tool_matrix_report(
        [
            {
                "policy_id": "all_local_tools",
                "passed": True,
                "metrics": {
                    "coverage_score": 0.9,
                    "evidence_recall": 0.9,
                    "unsupported_claim_count": 0,
                    "acl_safety": True,
                    "context_efficiency": {"avg_context_ratio": 0.5, "max_context_ratio": 0.6},
                    "latency_seconds": 5.0,
                },
            },
            {
                "policy_id": "search_plus_document_tools",
                "passed": True,
                "metrics": {
                    "coverage_score": 0.9,
                    "evidence_recall": 0.9,
                    "unsupported_claim_count": 0,
                    "acl_safety": True,
                    "context_efficiency": {"avg_context_ratio": 0.49, "max_context_ratio": 0.58},
                    "latency_seconds": 4.5,
                },
            },
        ]
    )

    assert report["recommended_policy_id"] == DEFAULT_RESEARCH_TOOL_MODE


def test_write_partial_deep_research_report_persists_incremental_snapshot(tmp_path: Path) -> None:
    report = {"passed": False, "items": [{"task_id": "alias_reformulation_chain", "passed": False}]}

    cli._write_partial_deep_research_report(tmp_path, report)

    partial_path = tmp_path / "report.partial.json"
    assert partial_path.exists()
    import json

    assert json.loads(partial_path.read_text(encoding="utf-8")) == report
