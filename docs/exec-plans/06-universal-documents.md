# ExecPlan 06 — Universal document ingestion

## Outcome

Users can upload supported documents, preview canonical structure/chunks and publish a versioned index through isolated Docling/Tika workers.

## In scope

- upload sessions and object storage;
- MIME/security limits;
- Docling primary parser and Tika fallback;
- canonical node model;
- general/book/paper/presentation/spreadsheet/scanned templates;
- preview and reprocess/reindex;
- degraded parse state;
- parser isolation and failure tests.

## Out of scope

Universal autonomous correctness for every file type and visual template studio.

## Acceptance criteria

- PDF, DOCX, PPTX, XLSX, image and one Tika fallback fixture pass versioned ingestion;
- zip bomb/path traversal/oversize fixtures are rejected;
- parser timeout does not block queue;
- failed document does not publish partial index;
- re-chunking from canonical artifact avoids repeat parse/OCR.

## Validation

```bash
make test-unit TEST=document-model
make test-integration TEST=parsers
make test-e2e TEST=document-ingestion
```

## Progress

- [ ] Plan refined.
- [ ] Implemented.
- [ ] Reviewed.
