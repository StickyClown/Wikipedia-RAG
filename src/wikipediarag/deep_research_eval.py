from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from wikipediarag.research_tool_registry import (
    ALLOWED_RESEARCH_TOOLS,
    DEFAULT_RESEARCH_TOOL_MODE,
    ResearchToolMode,
    allowed_research_tools_for_mode,
    is_document_research_tool,
    normalize_research_tool_mode,
)

DEEP_RESEARCH_FIXTURE_SCHEMA_VERSION = "deep_research_fixture_v1"
DEFAULT_DEEP_RESEARCH_FIXTURE_PATH = Path("tests/fixtures/deep_research/research_tasks.json")
DEFAULT_DEEP_RESEARCH_POLICY_ID = "target_45_abstracts_only_short_structured"

CoverageStatus = Literal["covered", "partial", "missing", "conflicting"]
PackingMode = Literal["abstracts_only", "abstracts_top_raw", "raw_chunks"]
ReflectionMode = Literal["none", "short_structured", "long_freeform"]

UNSAFE_PUBLIC_TOKENS = (
    "SECRET",
    "object_key",
    "original_artifact_key",
    "normalized_artifact_key",
    "server_side_tokens",
    "access_token",
    "refresh_token",
    "s3://",
    "raw_provider_payload",
)


def _default_covered_statuses() -> list[CoverageStatus]:
    return ["covered"]


class DeepResearchFixtureDocument(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=240)
    content_type: str = Field(default="text/markdown", min_length=1, max_length=120)
    parser_profile: str = Field(default="standard", min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=64000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    access: dict[str, Any] | None = None
    expected_visible: bool = True


class DeepResearchGoldEvidence(BaseModel):
    document_id: str = Field(min_length=1, max_length=120)
    marker: str = Field(min_length=1, max_length=240)
    required: bool = True


class DeepResearchExpectedQuestion(BaseModel):
    question_contains: str = Field(min_length=1, max_length=320)
    allowed_statuses: list[CoverageStatus] = Field(default_factory=_default_covered_statuses)
    required: bool = True


class DeepResearchExpectedCoverage(BaseModel):
    min_total: int = Field(default=1, ge=0)
    min_covered: int = Field(default=0, ge=0)
    allow_partial: bool = False
    requires_missing: bool = False
    requires_conflicting: bool = False


class DeepResearchTrajectoryExpectations(BaseModel):
    min_completed_tool_calls: int = Field(default=0, ge=0)
    min_document_tool_calls: int = Field(default=0, ge=0)
    min_derived_questions: int = Field(default=0, ge=0)
    derived_question_contains: list[str] = Field(default_factory=list)
    required_tool_names: list[str] = Field(default_factory=list)
    require_tool_query_hash: bool = True
    forbid_raw_tool_query: bool = True

    @model_validator(mode="after")
    def validate_trajectory_expectations(self) -> DeepResearchTrajectoryExpectations:
        normalized_terms = [term.casefold() for term in self.derived_question_contains]
        duplicates = sorted({term for term in normalized_terms if normalized_terms.count(term) > 1})
        if duplicates:
            raise ValueError(f"duplicate derived question expectation terms: {duplicates}")
        unknown_tools = sorted({name for name in self.required_tool_names if name not in ALLOWED_RESEARCH_TOOLS})
        if unknown_tools:
            raise ValueError(f"unknown research tool expectation(s): {unknown_tools}")
        return self


class DeepResearchFixture(BaseModel):
    task_id: str = Field(min_length=1, max_length=120)
    topic: str = Field(min_length=1, max_length=32000)
    documents: list[DeepResearchFixtureDocument] = Field(min_length=1, max_length=50)
    expected_questions: list[DeepResearchExpectedQuestion] = Field(default_factory=list)
    gold_evidence: list[DeepResearchGoldEvidence] = Field(min_length=1)
    expected_coverage: DeepResearchExpectedCoverage = Field(default_factory=DeepResearchExpectedCoverage)
    expected_contradictions: list[str] = Field(default_factory=list)
    hidden_markers: list[str] = Field(default_factory=list)
    acl_setup: dict[str, Any] = Field(default_factory=dict)
    quality_tags: list[str] = Field(default_factory=list)
    expected_run_statuses: list[str] = Field(default_factory=lambda: ["completed"])
    trajectory_expectations: DeepResearchTrajectoryExpectations = Field(
        default_factory=DeepResearchTrajectoryExpectations
    )

    @model_validator(mode="after")
    def validate_fixture_links(self) -> DeepResearchFixture:
        document_ids = [document.id for document in self.documents]
        duplicates = sorted({document_id for document_id in document_ids if document_ids.count(document_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate document ids in {self.task_id}: {duplicates}")
        known = set(document_ids)
        missing = sorted({item.document_id for item in self.gold_evidence if item.document_id not in known})
        if missing:
            raise ValueError(f"gold evidence references unknown documents in {self.task_id}: {missing}")
        if self.expected_coverage.min_covered > self.expected_coverage.min_total and self.expected_coverage.min_total:
            raise ValueError(f"min_covered exceeds min_total in {self.task_id}")
        return self


class DeepResearchFixtureManifest(BaseModel):
    schema_version: str
    tasks: list[DeepResearchFixture] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_manifest(self) -> DeepResearchFixtureManifest:
        if self.schema_version != DEEP_RESEARCH_FIXTURE_SCHEMA_VERSION:
            raise ValueError(f"unsupported Deep Research fixture schema: {self.schema_version}")
        task_ids = [task.task_id for task in self.tasks]
        duplicates = sorted({task_id for task_id in task_ids if task_ids.count(task_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate Deep Research task ids: {duplicates}")
        return self


class ContextExperimentPolicy(BaseModel):
    policy_id: str
    productive_target_ratio: float
    packing_mode: PackingMode
    reflection_mode: ReflectionMode


def load_deep_research_fixtures(path: str | Path = DEFAULT_DEEP_RESEARCH_FIXTURE_PATH) -> list[DeepResearchFixture]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return DeepResearchFixtureManifest.model_validate(payload).tasks


def deep_research_fixture_by_id(
    task_id: str,
    path: str | Path = DEFAULT_DEEP_RESEARCH_FIXTURE_PATH,
) -> DeepResearchFixture:
    for fixture in load_deep_research_fixtures(path):
        if fixture.task_id == task_id:
            return fixture
    raise KeyError(f"unknown Deep Research fixture task_id: {task_id}")


def evaluate_research_detail(
    fixture: DeepResearchFixture,
    detail: Any,
    *,
    declared_context_tokens: int = 80000,
    document_id_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload = _detail_dict(detail)
    run = _mapping(payload.get("run"))
    questions = _dict_list(payload.get("questions"))
    coverage = _dict_list(payload.get("coverage"))
    evidence = _dict_list(payload.get("evidence"))
    claims = _dict_list(payload.get("claims"))
    episodes = _dict_list(payload.get("episodes"))
    tool_calls = _dict_list(payload.get("tool_calls"))
    report = _mapping(payload.get("final_report"))

    failures: list[str] = []
    status = str(run.get("status") or "")
    if status and status not in set(fixture.expected_run_statuses):
        failures.append(f"unexpected run status {status}")

    question_score, question_failures = _question_coverage_score(fixture, questions, coverage)
    failures.extend(question_failures)
    open_required = [
        str(row.get("id") or row.get("question") or "")
        for row in questions
        if "execution_state" in row
        and str(row.get("execution_state") or "pending") != "done"
        and bool((_mapping(row.get("acceptance"))).get("required", True))
    ]
    if status in {"completed", "completed_partial"} and open_required:
        failures.append(f"terminal run has required questions open: {len(open_required)}")

    evidence_recall, missing_markers = _evidence_recall(
        fixture,
        evidence,
        report,
        document_id_aliases=document_id_aliases,
    )
    for marker in missing_markers:
        failures.append(f"missing required evidence marker {marker}")

    coverage_metrics = _coverage_metrics(coverage)
    expected = fixture.expected_coverage
    if coverage_metrics["total"] < expected.min_total:
        failures.append(f"coverage total {coverage_metrics['total']} below expected {expected.min_total}")
    effective_covered = coverage_metrics["covered"] + (coverage_metrics["partial"] if expected.allow_partial else 0)
    if effective_covered < expected.min_covered:
        coverage_label = "covered/partial" if expected.allow_partial else "covered"
        failures.append(f"{coverage_label} count {effective_covered} below expected {expected.min_covered}")
    if expected.requires_missing and coverage_metrics["missing"] == 0 and coverage_metrics["partial"] == 0:
        failures.append("missing or partial coverage was expected but not observed")

    contradiction_handled = _contradiction_handled(fixture, coverage, evidence, report)
    if not contradiction_handled:
        failures.append("expected contradiction was not represented in coverage, evidence or report")
    if (
        expected.requires_conflicting
        and coverage_metrics["total"] > 0
        and coverage_metrics["covered"] == coverage_metrics["total"]
    ):
        failures.append("contradiction task ended with only confident covered coverage")

    unsupported_claim_count = _unsupported_claim_count(claims, evidence)
    if unsupported_claim_count:
        failures.append(f"claims without visible evidence: {unsupported_claim_count}")

    acl_safety, acl_failures = _acl_safety(fixture, payload)
    failures.extend(acl_failures)

    resume_integrity, resume_failures = _resume_integrity(questions, evidence, episodes)
    failures.extend(resume_failures)

    trajectory_metrics, trajectory_failures = _trajectory_metrics(
        fixture,
        run,
        questions,
        tool_calls,
        episodes,
        payload,
    )
    failures.extend(trajectory_failures)

    context_metrics = _context_metrics(episodes, declared_context_tokens=max(1, declared_context_tokens))
    coverage_score = (
        question_score if fixture.expected_questions else _coarse_coverage_score(expected, coverage_metrics)
    )

    return {
        "task_id": fixture.task_id,
        "passed": not failures,
        "failures": failures,
        "metrics": {
            "coverage_score": coverage_score,
            "evidence_recall": evidence_recall,
            "unsupported_claim_count": unsupported_claim_count,
            "contradiction_handled": contradiction_handled,
            "context_efficiency": context_metrics,
            "acl_safety": acl_safety,
            "resume_integrity": resume_integrity,
            "trajectory": trajectory_metrics,
            "coverage": coverage_metrics,
            "open_required_questions": len(open_required),
            "run_status": status,
        },
    }


def context_experiment_matrix() -> list[ContextExperimentPolicy]:
    policies: list[ContextExperimentPolicy] = []
    for target in (0.35, 0.45, 0.55):
        for packing_mode in ("abstracts_only", "abstracts_top_raw", "raw_chunks"):
            for reflection_mode in ("none", "short_structured", "long_freeform"):
                target_id = int(target * 100)
                policies.append(
                    ContextExperimentPolicy(
                        policy_id=f"target_{target_id}_{packing_mode}_{reflection_mode}",
                        productive_target_ratio=target,
                        packing_mode=packing_mode,
                        reflection_mode=reflection_mode,
                    )
                )
    return policies


def build_context_experiment_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matrix = [policy.model_dump() for policy in context_experiment_matrix()]
    if not rows:
        return {
            "schema_version": "deep_research_context_experiment_report_v1",
            "policies": matrix,
            "results": [],
            "policy_results": [],
            "recommended_policy_id": DEFAULT_DEEP_RESEARCH_POLICY_ID,
            "reason": "no measured rows; keep current default policy",
        }
    normalized = [_normalize_experiment_row(row) for row in rows]
    policy_results = _aggregate_experiment_rows(normalized)
    ranked = sorted(policy_results, key=_experiment_sort_key)
    default = next((row for row in ranked if row["policy_id"] == DEFAULT_DEEP_RESEARCH_POLICY_ID), None)
    best = ranked[0]
    recommended = best
    reason = "best measured policy wins safety-first Pareto ranking"
    if default is not None and not _beats_default(best, default):
        recommended = default
        reason = "current 45% default remains because no policy gives a clear safe Pareto improvement"
    return {
        "schema_version": "deep_research_context_experiment_report_v1",
        "policies": matrix,
        "results": normalized,
        "policy_results": policy_results,
        "ranked_policy_ids": [row["policy_id"] for row in ranked],
        "recommended_policy_id": recommended["policy_id"],
        "reason": reason,
    }


def run_context_policy_experiment_rows(
    fixtures: list[DeepResearchFixture],
    *,
    declared_context_tokens: int = 80000,
) -> list[dict[str, Any]]:
    from wikipediarag.deep_research import ResearchContextBudget, pack_research_context

    rows: list[dict[str, Any]] = []
    max_context = max(1, int(declared_context_tokens))
    for policy in context_experiment_matrix():
        budget = _budget_for_context_policy(policy, max_context, ResearchContextBudget)
        for fixture in fixtures:
            evidence_records = _synthetic_evidence_records(fixture, policy)
            coverage_records = _synthetic_coverage_records(fixture)
            reflections = _synthetic_reflections(policy.reflection_mode)
            current_question = (
                fixture.expected_questions[0].question_contains if fixture.expected_questions else str(fixture.topic)
            )
            packed = pack_research_context(
                topic=str(fixture.topic),
                current_question=current_question,
                run_progress={"stage": "context_experiment", "fixture_task_id": fixture.task_id},
                coverage_records=coverage_records,
                evidence_records=evidence_records,
                reflections=reflections,
                budget=budget,
            )
            serialized = json.dumps(packed, ensure_ascii=False, sort_keys=True)
            required_markers = _visible_required_markers(fixture)
            missing_markers = [marker for marker in required_markers if marker not in serialized]
            hidden_leaks = [marker for marker in fixture.hidden_markers if marker in serialized]
            token_estimate = int(packed.get("token_estimate") or 0)
            over_hard = bool(packed.get("over_hard_input_limit"))
            evidence_recall = (
                (len(required_markers) - len(missing_markers)) / len(required_markers) if required_markers else 1.0
            )
            acl_safety = not hidden_leaks and all(token not in serialized for token in UNSAFE_PUBLIC_TOKENS)
            rows.append(
                {
                    "policy_id": policy.policy_id,
                    "fixture_task_id": fixture.task_id,
                    "passed": bool(acl_safety and not over_hard and evidence_recall >= 1.0),
                    "metrics": {
                        "coverage_score": 1.0,
                        "evidence_recall": evidence_recall,
                        "unsupported_claim_count": 0,
                        "acl_safety": acl_safety,
                        "context_efficiency": {
                            "max_tokens": token_estimate,
                            "avg_tokens": token_estimate,
                            "max_context_ratio": token_estimate / max_context,
                            "avg_context_ratio": token_estimate / max_context,
                        },
                        "latency_seconds": 0.0,
                        "over_soft_limit": bool(packed.get("over_soft_limit")),
                        "over_hard_input_limit": over_hard,
                    },
                    "policy": policy.model_dump(),
                    "trimming": list(packed.get("trimming") or []),
                    "missing_markers": missing_markers,
                    "hidden_leaks": hidden_leaks,
                    "experiment_mode": "offline_context_packer",
                }
            )
    return rows


def _detail_dict(detail: Any) -> dict[str, Any]:
    model_dump = getattr(detail, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, dict):
            return dumped
    if isinstance(detail, dict):
        return detail
    raise TypeError("research detail must be a dict or pydantic model")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _question_coverage_score(
    fixture: DeepResearchFixture,
    questions: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
) -> tuple[float, list[str]]:
    if not fixture.expected_questions:
        return 1.0, []
    coverage_by_question_id = {str(row.get("question_id")): str(row.get("status") or "") for row in coverage}
    matched = 0
    failures: list[str] = []
    for expected in fixture.expected_questions:
        matches = [
            row
            for row in questions
            if expected.question_contains.casefold() in str(row.get("question") or "").casefold()
        ]
        allowed = set(expected.allowed_statuses)
        question = next(
            (
                row
                for row in matches
                if coverage_by_question_id.get(str(row.get("id")), str(row.get("status") or "")) in allowed
            ),
            matches[0] if matches else None,
        )
        if question is None:
            if expected.required:
                failures.append(f"expected question containing {expected.question_contains!r} was not created")
            continue
        status = coverage_by_question_id.get(str(question.get("id")), str(question.get("status") or ""))
        if status in set(expected.allowed_statuses):
            matched += 1
        elif expected.required:
            failures.append(
                f"question containing {expected.question_contains!r} had status {status}, "
                f"expected one of {expected.allowed_statuses}"
            )
    return matched / max(len(fixture.expected_questions), 1), failures


def _evidence_recall(
    fixture: DeepResearchFixture,
    evidence: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    document_id_aliases: dict[str, str] | None = None,
) -> tuple[float, list[str]]:
    required = [item for item in fixture.gold_evidence if item.required]
    if not required:
        return 1.0, []
    evidence_by_document: dict[str, str] = {}
    has_document_provenance = any(str(row.get("document_id") or "") for row in evidence)
    for row in evidence:
        document_id = str(row.get("document_id") or "")
        if not document_id:
            continue
        evidence_by_document[document_id] = (
            f"{evidence_by_document.get(document_id, '')} {json.dumps(row, ensure_ascii=False)}"
        )
    if has_document_provenance:
        missing = [
            item.marker
            for item in required
            if item.marker
            not in evidence_by_document.get(
                str((document_id_aliases or {}).get(item.document_id) or item.document_id),
                "",
            )
        ]
    else:
        evidence_text = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        missing = [item.marker for item in required if item.marker not in evidence_text]
    return (len(required) - len(missing)) / len(required), missing


def _coverage_metrics(coverage: list[dict[str, Any]]) -> dict[str, int]:
    statuses = [str(row.get("status") or "") for row in coverage]
    return {
        "total": len(statuses),
        "covered": sum(1 for status in statuses if status == "covered"),
        "partial": sum(1 for status in statuses if status == "partial"),
        "missing": sum(1 for status in statuses if status == "missing"),
        "conflicting": sum(1 for status in statuses if status == "conflicting"),
    }


def _coarse_coverage_score(expected: DeepResearchExpectedCoverage, metrics: dict[str, int]) -> float:
    numerator = metrics["covered"] + (metrics["partial"] if expected.allow_partial else 0)
    denominator = max(metrics["total"], expected.min_total, 1)
    return min(1.0, numerator / denominator)


def _contradiction_handled(
    fixture: DeepResearchFixture,
    coverage: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    report: dict[str, Any],
) -> bool:
    required = fixture.expected_coverage.requires_conflicting or bool(fixture.expected_contradictions)
    if not required:
        return True
    serialized = json.dumps({"coverage": coverage, "evidence": evidence, "report": report}, ensure_ascii=False)
    lowered = serialized.casefold()
    return (
        '"status": "conflicting"' in serialized
        or '"support_status": "contradicts"' in serialized
        or "contradict" in lowered
        or "conflict" in lowered
        or "противореч" in lowered
    )


def _unsupported_claim_count(claims: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> int:
    visible_evidence_ids = {str(row.get("id")) for row in evidence}
    unsupported = 0
    for claim in claims:
        if str(claim.get("support_status") or "") == "unsupported":
            unsupported += 1
            continue
        evidence_ids = [str(item) for item in claim.get("evidence_ids") or []]
        if not evidence_ids or not any(item in visible_evidence_ids for item in evidence_ids):
            unsupported += 1
    return unsupported


def _acl_safety(fixture: DeepResearchFixture, detail: dict[str, Any]) -> tuple[bool, list[str]]:
    serialized = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    failures: list[str] = []
    for marker in fixture.hidden_markers:
        if marker in serialized:
            failures.append(f"hidden marker leaked: {marker}")
    for token in UNSAFE_PUBLIC_TOKENS:
        if token in serialized:
            failures.append(f"unsafe public token leaked: {token}")
    return not failures, failures


def _resume_integrity(
    questions: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    _append_duplicate_failure(failures, "question ids", [str(row.get("id")) for row in questions if row.get("id")])
    _append_duplicate_failure(failures, "evidence ids", [str(row.get("id")) for row in evidence if row.get("id")])
    _append_duplicate_failure(
        failures,
        "episode indexes",
        [str(row.get("episode_index")) for row in episodes if row.get("episode_index") is not None],
    )
    return not failures, failures


def _trajectory_metrics(
    fixture: DeepResearchFixture,
    run: dict[str, Any],
    questions: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    expectations = fixture.trajectory_expectations
    tool_mode = normalize_research_tool_mode(str(run.get("tool_mode") or DEFAULT_RESEARCH_TOOL_MODE))
    allowed_tool_names = set(allowed_research_tools_for_mode(tool_mode))
    required_tool_names = set(expectations.required_tool_names)
    derived_questions = [row for row in questions if str(row.get("kind") or "").casefold() == "derived"]
    completed_tool_calls = [row for row in tool_calls if str(row.get("status") or "").casefold() == "completed"]
    completed_document_tool_calls = [
        row for row in completed_tool_calls if is_document_research_tool(str(row.get("tool_name") or ""))
    ]
    tool_names = sorted({str(row.get("tool_name") or "") for row in tool_calls if row.get("tool_name")})
    completed_tool_names = {str(row.get("tool_name") or "") for row in completed_tool_calls if row.get("tool_name")}
    missing_hash_count = sum(1 for row in tool_calls if not str(row.get("tool_query_hash") or ""))
    derived_payload = json.dumps(
        [str(row.get("question") or "") for row in derived_questions],
        ensure_ascii=False,
        sort_keys=True,
    ).casefold()
    required_terms = list(expectations.derived_question_contains)
    found_terms = [term for term in required_terms if term.casefold() in derived_payload]
    missing_terms = [term for term in required_terms if term.casefold() not in derived_payload]
    raw_payload_leak = _contains_forbidden_public_key(payload)
    context_summaries = [_mapping(row.get("context_summary")) for row in episodes]

    failures: list[str] = []
    # Counts and preferred tool names describe research efficiency.  They do
    # not prove correctness and therefore remain diagnostic metrics rather
    # than hard failures.  The hard gate below still enforces allowlists,
    # hashes and public-surface safety.
    unknown_tools = sorted({name for name in tool_names if name not in ALLOWED_RESEARCH_TOOLS})
    if unknown_tools:
        failures.append(f"unknown public research tool calls: {unknown_tools}")
    disallowed_tools = sorted(
        {name for name in tool_names if name in ALLOWED_RESEARCH_TOOLS and name not in allowed_tool_names}
    )
    if disallowed_tools:
        failures.append(f"tool calls outside current tool_mode allowlist: {disallowed_tools}")
    if expectations.require_tool_query_hash and missing_hash_count:
        failures.append(f"tool calls without tool_query_hash: {missing_hash_count}")
    if expectations.forbid_raw_tool_query and raw_payload_leak:
        failures.append("unsafe raw query/provider/storage key leaked in public research detail")

    return (
        {
            "tool_call_count": len(tool_calls),
            "completed_tool_call_count": len(completed_tool_calls),
            "document_tool_call_count": sum(
                1 for row in tool_calls if is_document_research_tool(str(row.get("tool_name") or ""))
            ),
            "completed_document_tool_call_count": len(completed_document_tool_calls),
            "tool_names": tool_names,
            "tool_mode": tool_mode,
            "allowed_tool_names": sorted(allowed_tool_names),
            "required_tool_names_found": sorted(required_tool_names & completed_tool_names),
            "missing_tool_query_hash_count": missing_hash_count,
            "derived_question_count": len(derived_questions),
            "required_derived_terms": required_terms,
            "required_derived_terms_found": found_terms,
            "required_derived_terms_missing": missing_terms,
            "raw_tool_payload_leak": raw_payload_leak,
            "episodes_with_context_summary": sum(1 for summary in context_summaries if summary),
            "episodes_over_soft_limit": sum(
                1 for summary in context_summaries if summary.get("over_soft_limit") is True
            ),
            "episodes_over_hard_input_limit": sum(
                1 for summary in context_summaries if summary.get("over_hard_input_limit") is True
            ),
        },
        failures,
    )


def _contains_forbidden_public_key(value: Any) -> bool:
    forbidden = {
        "tool_query",
        "raw_query",
        "provider_payload",
        "raw_provider_payload",
        "prompt",
        "messages",
        "object_key",
        "original_artifact_key",
        "normalized_artifact_key",
        "server_side_tokens",
        "access_token",
        "refresh_token",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in forbidden:
                return True
            if _contains_forbidden_public_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_public_key(child) for child in value)
    return False


def _append_duplicate_failure(failures: list[str], label: str, values: list[str]) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        failures.append(f"duplicate {label}: {duplicates}")


def _context_metrics(episodes: list[dict[str, Any]], *, declared_context_tokens: int) -> dict[str, float | int]:
    estimates: list[int] = []
    for episode in episodes:
        summary = _mapping(episode.get("context_summary"))
        value = summary.get("token_estimate")
        if isinstance(value, int | float):
            estimates.append(int(value))
    if not estimates:
        return {"max_tokens": 0, "avg_tokens": 0, "max_context_ratio": 0.0, "avg_context_ratio": 0.0}
    return {
        "max_tokens": max(estimates),
        "avg_tokens": int(mean(estimates)),
        "max_context_ratio": max(estimates) / declared_context_tokens,
        "avg_context_ratio": mean(estimates) / declared_context_tokens,
    }


def _normalize_experiment_row(row: dict[str, Any]) -> dict[str, Any]:
    metrics = _mapping(row.get("metrics"))
    context = _mapping(metrics.get("context_efficiency"))
    normalized = {
        "policy_id": str(row.get("policy_id") or DEFAULT_DEEP_RESEARCH_POLICY_ID),
        "passed": bool(row.get("passed", True)),
        "coverage_score": float(metrics.get("coverage_score") or 0.0),
        "evidence_recall": float(metrics.get("evidence_recall") or 0.0),
        "unsupported_claim_count": int(metrics.get("unsupported_claim_count") or 0),
        "acl_safety": bool(metrics.get("acl_safety", True)),
        "max_context_ratio": float(context.get("max_context_ratio") or metrics.get("max_context_ratio") or 0.0),
        "avg_context_ratio": float(context.get("avg_context_ratio") or metrics.get("avg_context_ratio") or 0.0),
        "latency_seconds": float(metrics.get("latency_seconds") or 0.0),
    }
    if row.get("fixture_task_id") is not None:
        normalized["fixture_task_id"] = str(row["fixture_task_id"])
    if row.get("experiment_mode") is not None:
        normalized["experiment_mode"] = str(row["experiment_mode"])
    if isinstance(row.get("trimming"), list):
        normalized["trimming"] = list(row["trimming"])
    if isinstance(row.get("missing_markers"), list):
        normalized["missing_markers"] = list(row["missing_markers"])
    return normalized


def _aggregate_experiment_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["policy_id"]), []).append(row)
    aggregates: list[dict[str, Any]] = []
    for policy_id, policy_rows in sorted(grouped.items()):
        row_count = len(policy_rows)
        aggregates.append(
            {
                "task_id": policy_id,
                "policy_id": policy_id,
                "passed": all(bool(row["passed"]) for row in policy_rows),
                "row_count": row_count,
                "passed_count": sum(1 for row in policy_rows if bool(row["passed"])),
                "coverage_score": mean(float(row["coverage_score"]) for row in policy_rows),
                "evidence_recall": mean(float(row["evidence_recall"]) for row in policy_rows),
                "unsupported_claim_count": sum(int(row["unsupported_claim_count"]) for row in policy_rows),
                "acl_safety": all(bool(row["acl_safety"]) for row in policy_rows),
                "max_context_ratio": max(float(row["max_context_ratio"]) for row in policy_rows),
                "avg_context_ratio": mean(float(row["avg_context_ratio"]) for row in policy_rows),
                "latency_seconds": mean(float(row["latency_seconds"]) for row in policy_rows),
                "failures": _policy_row_failures(policy_rows),
            }
        )
    return aggregates


def _policy_row_failures(rows: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for row in rows:
        if bool(row["passed"]):
            continue
        fixture_id = str(row.get("fixture_task_id") or "fixture")
        failures.append(
            f"{fixture_id}: recall={float(row['evidence_recall']):.3f}, "
            f"context={float(row['max_context_ratio']):.3f}, acl={bool(row['acl_safety'])}"
        )
    return failures[:20]


def _budget_for_context_policy(policy: ContextExperimentPolicy, max_context: int, budget_type: Any) -> Any:
    target = float(policy.productive_target_ratio)
    soft = min(0.95, target + 0.10)
    hard = min(0.95, target + 0.25)
    return budget_type(
        max_context_tokens=max_context,
        productive_target_tokens=max(1, int(max_context * target)),
        soft_limit_tokens=max(1, int(max_context * soft)),
        hard_input_limit_tokens=max(1, int(max_context * hard)),
        output_reserve_tokens=max(1, int(max_context * 0.15)),
        safety_reserve_tokens=max(1, int(max_context * 0.15)),
    )


def _synthetic_evidence_records(
    fixture: DeepResearchFixture,
    policy: ContextExperimentPolicy,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    markers_by_document_id: dict[str, list[str]] = {}
    for evidence in fixture.gold_evidence:
        markers_by_document_id.setdefault(evidence.document_id, []).append(evidence.marker)
    visible_documents = [document for document in fixture.documents if document.expected_visible]
    for index, document in enumerate(visible_documents, start=1):
        markers = markers_by_document_id.get(document.id, [])
        abstract = _policy_abstract(str(document.content), markers, index, policy.packing_mode)
        records.append(
            {
                "id": f"{fixture.task_id}:{document.id}",
                "evidence_ref": f"E{index}",
                "title": str(document.metadata.get("title") or document.filename),
                "section_path": [fixture.task_id, document.id],
                "content_abstract": abstract,
                "support_status": "supports" if markers else "context",
                "score": 1.0 / index,
            }
        )
    return records


def _policy_abstract(content: str, markers: list[str], index: int, packing_mode: PackingMode) -> str:
    if packing_mode == "raw_chunks":
        return content
    if packing_mode == "abstracts_top_raw" and index <= 2:
        return content[:1600]
    return _marker_snippet(content, markers, max_chars=420)


def _marker_snippet(content: str, markers: list[str], *, max_chars: int) -> str:
    normalized = " ".join(content.split())
    marker_position = min((normalized.find(marker) for marker in markers if marker in normalized), default=-1)
    if marker_position < 0:
        return normalized[:max_chars]
    start = max(0, marker_position - max_chars // 3)
    return normalized[start : start + max_chars]


def _synthetic_coverage_records(fixture: DeepResearchFixture) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, expected in enumerate(fixture.expected_questions, start=1):
        status = expected.allowed_statuses[0] if expected.allowed_statuses else "covered"
        records.append(
            {
                "id": f"{fixture.task_id}:coverage:{index}",
                "question_id": f"{fixture.task_id}:question:{index}",
                "question": expected.question_contains,
                "status": status,
                "reason": "synthetic context experiment expected coverage",
            }
        )
    return records


def _synthetic_reflections(reflection_mode: ReflectionMode) -> list[dict[str, Any]]:
    if reflection_mode == "none":
        return []
    if reflection_mode == "short_structured":
        return [
            {"id": "r1", "body": "Need verify coverage gaps before final synthesis."},
            {"id": "r2", "body": "Prioritize explicit markers and contradiction status."},
            {"id": "r3", "body": "Keep reflection operational, not factual evidence."},
        ]
    long_body = (
        "Operational reflection: the current evidence set is broad, noisy and should be compacted before "
        "generation. Prefer visible source abstracts, preserve contradiction state, and do not promote "
        "hypotheses into claims without evidence. " * 24
    )
    return [{"id": f"r{index}", "body": long_body} for index in range(1, 4)]


def _visible_required_markers(fixture: DeepResearchFixture) -> list[str]:
    visible_document_ids = {document.id for document in fixture.documents if document.expected_visible}
    return [
        evidence.marker
        for evidence in fixture.gold_evidence
        if evidence.required and evidence.document_id in visible_document_ids
    ]


def _experiment_sort_key(row: dict[str, Any]) -> tuple[int, int, int, float, float, float, float, int]:
    return (
        0 if row["passed"] else 1,
        0 if row["acl_safety"] else 1,
        int(row["unsupported_claim_count"]),
        -float(row["evidence_recall"]),
        -float(row["coverage_score"]),
        float(row["avg_context_ratio"]),
        float(row["latency_seconds"]),
        0 if row["policy_id"] == DEFAULT_DEEP_RESEARCH_POLICY_ID else 1,
    )


def _beats_default(best: dict[str, Any], default: dict[str, Any]) -> bool:
    if best["policy_id"] == default["policy_id"]:
        return False
    if not best["passed"] or not best["acl_safety"]:
        return False
    if int(best["unsupported_claim_count"]) > int(default["unsupported_claim_count"]):
        return False
    quality_is_not_worse = float(best["coverage_score"]) >= float(default["coverage_score"]) and float(
        best["evidence_recall"]
    ) >= float(default["evidence_recall"])
    context_improves = float(best["avg_context_ratio"]) <= float(default["avg_context_ratio"]) * 0.95
    quality_improves = float(best["coverage_score"]) > float(default["coverage_score"]) or float(
        best["evidence_recall"]
    ) > float(default["evidence_recall"])
    return quality_is_not_worse and (context_improves or quality_improves)


def runtime_tool_matrix_modes() -> list[ResearchToolMode]:
    return ["extended_search_only", "search_plus_document_tools", "all_local_tools"]


def build_runtime_tool_matrix_report(
    rows: list[dict[str, Any]],
    *,
    default_policy_id: str = DEFAULT_RESEARCH_TOOL_MODE,
) -> dict[str, Any]:
    policies = [
        {
            "policy_id": tool_mode,
            "tool_mode": tool_mode,
            "allowed_tools": list(allowed_research_tools_for_mode(tool_mode)),
        }
        for tool_mode in runtime_tool_matrix_modes()
    ]
    if not rows:
        return {
            "schema_version": "deep_research_runtime_tool_matrix_report_v1",
            "policies": policies,
            "results": [],
            "policy_results": [],
            "recommended_policy_id": default_policy_id,
            "reason": "no measured rows; keep current default tool_mode",
        }
    normalized = [_normalize_experiment_row(row) for row in rows]
    policy_results = _aggregate_experiment_rows(normalized)
    ranked = sorted(policy_results, key=_experiment_sort_key)
    default = next((row for row in ranked if row["policy_id"] == default_policy_id), None)
    best = ranked[0]
    recommended = best
    reason = "best measured tool_mode wins safety-first Pareto ranking"
    if default is not None and not _beats_default(best, default):
        recommended = default
        reason = "current all_local_tools default remains because no tool_mode gives a clear safe Pareto improvement"
    return {
        "schema_version": "deep_research_runtime_tool_matrix_report_v1",
        "policies": policies,
        "results": normalized,
        "policy_results": policy_results,
        "ranked_policy_ids": [row["policy_id"] for row in ranked],
        "recommended_policy_id": recommended["policy_id"],
        "reason": reason,
    }
