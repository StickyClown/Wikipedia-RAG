from __future__ import annotations

from wikipediarag.ingestion import _with_publication_status
from wikipediarag.repository import fetch_chunk_by_id, fetch_chunks_for_dense_scan
from wikipediarag.wiki_dump import Chunk


def _chunk() -> Chunk:
    return Chunk(
        id="chunk:1",
        document_id="doc:1",
        page_id=1,
        revision_id=1,
        title="Hardening",
        section_path=("Hardening",),
        content="tenant isolation content",
        source_uri="upload://doc:1",
        source_url="http://localhost/documents/doc:1",
        embedding=[0.1, 0.2],
        content_hash="hash",
        parent_chunk_id=None,
        prev_chunk_id=None,
        next_chunk_id=None,
        metadata={"document_version_id": "docv:1"},
    )


def test_publication_status_helper_does_not_mutate_original_chunks() -> None:
    chunk = _chunk()

    staged = _with_publication_status([chunk], "staged")
    published = _with_publication_status([chunk], "published")

    assert chunk.metadata == {"document_version_id": "docv:1"}
    assert staged[0].metadata["publication_status"] == "staged"
    assert published[0].metadata["publication_status"] == "published"


def test_retrieval_chunk_loaders_only_return_published_chunks() -> None:
    dense_sql = str(fetch_chunks_for_dense_scan.__code__.co_consts)
    by_id_sql = str(fetch_chunk_by_id.__code__.co_consts)

    assert "publication_status = 'published'" in dense_sql
    assert "publication_status = 'published'" in by_id_sql
