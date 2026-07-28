from __future__ import annotations

import json

import pytest

from wikipediarag.cli import (
    _parse_family_weight_specs,
    build_parser,
    run_eval_generate,
    run_eval_generate_status,
)
from wikipediarag.eval.progress import EvalGenerateCliReporter, format_generate_status, format_progress_event
from wikipediarag.eval.schemas import (
    EvalDatasetManifest,
    EvalGenerateAcceptedTaskRecord,
    EvalGenerateModelRef,
    EvalGenerateProgressEvent,
    EvalGenerateRunStatus,
    EvalGenerateRuntimeConfig,
    EvalGenerateStats,
)


def _status() -> EvalGenerateRunStatus:
    return EvalGenerateRunStatus(
        run_id="run-123",
        state="running",
        phase="family_generation",
        started_at="2026-07-26T10:00:00Z",
        updated_at="2026-07-26T10:05:00Z",
        active_family="comparison_multi_hop",
        current_attempt=11,
        count_target=20,
        family_targets={
            "single_hop_factual": 0,
            "alias_redirect_rare": 0,
            "deep_section_fact": 0,
            "comparison_multi_hop": 20,
            "unanswerable": 0,
            "hard_negative": 0,
        },
        family_attempts_started={
            "single_hop_factual": 0,
            "alias_redirect_rare": 0,
            "deep_section_fact": 0,
            "comparison_multi_hop": 11,
            "unanswerable": 0,
            "hard_negative": 0,
        },
        config=EvalGenerateRuntimeConfig(
            run_id="run-123",
            count=20,
            concurrency=10,
            generator=EvalGenerateModelRef(alias="generator_main", provider="openrouter", model="gpt-test"),
            verifier=EvalGenerateModelRef(alias="verifier", provider="openrouter", model="gpt-check"),
            family_weights={
                "single_hop_factual": 0.0,
                "alias_redirect_rare": 0.0,
                "deep_section_fact": 0.0,
                "comparison_multi_hop": 1.0,
                "unanswerable": 0.0,
                "hard_negative": 0.0,
            },
            family_targets={
                "single_hop_factual": 0,
                "alias_redirect_rare": 0,
                "deep_section_fact": 0,
                "comparison_multi_hop": 20,
                "unanswerable": 0,
                "hard_negative": 0,
            },
        ),
        snapshot_id="snapshot",
        index_version="index",
        zim_checksum="sha",
        retrieval_profile_hash="profile",
        stats=EvalGenerateStats(
            accepted=7,
            rejected=4,
            errors=1,
            retries=2,
            family_accepted={
                "single_hop_factual": 0,
                "alias_redirect_rare": 0,
                "deep_section_fact": 0,
                "comparison_multi_hop": 7,
                "unanswerable": 0,
                "hard_negative": 0,
            },
            family_targets={
                "single_hop_factual": 0,
                "alias_redirect_rare": 0,
                "deep_section_fact": 0,
                "comparison_multi_hop": 20,
                "unanswerable": 0,
                "hard_negative": 0,
            },
        ),
        accepted_tasks=[
            EvalGenerateAcceptedTaskRecord(
                question="Чем различаются «Статья 1» и «Статья 2» по локальным фактам?",
                task_family="comparison_multi_hop",
                gold_page_ids=["p1", "p2"],
                gold_chunk_ids=["c1", "c2"],
                reasoning_path=["Статья 1", "Статья 2"],
                generation_seed=1,
            )
        ],
    )


def test_parse_family_weight_specs_sets_unspecified_families_to_zero() -> None:
    parsed = _parse_family_weight_specs(["comparison_multi_hop=1", "single_hop_factual=2.5"])

    assert parsed == {
        "single_hop_factual": 2.5,
        "alias_redirect_rare": 0.0,
        "deep_section_fact": 0.0,
        "comparison_multi_hop": 1.0,
        "unanswerable": 0.0,
        "hard_negative": 0.0,
    }


def test_parse_family_weight_specs_rejects_unknown_family() -> None:
    with pytest.raises(ValueError, match="unknown task family"):
        _parse_family_weight_specs(["made_up_family=1"])


def test_format_progress_event_includes_elapsed_question_and_summary() -> None:
    accepted = format_progress_event(
        EvalGenerateProgressEvent(
            event="task_accepted",
            elapsed_seconds=65,
            count_target=150,
            total_accepted=7,
            family="single_hop_factual",
            family_target=60,
            family_accepted=3,
            attempt=14,
            question="Что известно о статье «Россия»?",
        )
    )
    completed = format_progress_event(
        EvalGenerateProgressEvent(
            event="run_completed",
            elapsed_seconds=3661,
            count_target=150,
            total_accepted=150,
            stats=EvalGenerateStats(
                accepted=150,
                rejected=17,
                errors=2,
                retries=5,
                family_accepted={
                    "single_hop_factual": 60,
                    "alias_redirect_rare": 20,
                    "deep_section_fact": 20,
                    "comparison_multi_hop": 25,
                    "unanswerable": 15,
                    "hard_negative": 10,
                },
                family_targets={
                    "single_hop_factual": 60,
                    "alias_redirect_rare": 20,
                    "deep_section_fact": 20,
                    "comparison_multi_hop": 25,
                    "unanswerable": 15,
                    "hard_negative": 10,
                },
            ),
        )
    )

    assert accepted == (
        "[00:01:05] family=single_hop_factual family_progress=3/60 total=7/150"
        ' attempt=14 state=accepted question="Что известно о статье «Россия»?"'
    )
    assert completed.startswith("[01:01:01] state=completed duration=01:01:01 total=150/150 accepted=150 rejected=17")
    assert "errors=2" in completed
    assert "retries=5" in completed
    assert "single_hop_factual:60/60" in completed
    assert "hard_negative:10/10" in completed


def test_format_generate_status_includes_models_progress_and_recent_questions() -> None:
    rendered = format_generate_status(_status())

    assert "run_id=run-123 state=running phase=family_generation" in rendered
    assert "progress total=7/20 rejected=4 errors=1 retries=2" in rendered
    assert "generator=generator_main (openrouter:gpt-test)" in rendered
    assert "verifier=verifier (openrouter:gpt-check)" in rendered
    assert "active_family=comparison_multi_hop family_progress=7/20 current_attempt=11" in rendered
    assert "comparison_multi_hop:7/20" in rendered
    assert "Чем различаются «Статья 1» и «Статья 2» по локальным фактам?" in rendered


def test_run_eval_generate_streams_progress_before_manifest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_eval_generate(
        count: int | None = None,
        *,
        concurrency: int | None = None,
        generator_alias: str | None = None,
        verifier_alias: str | None = None,
        family_weights: dict[str, float] | None = None,
        run_id: str | None = None,
        resume_run_id: str | None = None,
        settings: object | None = None,
        progress_callback: EvalGenerateCliReporter | None = None,
    ) -> EvalDatasetManifest:
        assert count == 3
        assert concurrency == 5
        assert generator_alias == "gen-custom"
        assert verifier_alias == "ver-custom"
        assert family_weights == {
            "single_hop_factual": 0.0,
            "alias_redirect_rare": 0.0,
            "deep_section_fact": 0.0,
            "comparison_multi_hop": 1.0,
            "unanswerable": 0.0,
            "hard_negative": 0.0,
        }
        assert run_id == "run-123"
        assert resume_run_id is None
        assert settings is None
        assert progress_callback is not None
        assert isinstance(progress_callback, EvalGenerateCliReporter)
        progress_callback(
            EvalGenerateProgressEvent(
                event="run_started",
                elapsed_seconds=0,
                count_target=3,
                total_accepted=0,
                dataset_name="generated-wikipedia-v1",
                run_id="run-123",
                snapshot_id="snapshot",
                index_version="index",
            )
        )
        progress_callback(
            EvalGenerateProgressEvent(
                event="candidate_generated",
                elapsed_seconds=5,
                count_target=3,
                total_accepted=0,
                family="comparison_multi_hop",
                family_target=3,
                family_accepted=0,
                attempt=1,
                question="Какой вопрос был сгенерирован?",
            )
        )
        return EvalDatasetManifest(
            dataset_name="generated-wikipedia-v1",
            dataset_version="2026.07.1",
            dataset_hash="hash",
            task_count=3,
            created_at="2026-07-26T12:00:00Z",
            snapshot_id="snapshot",
            index_version="index",
            zim_checksum="sha",
            retrieval_profile_hash="profile",
            generator_alias="gen-custom",
            verifier_alias="ver-custom",
            jsonl_path="artifacts/eval/datasets/generated-wikipedia-v1/test.jsonl",
        )

    monkeypatch.setattr("wikipediarag.eval.commands.eval_generate", fake_eval_generate)

    args = build_parser().parse_args(
        [
            "eval-generate",
            "--count",
            "3",
            "--concurrency",
            "5",
            "--generator-alias",
            "gen-custom",
            "--verifier-alias",
            "ver-custom",
            "--family-weight",
            "comparison_multi_hop=1",
            "--run-id",
            "run-123",
        ]
    )
    run_eval_generate(args)
    captured = capsys.readouterr().out

    assert (
        "[00:00:00] start run_id=run-123 total=0/3 dataset=generated-wikipedia-v1 snapshot=snapshot index=index"
        in captured
    )
    assert 'state=candidate_generated question="Какой вопрос был сгенерирован?"' in captured
    assert captured.find("state=candidate_generated") < captured.find('"task_count": 3')


def test_run_eval_generate_status_outputs_human_and_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = _status()

    monkeypatch.setattr("wikipediarag.eval.commands.eval_generate_status", lambda **_kwargs: status)

    run_eval_generate_status("run-123", False, False)
    human = capsys.readouterr().out
    assert "run_id=run-123 state=running phase=family_generation" in human
    assert "comparison_multi_hop:7/20" in human

    run_eval_generate_status(None, True, True)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["run_id"] == "run-123"
    assert parsed["stats"]["accepted"] == 7
