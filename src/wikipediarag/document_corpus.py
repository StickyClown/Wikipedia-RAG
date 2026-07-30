from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, cast
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

DOCUMENT_CORPUS_SCHEMA_VERSION = "document_corpus_manifest_v1"
SYNTHETIC_CORPUS_VERSION = "synthetic_document_corpus_v1"

CorpusOutcome = Literal["completed", "failed", "session_rejected", "complete_rejected"]


@dataclass(frozen=True)
class DocumentCorpusItem:
    id: str
    filename: str
    content_type: str
    parser_profile: str
    expected_outcome: CorpusOutcome
    license: str
    source_id: str
    expected_language: str | None = None
    expected_document_date: str | None = None
    expected_parser_route: str | None = None
    expected_error_code: str | None = None
    retrieval_query: str | None = None
    content: bytes | None = None
    url: str | None = None
    sha256: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def is_external(self) -> bool:
        return self.content is None

    def expected_sha256(self) -> str:
        if self.sha256 is not None:
            return self.sha256.lower()
        if self.content is None:
            raise ValueError(f"external corpus item {self.id} has no sha256")
        return hashlib.sha256(self.content).hexdigest()

    def expected_size(self) -> int:
        if self.content is None:
            raise ValueError(f"external corpus item {self.id} has no local bytes")
        return len(self.content)

    def report_metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "content_type": self.content_type,
            "parser_profile": self.parser_profile,
            "expected_outcome": self.expected_outcome,
            "expected_language": self.expected_language,
            "expected_document_date": self.expected_document_date,
            "expected_parser_route": self.expected_parser_route,
            "expected_error_code": self.expected_error_code,
            "source_id": self.source_id,
            "license": self.license,
            "url": self.url,
            "sha256": self.sha256,
            "metadata": self.metadata or {},
        }


def synthetic_document_corpus(
    *,
    fixture_set: str = "standard",
    include_negative: bool = True,
) -> list[DocumentCorpusItem]:
    positives = _synthetic_positive_items(include_office=fixture_set == "full")
    if fixture_set == "smoke":
        positives = [item for item in positives if item.id in {"synthetic-pdf-legal", "synthetic-csv-ru"}]
    if not include_negative:
        return positives
    negatives = _synthetic_negative_items()
    if fixture_set == "smoke":
        negatives = [item for item in negatives if item.id in {"negative-remote-html"}]
    return [*positives, *negatives]


def load_manifest_corpus(path: Path, *, include_disabled: bool = False) -> list[DocumentCorpusItem]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != DOCUMENT_CORPUS_SCHEMA_VERSION:
        raise ValueError(f"unsupported document corpus manifest schema: {payload.get('schema_version')}")
    documents = payload.get("documents")
    if not isinstance(documents, list):
        raise ValueError("document corpus manifest must contain a documents array")
    items: list[DocumentCorpusItem] = []
    for raw in documents:
        if not isinstance(raw, dict):
            raise ValueError("document corpus manifest documents must be objects")
        if not bool(raw.get("enabled", True)) and not include_disabled:
            continue
        item = _manifest_item(raw)
        items.append(item)
    return items


def materialize_corpus_item(item: DocumentCorpusItem, *, data: bytes) -> DocumentCorpusItem:
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != item.expected_sha256():
        raise ValueError(f"corpus item {item.id} checksum mismatch: {actual_sha256} != {item.expected_sha256()}")
    return replace(item, content=data, sha256=actual_sha256)


def corpus_summary(items: Iterable[DocumentCorpusItem]) -> dict[str, Any]:
    by_outcome: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_extension: dict[str, int] = {}
    for item in items:
        by_outcome[item.expected_outcome] = by_outcome.get(item.expected_outcome, 0) + 1
        by_source[item.source_id] = by_source.get(item.source_id, 0) + 1
        extension = "." + item.filename.rsplit(".", 1)[-1].casefold() if "." in item.filename else ""
        by_extension[extension] = by_extension.get(extension, 0) + 1
    return {"by_outcome": by_outcome, "by_source": by_source, "by_extension": by_extension}


def _synthetic_positive_items(*, include_office: bool) -> list[DocumentCorpusItem]:
    items = [
        _item(
            "synthetic-txt-ru",
            "synthetic-legal-ru.txt",
            "text/plain",
            (
                "Проверочный юридический документ CorpusCaseTxtRu. "
                "Дата документа 2026-07-29. Стороны согласовали порядок оказания услуг."
            ).encode(),
            expected_language="ru",
            expected_parser_route="local_text_adapter",
            query="CorpusCaseTxtRu 2026-07-29",
        ),
        _item(
            "synthetic-md-en",
            "synthetic-contract-en.md",
            "text/markdown",
            b"# CorpusCaseMdEn Agreement\n\nEffective date 2026-07-29. The supplier must preserve records.",
            expected_language="en",
            expected_parser_route="local_text_adapter",
            query="CorpusCaseMdEn 2026-07-29",
        ),
        _item(
            "synthetic-html-en",
            "synthetic-notice.html",
            "text/html",
            (
                b"<html><body><h1>CorpusCaseHtmlEn Notice</h1>"
                b"<p>Document date 2026-07-29. Local legal notice without remote resources.</p>"
                b"</body></html>"
            ),
            expected_language="en",
            expected_parser_route="local_text_adapter",
            query="CorpusCaseHtmlEn 2026-07-29",
        ),
        _item(
            "synthetic-csv-ru",
            "synthetic-metadata-ru.csv",
            "text/csv",
            (
                "case_id,document_date,language,note\n"
                "CorpusCaseCsvRu,2026-07-29,ru,"
                "русский договор содержит условие срок уведомление ответственность исполнение приемка\n"
            ).encode(),
            expected_language="ru",
            expected_parser_route="local_csv_adapter",
            query="CorpusCaseCsvRu 2026-07-29",
        ),
        _item(
            "synthetic-tsv-en",
            "synthetic-rows-en.tsv",
            "text/tab-separated-values",
            b"case_id\tdocument_date\tlanguage\tnote\nCorpusCaseTsvEn\t2026-07-29\ten\tlocal row provenance check\n",
            expected_language="en",
            expected_parser_route="local_tsv_adapter",
            query="CorpusCaseTsvEn 2026-07-29",
        ),
        _item(
            "synthetic-json-en",
            "synthetic-claim-en.json",
            "application/json",
            json.dumps(
                {
                    "case_id": "CorpusCaseJsonEn",
                    "document_date": "2026-07-29",
                    "language": "en",
                    "clause": "The agreement terminates after written notice.",
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode(),
            expected_language="en",
            expected_parser_route="local_json_adapter",
            query="CorpusCaseJsonEn 2026-07-29",
        ),
        _item(
            "synthetic-jsonl-ru",
            "synthetic-events-ru.jsonl",
            "application/x-ndjson",
            (
                '{"case_id":"CorpusCaseJsonlRu","document_date":"2026-07-29","language":"ru"}\n'
                '{"note":"Русский договор проверяет дату язык источник строку ответственность уведомление"}\n'
            ).encode(),
            expected_language="ru",
            expected_parser_route="local_jsonl_adapter",
            query="CorpusCaseJsonlRu 2026-07-29",
        ),
        _item(
            "synthetic-pdf-legal",
            "synthetic-contract.pdf",
            "application/pdf",
            _minimal_pdf("CorpusCasePdfEn document date 2026-07-29 table clause"),
            expected_language="en",
            expected_parser_route="docling",
            query="CorpusCasePdfEn 2026-07-29",
        ),
    ]
    if include_office:
        items.extend(
            [
                _item(
                    "synthetic-docx-en",
                    "synthetic-contract.docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    _minimal_docx("CorpusCaseDocxEn effective date 2026-07-29 confidentiality clause"),
                    expected_language="en",
                    query="CorpusCaseDocxEn 2026-07-29",
                ),
                _item(
                    "synthetic-pptx-en",
                    "synthetic-briefing.pptx",
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    _minimal_pptx("CorpusCasePptxEn briefing date 2026-07-29 approval slide"),
                    expected_language="en",
                    query="CorpusCasePptxEn 2026-07-29",
                ),
                _item(
                    "synthetic-xlsx-en",
                    "synthetic-register.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    _minimal_xlsx("CorpusCaseXlsxEn", "2026-07-29", "formula provenance"),
                    expected_language="en",
                    query="CorpusCaseXlsxEn 2026-07-29",
                ),
            ]
        )
    return items


def _synthetic_negative_items() -> list[DocumentCorpusItem]:
    deep_json = '{"a":' * 40 + "1" + "}" * 40
    return [
        _item(
            "negative-zero-byte",
            "empty.txt",
            "text/plain",
            b"",
            expected_outcome="session_rejected",
            expected_error_code="request_validation_error",
        ),
        _item(
            "negative-archive",
            "archive.zip",
            "application/zip",
            _zip_bytes({"payload.txt": "archives are rejected"}),
            expected_outcome="failed",
            expected_error_code="ARCHIVE_NOT_ALLOWED",
        ),
        _item(
            "negative-renamed-exe",
            "renamed-report.txt",
            "text/plain",
            b"MZrenamed executable CorpusCaseRenamedExe",
            expected_outcome="failed",
            expected_error_code="RENAMED_EXECUTABLE",
        ),
        _item(
            "negative-signature-mismatch",
            "mismatch.txt",
            "text/plain",
            _minimal_pdf("CorpusCaseMismatch"),
            expected_outcome="failed",
            expected_error_code="SIGNATURE_MISMATCH",
        ),
        _item(
            "negative-encrypted-pdf",
            "encrypted.pdf",
            "application/pdf",
            b"%PDF-1.4\n1 0 obj\n<</Encrypt 2 0 R>>\nendobj\n%%EOF\n",
            expected_outcome="failed",
            expected_error_code="ENCRYPTED_PDF",
        ),
        _item(
            "negative-macro-office",
            "macro.docm",
            "application/vnd.ms-word.document.macroEnabled.12",
            _minimal_docx("CorpusCaseMacroRejected"),
            expected_outcome="failed",
            expected_error_code="MACRO_OFFICE_NOT_ALLOWED",
        ),
        _item(
            "negative-deep-json",
            "deep.json",
            "application/json",
            deep_json.encode("utf-8"),
            expected_outcome="failed",
            expected_error_code="JSON_TOO_DEEP",
        ),
        _item(
            "negative-remote-html",
            "remote.html",
            "text/html",
            b'<html><body><img src="https://example.invalid/pixel.png">CorpusCaseRemoteHtml</body></html>',
            expected_outcome="failed",
            expected_error_code="REMOTE_HTML_RESOURCE",
        ),
        _item(
            "negative-size-mismatch",
            "size-mismatch.txt",
            "text/plain",
            b"abc",
            expected_outcome="complete_rejected",
            expected_error_code="uploaded object size mismatch",
            metadata={"declared_size_bytes": 4},
        ),
        _item(
            "negative-checksum-mismatch",
            "checksum-mismatch.txt",
            "text/plain",
            b"CorpusCaseChecksumMismatch 2026-07-29",
            expected_outcome="failed",
            expected_error_code="CHECKSUM_MISMATCH",
            metadata={"declared_sha256": hashlib.sha256(b"different bytes").hexdigest()},
        ),
    ]


def _item(
    item_id: str,
    filename: str,
    content_type: str,
    content: bytes,
    *,
    expected_language: str | None = None,
    expected_parser_route: str | None = None,
    expected_outcome: CorpusOutcome = "completed",
    expected_error_code: str | None = None,
    query: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DocumentCorpusItem:
    return DocumentCorpusItem(
        id=item_id,
        filename=filename,
        content_type=content_type,
        parser_profile="standard",
        expected_outcome=expected_outcome,
        expected_language=expected_language,
        expected_document_date="2026-07-29" if expected_outcome == "completed" else None,
        expected_parser_route=expected_parser_route,
        expected_error_code=expected_error_code,
        retrieval_query=query,
        content=content,
        license="generated-test-fixture",
        source_id="synthetic",
        metadata=metadata,
    )


def _manifest_item(raw: dict[str, Any]) -> DocumentCorpusItem:
    required = ("id", "filename", "content_type", "url", "sha256", "license", "source_id")
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise ValueError(f"document corpus manifest item is missing required fields: {missing}")
    sha256 = str(raw["sha256"]).lower()
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise ValueError(f"document corpus manifest item {raw['id']} has invalid sha256")
    return DocumentCorpusItem(
        id=str(raw["id"]),
        filename=str(raw["filename"]),
        content_type=str(raw["content_type"]),
        parser_profile=str(raw.get("parser_profile") or "standard"),
        expected_outcome=_manifest_outcome(raw.get("expected_outcome")),
        expected_language=_optional_str(raw.get("expected_language")),
        expected_document_date=_optional_str(raw.get("expected_document_date")),
        expected_parser_route=_optional_str(raw.get("expected_parser_route")),
        expected_error_code=_optional_str(raw.get("expected_error_code")),
        retrieval_query=_optional_str(raw.get("retrieval_query")),
        content=None,
        url=str(raw["url"]),
        sha256=sha256,
        license=str(raw["license"]),
        source_id=str(raw["source_id"]),
        metadata=dict(raw.get("metadata") or {}),
    )


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _manifest_outcome(value: Any) -> CorpusOutcome:
    outcome = str(value or "completed")
    if outcome not in {"completed", "failed", "session_rejected", "complete_rejected"}:
        raise ValueError(f"unsupported corpus expected_outcome: {outcome}")
    return cast(CorpusOutcome, outcome)


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = ZipInfo(name)
            info.date_time = (2026, 7, 29, 0, 0, 0)
            archive.writestr(info, files[name])
    return buffer.getvalue()


def _minimal_pdf(text: str) -> bytes:
    safe_text = "".join(char if 32 <= ord(char) < 127 and char not in "\\()" else " " for char in text)
    stream = f"BT /F1 12 Tf 40 100 Td ({safe_text}) Tj ET".encode("ascii")
    objects = [
        b"1 0 obj\n<</Type /Catalog /Pages 2 0 R>>\nendobj\n",
        b"2 0 obj\n<</Type /Pages /Kids [3 0 R] /Count 1>>\nendobj\n",
        (
            b"3 0 obj\n<</Type /Page /Parent 2 0 R /MediaBox [0 0 420 144] "
            b"/Resources <</Font <</F1 5 0 R>>>> /Contents 4 0 R>>\nendobj\n"
        ),
        b"4 0 obj\n<</Length " + str(len(stream)).encode("ascii") + b">>\nstream\n" + stream + b"\nendstream\nendobj\n",
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


def _minimal_docx(text: str) -> bytes:
    escaped = xml_escape(text)
    return _zip_bytes(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/>'
                "</Relationships>"
            ),
            "word/document.xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{escaped}</w:t></w:r></w:p></w:body></w:document>"
            ),
        }
    )


def _minimal_pptx(text: str) -> bytes:
    escaped = xml_escape(text)
    return _zip_bytes(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/ppt/presentation.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
                '<Override PartName="/ppt/slides/slide1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="ppt/presentation.xml"/>'
                "</Relationships>"
            ),
            "ppt/presentation.xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>'
            ),
            "ppt/_rels/presentation.xml.rels": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
                'Target="slides/slide1.xml"/>'
                "</Relationships>"
            ),
            "ppt/slides/slide1.xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                "<p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/>"
                "<p:sp><p:txBody><a:bodyPr/><a:lstStyle/>"
                f"<a:p><a:r><a:t>{escaped}</a:t></a:r></a:p>"
                "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
            ),
        }
    )


def _minimal_xlsx(case_id: str, document_date: str, note: str) -> bytes:
    return _zip_bytes(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="xl/workbook.xml"/>'
                "</Relationships>"
            ),
            "xl/workbook.xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Cases" sheetId="1" r:id="rId1"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                'Target="worksheets/sheet1.xml"/>'
                "</Relationships>"
            ),
            "xl/worksheets/sheet1.xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                "<sheetData>"
                '<row r="1"><c r="A1" t="inlineStr"><is><t>case_id</t></is></c>'
                '<c r="B1" t="inlineStr"><is><t>document_date</t></is></c>'
                '<c r="C1" t="inlineStr"><is><t>note</t></is></c></row>'
                f'<row r="2"><c r="A2" t="inlineStr"><is><t>{xml_escape(case_id)}</t></is></c>'
                f'<c r="B2" t="inlineStr"><is><t>{xml_escape(document_date)}</t></is></c>'
                f'<c r="C2" t="inlineStr"><is><t>{xml_escape(note)}</t></is></c></row>'
                "</sheetData></worksheet>"
            ),
        }
    )
