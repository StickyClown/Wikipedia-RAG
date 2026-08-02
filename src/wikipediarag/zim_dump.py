from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import quote

from bs4 import BeautifulSoup

from wikipediarag.ids import stable_hash
from wikipediarag.wiki_dump import Chunk

ARTICLE_MIMETYPES = {"text/html", "text/plain", "text/html; charset=utf-8"}
SKIP_PREFIXES = ("-", "I/", "M/", "S/", "skin/", "media/", "static/", "assets/")
SKIP_PATHS = {"mainPage"}
SKIP_SUFFIXES = (
    ".css",
    ".js",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
)


@dataclass(frozen=True)
class ZimArchiveInfo:
    archive_id: str
    filename: str
    book_name: str
    title: str
    article_count: int
    entry_count: int


@dataclass(frozen=True)
class ZimRedirect:
    entry_index: int
    title: str
    zim_entry_path: str
    redirect_target: str


@dataclass(frozen=True)
class ZimPage:
    entry_index: int
    title: str
    zim_entry_path: str
    redirect_target: str | None
    html_or_text: str
    mimetype: str
    source_url: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ZimScanItem:
    entry_index: int
    page: ZimPage | None = None
    redirect: ZimRedirect | None = None
    skipped: bool = False


class ZimSection(TypedDict):
    path: list[str]
    text: str


class ZimArchiveAdapter:
    def __init__(self, zim_path: Path, *, public_base_url: str, book_name_override: str = "") -> None:
        self.zim_path = zim_path
        self.public_base_url = public_base_url
        self.book_name_override = book_name_override

    def iter_items(self, *, start_after_index: int = -1) -> Iterator[ZimScanItem]:
        from libzim.reader import Archive

        archive = Archive(self.zim_path)
        if not hasattr(archive, "_get_entry_by_id"):
            raise RuntimeError("installed libzim wheel does not expose Archive._get_entry_by_id")
        info = archive_info_from_archive(archive, self.zim_path, self.book_name_override)
        for entry_index in range(start_after_index + 1, int(archive.all_entry_count)):
            try:
                entry = archive._get_entry_by_id(entry_index)  # noqa: SLF001 - libzim exposes no public iterator in stubs.
            except (KeyError, RuntimeError, ValueError):
                yield ZimScanItem(entry_index=entry_index, skipped=True)
                continue
            path = str(entry.path)
            title = str(entry.title or path)
            if _is_service_path(path):
                yield ZimScanItem(entry_index=entry_index, skipped=True)
                continue
            if bool(entry.is_redirect):
                try:
                    target = entry.get_redirect_entry()
                    target_path = str(target.path)
                except (KeyError, RuntimeError, ValueError):
                    target_path = ""
                yield ZimScanItem(
                    entry_index=entry_index,
                    redirect=ZimRedirect(
                        entry_index=entry_index,
                        title=title,
                        zim_entry_path=path,
                        redirect_target=target_path,
                    ),
                )
                continue
            try:
                item = entry.get_item()
            except (KeyError, RuntimeError, ValueError):
                yield ZimScanItem(entry_index=entry_index, skipped=True)
                continue
            mimetype = str(item.mimetype or "")
            if not _is_article_item(path, mimetype):
                yield ZimScanItem(entry_index=entry_index, skipped=True)
                continue
            content = bytes(item.content).decode("utf-8", errors="replace")
            if len(content.strip()) < 40:
                yield ZimScanItem(entry_index=entry_index, skipped=True)
                continue
            metadata = {
                "zim_archive_id": info.archive_id,
                "zim_filename": info.filename,
                "zim_book_name": info.book_name,
                "zim_entry_path": path,
                "snapshot_id": info.archive_id,
                "source_type": "wikipedia_zim",
                "mimetype": mimetype,
                "entry_index": entry_index,
            }
            yield ZimScanItem(
                entry_index=entry_index,
                page=ZimPage(
                    entry_index=entry_index,
                    title=title,
                    zim_entry_path=path,
                    redirect_target=None,
                    html_or_text=content,
                    mimetype=mimetype,
                    source_url=build_kiwix_source_url(self.public_base_url, info.book_name, path),
                    metadata=metadata,
                ),
            )

    def info(self) -> ZimArchiveInfo:
        from libzim.reader import Archive

        archive = Archive(self.zim_path)
        return archive_info_from_archive(archive, self.zim_path, self.book_name_override)


def resolve_zim_path(zim_dir: Path, zim_filename: str = "", explicit_path: str | None = None) -> Path:
    if explicit_path:
        path = Path(explicit_path)
        if path.exists():
            return path
        raise FileNotFoundError(f"ZIM file does not exist: {path}")
    if zim_filename:
        path = zim_dir / zim_filename
        if path.exists():
            return path
        raise FileNotFoundError(f"ZIM file does not exist: {path}")
    candidates = sorted(zim_dir.glob("*.zim"))
    if not candidates:
        raise FileNotFoundError(f"no .zim file found in {zim_dir}")
    return candidates[0]


def archive_info_from_archive(archive: Any, zim_path: Path, book_name_override: str = "") -> ZimArchiveInfo:
    archive_id = str(archive.uuid)
    metadata_name = _decode_metadata(archive, "Name")
    metadata_title = _decode_metadata(archive, "Title")
    # kiwix-serve addresses a mounted archive by its filename stem, not ZIM's Name metadata.
    book_name = (book_name_override or zim_path.stem).strip("/")
    return ZimArchiveInfo(
        archive_id=archive_id,
        filename=zim_path.name,
        book_name=book_name,
        title=metadata_title or metadata_name or zim_path.stem,
        article_count=int(getattr(archive, "article_count", 0)),
        entry_count=int(getattr(archive, "all_entry_count", 0)),
    )


def build_kiwix_source_url(base_url: str, book_name: str, zim_entry_path: str) -> str:
    encoded_book = quote(book_name.strip("/"), safe="")
    encoded_path = quote(zim_entry_path.lstrip("/"), safe="/:@")
    return f"{base_url.rstrip('/')}/content/{encoded_book}/{encoded_path}"


def chunks_for_zim_page(
    page: ZimPage,
    *,
    snapshot_id: str,
    dimensions: int,
    child_tokens_min: int,
    child_tokens_max: int,
    parent_tokens_min: int,
    parent_tokens_max: int,
) -> list[Chunk]:
    sections = extract_zim_sections(page)
    chunks: list[Chunk] = []
    document_id = f"zim:{snapshot_id}:{stable_hash([page.zim_entry_path], 24)}"
    for section_index, section in enumerate(sections):
        words = section["text"].split()
        if not words:
            continue
        parent_text = " ".join(words[:parent_tokens_max])
        parent_hash = stable_hash([snapshot_id, page.zim_entry_path, section_index, parent_text], 24)
        parent_id = f"section:{parent_hash}"
        start = 0
        sequence = 0
        while start < len(words):
            end = min(start + child_tokens_max, len(words))
            if end - start < child_tokens_min and chunks:
                break
            content = " ".join(words[start:end])
            content_hash = stable_hash([content])
            chunk_ordinal = len(chunks) + 1
            chunk_id = "zim:" + stable_hash(
                [snapshot_id, page.zim_entry_path, section_index, sequence, content_hash],
                32,
            )
            section_path = tuple(str(item) for item in section["path"])
            section_id = f"section:{stable_hash([snapshot_id, page.zim_entry_path, *section_path], 24)}"
            metadata = {
                **page.metadata,
                "parent_text": parent_text,
                "parent_tokens": min(len(words), parent_tokens_max),
                "parent_tokens_min": parent_tokens_min,
                "child_tokens": len(content.split()),
                "chunk_ordinal": chunk_ordinal,
                "section_id": section_id,
                "locator": {
                    "entry_index": page.entry_index,
                    "section_index": section_index + 1,
                    "chunk_index": sequence + 1,
                },
            }
            chunks.append(
                Chunk(
                    id=chunk_id,
                    document_id=document_id,
                    page_id=page.entry_index,
                    revision_id=0,
                    title=page.title,
                    section_path=section_path,
                    content=content,
                    parent_chunk_id=parent_id,
                    prev_chunk_id=None,
                    next_chunk_id=None,
                    source_uri=f"zim://{snapshot_id}/{page.zim_entry_path}",
                    source_url=page.source_url,
                    content_hash=content_hash,
                    embedding=[0.0] * dimensions,
                    metadata=metadata,
                )
            )
            start = end
            sequence += 1
    return _link_neighbors(chunks)


def extract_zim_sections(page: ZimPage) -> list[ZimSection]:
    if page.mimetype.startswith("text/plain"):
        text = " ".join(page.html_or_text.split())
        return [{"path": [page.title], "text": text}] if text else []
    soup = BeautifulSoup(page.html_or_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    body = soup.body or soup
    current_path = [page.title]
    sections: list[ZimSection] = []
    buffers: dict[tuple[str, ...], list[str]] = {}
    for tag in body.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th"], recursive=True):
        text = " ".join(tag.get_text(" ", strip=True).split())
        if not text:
            continue
        if tag.name and tag.name.startswith("h"):
            level = max(1, min(int(tag.name[1]), 6))
            current_path = [*current_path[:level], text]
            continue
        key = tuple(current_path)
        buffers.setdefault(key, []).append(text)
    for path, parts in buffers.items():
        text = " ".join(parts)
        if text:
            sections.append({"path": list(path), "text": text})
    if sections:
        return sections
    text = " ".join(body.get_text(" ", strip=True).split())
    return [{"path": [page.title], "text": text}] if text else []


def _decode_metadata(archive: Any, name: str) -> str:
    try:
        return bytes(archive.get_metadata(name)).decode("utf-8", errors="replace").strip()
    except (KeyError, RuntimeError, ValueError):
        return ""


def _is_service_path(path: str) -> bool:
    normalized = path.lstrip("/")
    return (
        normalized in SKIP_PATHS
        or normalized.startswith(SKIP_PREFIXES)
        or normalized.endswith(SKIP_SUFFIXES)
        or "/-/" in normalized
    )


def _is_article_item(path: str, mimetype: str) -> bool:
    normalized_mimetype = mimetype.split(";", 1)[0].strip().lower()
    if normalized_mimetype not in ARTICLE_MIMETYPES and normalized_mimetype not in {"text/html", "text/plain"}:
        return False
    return not _is_service_path(path)


def _link_neighbors(chunks: list[Chunk]) -> list[Chunk]:
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
