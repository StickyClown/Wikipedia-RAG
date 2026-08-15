"""Create the local, content-free P0.1 control corpus.

The generated corpus is intentionally small and deterministic. It is a safety
fixture for validating the evaluation path; production baselines can replace
the ignored files with approved Wikipedia/RRNCB material without changing the
schema or runner.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from wikipediarag.eval.artifacts import write_json, write_jsonl
from wikipediarag.eval.quality import QUALITY_FAMILIES, QUALITY_LANGUAGES
from wikipediarag.eval.schemas import CorpusSource, EvalTask, ExpectedClaim, GoldEvidence, ScopeReview

LANGUAGE_WORDS = {
    "ru": ("Какой контрольный факт указан", "Контрольный факт"),
    "en": ("Which control fact is stated", "Control fact"),
    "uk": ("Який контрольний факт зазначено", "Контрольний факт"),
    "de": ("Welche Kontrolltatsache ist angegeben", "Kontrollfakt"),
    "ko": ("기록된 확인 사실은 무엇입니까", "확인 사실"),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(source_id: str, filename: str, language: str, kind: str, path: Path) -> CorpusSource:
    return CorpusSource(
        source_id=source_id,
        filename=filename,
        language_group=language,
        source_kind=kind,
        sha256=_sha256(path),
        revision="fixture-v1",
        observed_at="2026-08-15T00:00:00Z",
        current=kind != "superseded",
    )


def _evidence(
    *,
    task_id: str,
    source_id: str,
    quote: str,
    index: int,
    claim_id: str,
    contradicts: bool = False,
) -> GoldEvidence:
    return GoldEvidence(
        evidence_id=f"e-{task_id}-{source_id}",
        document_id=source_id,
        section_id=f"section-{index}",
        chunk_id=f"chunk-{source_id}-{index}",
        quote=quote,
        source_id=source_id,
        source_revision="fixture-v1",
        source_sha256="",
        supports_claim_ids=[] if contradicts else [claim_id],
        contradicts_claim_ids=[claim_id] if contradicts else [],
    )


def build_quality_fixture(corpus_dir: Path, *, overwrite: bool = False) -> dict[str, object]:
    """Write deterministic files in the supported formats and 220 tasks."""

    corpus_dir = corpus_dir.resolve()
    corpus_dir.mkdir(parents=True, exist_ok=True)
    if not overwrite and any(corpus_dir.iterdir()):
        raise FileExistsError(f"quality corpus directory is not empty: {corpus_dir}")
    sources: list[CorpusSource] = []
    tasks: list[EvalTask] = []
    for language_index, language in enumerate(QUALITY_LANGUAGES):
        question_prefix, fact_prefix = LANGUAGE_WORDS[language]
        control_id = f"src-{language}-control"
        table_id = f"src-{language}-table"
        old_id = f"src-{language}-old"
        current_id = f"src-{language}-current"
        control_path = corpus_dir / f"control-{language}.md"
        table_path = corpus_dir / f"table-{language}.csv"
        old_path = corpus_dir / f"old-{language}.md"
        current_path = corpus_dir / f"current-{language}.md"
        note_html_path = corpus_dir / f"note-{language}.html"
        note_txt_path = corpus_dir / f"note-{language}.txt"
        control_lines = [
            f"fixture-{language}-{index}: {fact_prefix} {index} подтверждён локальным набором."
            for index in range(1, 45)
        ]
        control_path.write_text("\n".join(control_lines) + "\n", encoding="utf-8")
        table_lines = ["key,value,language"] + [
            f"row-{index},{fact_prefix} {index},{language}" for index in range(1, 45)
        ]
        table_path.write_text("\n".join(table_lines) + "\n", encoding="utf-8")
        old_path.write_text(f"fixture-{language}-fresh: {fact_prefix} старая версия 1.\n", encoding="utf-8")
        current_path.write_text(f"fixture-{language}-fresh: {fact_prefix} новая версия 2.\n", encoding="utf-8")
        note_html_path.write_text(
            f"<html><body><p>{fact_prefix} {language} безопасная заметка.</p></body></html>\n",
            encoding="utf-8",
        )
        note_txt_path.write_text(f"{fact_prefix} {language} безопасная текстовая заметка.\n", encoding="utf-8")
        sources.extend(
            [
                _source(control_id, control_path.name, language, "controlled_text", control_path),
                _source(table_id, table_path.name, language, "controlled_table", table_path),
                _source(old_id, old_path.name, language, "superseded", old_path),
                _source(current_id, current_path.name, language, "current", current_path),
                _source(f"src-{language}-html", note_html_path.name, language, "controlled_html", note_html_path),
                _source(f"src-{language}-txt", note_txt_path.name, language, "controlled_text", note_txt_path),
            ]
        )
        for family_index, family in enumerate(QUALITY_FAMILIES):
            for item_index in range(1, 5):
                task_id = f"p01-{family}-{language}-{item_index}"
                claim_id = f"claim-{task_id}"
                fact_number = family_index * 4 + item_index
                quote = f"fixture-{language}-{fact_number}: {fact_prefix} {fact_number} подтверждён локальным набором."
                source_ids = [control_id]
                required_source_ids = [control_id]
                evidence = [
                    _evidence(task_id=task_id, source_id=control_id, quote=quote, index=fact_number, claim_id=claim_id)
                ]
                outcome = "answered"
                expected_answer = f"{fact_prefix} {fact_number}"
                if family == "table_lookup":
                    source_ids = [table_id]
                    required_source_ids = [table_id]
                    evidence = [
                        _evidence(
                            task_id=task_id,
                            source_id=table_id,
                            quote=f"row-{fact_number},{fact_prefix} {fact_number},{language}",
                            index=fact_number,
                            claim_id=claim_id,
                        )
                    ]
                elif family == "cross_source":
                    source_ids = [control_id, table_id]
                    required_source_ids = [control_id, table_id]
                    evidence.append(
                        _evidence(
                            task_id=task_id,
                            source_id=table_id,
                            quote=f"row-{fact_number},{fact_prefix} {fact_number},{language}",
                            index=fact_number,
                            claim_id=claim_id,
                        )
                    )
                elif family == "freshness":
                    source_ids = [old_id, current_id]
                    required_source_ids = [current_id]
                    evidence = [
                        _evidence(
                            task_id=task_id,
                            source_id=current_id,
                            quote=f"fixture-{language}-fresh: {fact_prefix} новая версия 2.",
                            index=fact_number,
                            claim_id=claim_id,
                        )
                    ]
                elif family == "conflicting":
                    source_ids = [old_id, current_id]
                    required_source_ids = [old_id, current_id]
                    outcome = "conflicting"
                    evidence = [
                        _evidence(
                            task_id=task_id,
                            source_id=old_id,
                            quote=f"fixture-{language}-fresh: {fact_prefix} старая версия 1.",
                            index=fact_number,
                            claim_id=claim_id,
                            contradicts=True,
                        ),
                        _evidence(
                            task_id=task_id,
                            source_id=current_id,
                            quote=f"fixture-{language}-fresh: {fact_prefix} новая версия 2.",
                            index=fact_number,
                            claim_id=claim_id,
                        ),
                    ]
                    expected_answer = "В источниках есть противоречие"
                elif family == "not_found_in_scope":
                    outcome = "not_found_in_scope"
                    source_ids = [control_id]
                    required_source_ids = []
                    evidence = []
                    expected_answer = "Сведения не найдены в выбранном наборе"
                elif family == "partial":
                    outcome = "partial"
                    expected_answer = f"Частично подтверждено: {fact_prefix} {fact_number}"
                question = f"{question_prefix} {fact_number} для набора {language}?"
                tasks.append(
                    EvalTask(
                        task_id=task_id,
                        question=question,
                        task_family=family,  # type: ignore[arg-type]
                        reference_answer=expected_answer,
                        accepted_answers=[expected_answer],
                        unanswerable=outcome == "not_found_in_scope",
                        expected_mode="unanswerable" if outcome == "not_found_in_scope" else "normal_sufficient",
                        gold_page_ids=source_ids,
                        gold_section_ids=[item.section_id for item in evidence],
                        gold_chunk_ids=[item.chunk_id for item in evidence],
                        gold_evidence=evidence,
                        reasoning_path=source_ids if family in {"multi_hop", "cross_source"} else [],
                        generator_alias="generator_main",
                        verifier_alias="verifier",
                        zim_checksum="",
                        snapshot_id="quality-fixture-v1",
                        index_version="",
                        retrieval_profile_hash="",
                        language=language,
                        language_group=language,
                        evaluation_schema_version="search_quality_eval_v1",
                        expected_outcome=outcome,  # type: ignore[arg-type]
                        source_ids=source_ids,
                        required_source_ids=required_source_ids,
                        forbidden_source_ids=[old_id] if family == "freshness" else [],
                        expected_claims=(
                            [
                                ExpectedClaim(
                                    claim_id=claim_id,
                                    statement=expected_answer,
                                    accepted_answers=[expected_answer],
                                    status="missing",
                                )
                            ]
                            if outcome == "not_found_in_scope"
                            else [
                                ExpectedClaim(
                                    claim_id=claim_id,
                                    statement=expected_answer,
                                    accepted_answers=[expected_answer],
                                    status="conflicting" if outcome == "conflicting" else "supported",
                                    supports_evidence_ids=[
                                        item.evidence_id for item in evidence if not item.contradicts_claim_ids
                                    ],
                                    contradicts_evidence_ids=[
                                        item.evidence_id for item in evidence if item.contradicts_claim_ids
                                    ],
                                ),
                                *(
                                    [
                                        ExpectedClaim(
                                            claim_id=f"{claim_id}-missing",
                                            statement="Дополнительная часть ответа отсутствует",
                                            status="missing",
                                        )
                                    ]
                                    if outcome == "partial"
                                    else []
                                ),
                            ]
                        ),
                        scope_review=ScopeReview(
                            reviewed=True,
                            reviewed_by="fixture-author",
                            reviewed_at="2026-08-15T00:00:00Z",
                            source_ids=source_ids,
                            checked_source_count=len(source_ids),
                            notes=(
                                "Deterministic local fixture; verify against approved corpus "
                                "before a production baseline."
                            ),
                        ),
                        reviewed_by="fixture-author",
                        reviewed_at="2026-08-15T00:00:00Z",
                        review_notes=["deterministic_fixture"],
                        split="dev" if language_index < 4 and item_index == 1 else "test",
                        source_document_name=source_ids[0],
                    )
                )
    write_json(
        corpus_dir / "sources.json",
        {"suite": "p0-search-quality-v1", "sources": [source.model_dump(mode="json") for source in sources]},
    )
    source_hashes = {source.source_id: source.sha256 for source in sources}
    for task in tasks:
        for task_evidence in task.gold_evidence:
            if task_evidence.source_id in source_hashes:
                task_evidence.source_sha256 = source_hashes[task_evidence.source_id]
    write_jsonl(corpus_dir / "tasks.jsonl", tasks)
    return {"corpus_dir": str(corpus_dir), "source_count": len(sources), "task_count": len(tasks)}
