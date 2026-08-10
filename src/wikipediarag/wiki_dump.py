from __future__ import annotations

import bz2
import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from wikipediarag.embedding import embed_text
from wikipediarag.ids import scoped_id, stable_hash
from wikipediarag.wikitext import extract_sections

MW_NS = "http://www.mediawiki.org/xml/export-0.11/"
NS = {"mw": MW_NS}
REDIRECT_RE = re.compile(r"^\s*#(?:REDIRECT|перенаправление)\s*\[\[(.*?)\]\]", re.IGNORECASE)


@dataclass(frozen=True)
class IndexEntry:
    offset: int
    page_id: int
    title: str


@dataclass(frozen=True)
class StreamGroup:
    offset: int
    page_ids: tuple[int, ...]
    titles: tuple[str, ...]


@dataclass(frozen=True)
class WikiPage:
    title: str
    namespace: int
    page_id: int
    revision_id: int
    timestamp: str
    redirect_target: str | None
    wikitext: str


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    page_id: int
    revision_id: int
    title: str
    section_path: tuple[str, ...]
    content: str
    parent_chunk_id: str | None
    prev_chunk_id: str | None
    next_chunk_id: str | None
    source_uri: str
    source_url: str
    content_hash: str
    embedding: list[float]
    metadata: dict[str, object] = field(default_factory=dict)


def validate_index(index_path: Path, xml_path: Path, sample_unique_offsets: int = 32) -> dict[str, int]:
    if not xml_path.exists():
        raise ValueError("XML dump file does not exist")
    if not index_path.exists():
        raise ValueError("index file does not exist")
    with xml_path.open("rb") as xml_file:
        if xml_file.read(4) != b"BZh9":
            raise ValueError("XML dump is not a bzip2 multistream file")
    with index_path.open("rb") as index_file:
        if index_file.read(4) != b"BZh9":
            raise ValueError("index is not a bzip2 file")

    line_count = 0
    unique_count = 0
    previous_offset = -1
    previous_unique: int | None = None
    samples: list[int] = []
    min_offset: int | None = None
    max_offset: int | None = None
    xml_size = xml_path.stat().st_size

    for entry in iter_index_entries(index_path):
        line_count += 1
        if entry.offset < previous_offset:
            raise ValueError(f"index offsets must be monotonic non-decreasing: {entry.offset} after {previous_offset}")
        previous_offset = entry.offset
        min_offset = entry.offset if min_offset is None else min(min_offset, entry.offset)
        max_offset = entry.offset if max_offset is None else max(max_offset, entry.offset)
        if entry.offset != previous_unique:
            unique_count += 1
            previous_unique = entry.offset
            if len(samples) < sample_unique_offsets:
                samples.append(entry.offset)

    if line_count == 0:
        raise ValueError("index is empty")
    if max_offset is None or max_offset >= xml_size:
        raise ValueError("index offset points outside XML dump")

    with xml_path.open("rb") as file:
        for offset in samples:
            file.seek(offset)
            if file.read(4) != b"BZh9":
                raise ValueError(f"unique stream offset {offset} does not point to bzip2 data")

    return {
        "line_count": line_count,
        "unique_stream_count": unique_count,
        "min_offset": min_offset or 0,
        "max_offset": max_offset,
    }


def iter_index_entries(index_path: Path) -> Iterator[IndexEntry]:
    with bz2.open(index_path, "rt", encoding="utf-8", errors="strict") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.rstrip("\n")
            parts = line.split(":", 2)
            if len(parts) != 3:
                raise ValueError(f"invalid index line {line_number}")
            try:
                offset = int(parts[0])
                page_id = int(parts[1])
            except ValueError as exc:
                raise ValueError(f"invalid numeric fields at index line {line_number}") from exc
            yield IndexEntry(offset=offset, page_id=page_id, title=parts[2])


def iter_stream_groups(index_path: Path, limit: int | None = None) -> Iterator[StreamGroup]:
    current_offset: int | None = None
    page_ids: list[int] = []
    titles: list[str] = []
    yielded_pages = 0
    for entry in iter_index_entries(index_path):
        if current_offset is None:
            current_offset = entry.offset
        if entry.offset != current_offset:
            yield StreamGroup(current_offset, tuple(page_ids), tuple(titles))
            yielded_pages += len(page_ids)
            if limit is not None and yielded_pages >= limit:
                return
            current_offset = entry.offset
            page_ids = []
            titles = []
        page_ids.append(entry.page_id)
        titles.append(entry.title)
    if current_offset is not None and (limit is None or yielded_pages < limit):
        yield StreamGroup(current_offset, tuple(page_ids), tuple(titles))


def read_bzip2_stream(xml_path: Path, offset: int, chunk_size: int = 1024 * 1024) -> bytes:
    decompressor = bz2.BZ2Decompressor()
    output = io.BytesIO()
    with xml_path.open("rb") as file:
        file.seek(offset)
        while not decompressor.eof:
            data = file.read(chunk_size)
            if not data:
                break
            output.write(decompressor.decompress(data))
    if not decompressor.eof:
        raise ValueError(f"bzip2 stream at offset {offset} ended before EOF marker")
    return output.getvalue()


def parse_pages_fragment(fragment: bytes) -> list[WikiPage]:
    fragment = fragment.replace(b"</mediawiki>", b"")
    wrapped = b'<root xmlns="' + MW_NS.encode("ascii") + b'">' + fragment + b"</root>"
    root = ET.fromstring(wrapped)  # noqa: S314 - local Wikimedia dump, no external XML entity use.
    pages: list[WikiPage] = []
    for page_el in root.findall("mw:page", NS):
        title = page_el.findtext("mw:title", default="", namespaces=NS)
        namespace = int(page_el.findtext("mw:ns", default="-1", namespaces=NS))
        page_id = int(page_el.findtext("mw:id", default="0", namespaces=NS))
        redirect_el = page_el.find("mw:redirect", NS)
        revision_el = page_el.find("mw:revision", NS)
        if revision_el is None:
            continue
        revision_id = int(revision_el.findtext("mw:id", default="0", namespaces=NS))
        timestamp = revision_el.findtext("mw:timestamp", default="", namespaces=NS)
        text_el = revision_el.find("mw:text", NS)
        wikitext = text_el.text if text_el is not None and text_el.text is not None else ""
        redirect_target = redirect_el.attrib.get("title") if redirect_el is not None else None
        if redirect_target is None:
            match = REDIRECT_RE.match(wikitext)
            redirect_target = match.group(1) if match else None
        pages.append(
            WikiPage(
                title=title,
                namespace=namespace,
                page_id=page_id,
                revision_id=revision_id,
                timestamp=timestamp,
                redirect_target=redirect_target,
                wikitext=wikitext,
            )
        )
    return pages


def chunks_for_page(
    page: WikiPage,
    snapshot_id: str,
    dimensions: int,
    target_words: int = 220,
    max_words: int = 360,
    *,
    tenant_id: str | None = None,
    knowledge_base_id: str | None = None,
) -> list[Chunk]:
    if page.namespace != 0:
        return []
    document_id = (
        scoped_id(
            "wiki-document",
            page.page_id,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            source_type="wikipedia_xml",
            snapshot_id=snapshot_id,
        )
        if tenant_id and knowledge_base_id
        else f"wiki:{snapshot_id}:{page.page_id}"
    )
    if page.redirect_target:
        content = f"{page.title} перенаправляет на {page.redirect_target}."
        content_hash = stable_hash([snapshot_id, page.page_id, page.revision_id, content])
        chunk_id = (
            scoped_id(
                "wiki-chunk",
                [page.page_id, page.revision_id, "redirect", content_hash],
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                source_type="wikipedia_xml",
                snapshot_id=snapshot_id,
            )
            if tenant_id and knowledge_base_id
            else f"wiki:{stable_hash([snapshot_id, page.page_id, page.revision_id, 'redirect', content_hash], 32)}"
        )
        return [
            Chunk(
                id=chunk_id,
                document_id=document_id,
                page_id=page.page_id,
                revision_id=page.revision_id,
                title=page.title,
                section_path=(page.title,),
                content=content,
                parent_chunk_id=None,
                prev_chunk_id=None,
                next_chunk_id=None,
                source_uri=f"wikipedia://{snapshot_id}/{page.page_id}",
                source_url=f"https://ru.wikipedia.org/wiki/{page.title.replace(' ', '_')}",
                content_hash=content_hash,
                embedding=embed_text(f"{page.title}\n{content}", dimensions),
                metadata={
                    "source_type": "wikipedia_xml",
                    "source_document_id": f"wiki:{snapshot_id}:{page.page_id}",
                    "source_chunk_id": chunk_id,
                    "snapshot_id": snapshot_id,
                    "chunk_ordinal": 1,
                    "locator": {"page_id": page.page_id, "section_index": 1, "chunk_index": 1},
                },
            )
        ]

    chunks: list[Chunk] = []
    for section in extract_sections(page.title, page.wikitext):
        words = section.clean_text.split()
        if not words:
            continue
        start = 0
        sequence = 0
        parent_scope = [
            tenant_id or "legacy",
            knowledge_base_id or "legacy",
            snapshot_id,
            page.page_id,
            page.revision_id,
            *section.path,
        ]
        parent_id = f"section:{stable_hash(parent_scope, 24)}"
        while start < len(words):
            end = min(start + max_words, len(words))
            if end - start > target_words:
                end = min(start + target_words, len(words))
            content = " ".join(words[start:end])
            content_hash = stable_hash([content])
            chunk_ordinal = len(chunks) + 1
            native_chunk_id = "wiki:" + stable_hash(
                [
                    snapshot_id,
                    page.page_id,
                    page.revision_id,
                    *section.path,
                    sequence,
                    content_hash,
                ],
                32,
            )
            chunk_id = (
                scoped_id(
                    "wiki-chunk",
                    native_chunk_id,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    source_type="wikipedia_xml",
                    snapshot_id=snapshot_id,
                )
                if tenant_id and knowledge_base_id
                else native_chunk_id
            )
            section_id = f"section:{stable_hash([snapshot_id, page.page_id, page.revision_id, *section.path], 24)}"
            chunks.append(
                Chunk(
                    id=chunk_id,
                    document_id=document_id,
                    page_id=page.page_id,
                    revision_id=page.revision_id,
                    title=page.title,
                    section_path=section.path,
                    content=content,
                    parent_chunk_id=parent_id,
                    prev_chunk_id=None,
                    next_chunk_id=None,
                    source_uri=f"wikipedia://{snapshot_id}/{page.page_id}",
                    source_url=f"https://ru.wikipedia.org/wiki/{page.title.replace(' ', '_')}",
                    content_hash=content_hash,
                    embedding=embed_text(f"{page.title}\n{' / '.join(section.path)}\n{content}", dimensions),
                    metadata={
                        "source_type": "wikipedia_xml",
                        "source_document_id": f"wiki:{snapshot_id}:{page.page_id}",
                        "source_chunk_id": native_chunk_id,
                        "snapshot_id": snapshot_id,
                        "chunk_ordinal": chunk_ordinal,
                        "section_id": section_id,
                        "locator": {
                            "page_id": page.page_id,
                            "section_index": len(chunks) + 1,
                            "chunk_index": sequence + 1,
                        },
                    },
                )
            )
            start = end
            sequence += 1
    return link_neighbors(chunks)


def link_neighbors(chunks: list[Chunk]) -> list[Chunk]:
    linked: list[Chunk] = []
    for index, chunk in enumerate(chunks):
        prev_id = chunks[index - 1].id if index > 0 else None
        next_id = chunks[index + 1].id if index + 1 < len(chunks) else None
        linked.append(
            Chunk(
                id=chunk.id,
                document_id=chunk.document_id,
                page_id=chunk.page_id,
                revision_id=chunk.revision_id,
                title=chunk.title,
                section_path=chunk.section_path,
                content=chunk.content,
                parent_chunk_id=chunk.parent_chunk_id,
                prev_chunk_id=prev_id,
                next_chunk_id=next_id,
                source_uri=chunk.source_uri,
                source_url=chunk.source_url,
                content_hash=chunk.content_hash,
                embedding=chunk.embedding,
                metadata=chunk.metadata,
            )
        )
    return linked
