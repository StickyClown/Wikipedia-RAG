from __future__ import annotations

from wikipediarag.cli import build_parser
from wikipediarag.deep_research_eval import (
    DEFAULT_DEEP_RESEARCH_POLICY_ID,
    build_context_experiment_report,
    context_experiment_matrix,
    evaluate_research_detail,
    load_deep_research_fixtures,
    run_context_policy_experiment_rows,
)


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
    args = parser.parse_args(["deep-research-smoke", "--skip-compose", "--max-tasks", "1"])

    assert args.command == "deep-research-smoke"
    assert args.max_tasks == 1


def test_deep_research_matrix_cli_is_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(["deep-research-matrix", "--max-tasks", "2"])

    assert args.command == "deep-research-matrix"
    assert args.max_tasks == 2
