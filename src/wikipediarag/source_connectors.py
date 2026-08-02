from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import mimetypes
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from wikipediarag.document_ingestion import sha256_hex
from wikipediarag.ids import stable_hash

SUPPORTED_TEXT_EXTENSIONS = {
    ".adoc",
    ".asc",
    ".csv",
    ".html",
    ".htm",
    ".json",
    ".md",
    ".rst",
    ".text",
    ".tsv",
    ".txt",
    ".xml",
}


class ConnectorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True, slots=True)
class SourceDocument:
    external_id: str
    title: str
    source_uri: str
    source_url: str
    source_version: str
    content_hash: str
    content_bytes: bytes
    content_type: str = "text/plain; charset=utf-8"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceTombstone:
    external_id: str
    source_version: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    status: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceSyncPayload:
    documents: list[SourceDocument] = field(default_factory=list)
    tombstones: list[SourceTombstone] = field(default_factory=list)
    next_cursor: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)


class SourceConnector:
    def __init__(self, config: dict[str, Any], credentials: dict[str, Any] | None = None) -> None:
        self.config = config
        self.credentials = credentials or {}

    async def healthcheck(self) -> ConnectorHealth:
        return ConnectorHealth(status="ok")

    async def sync(
        self,
        *,
        mode: str,
        cursor: dict[str, Any],
        known_external_ids: set[str],
    ) -> SourceSyncPayload:
        raise NotImplementedError


def connector_for_kind(kind: str, config: dict[str, Any], credentials: dict[str, Any] | None = None) -> SourceConnector:
    mapping: dict[str, type[SourceConnector]] = {
        "confluence_dc": ConfluenceDataCenterConnector,
        "jira_dc": JiraDataCenterConnector,
        "gitlab_self_managed": GitLabSelfManagedConnector,
        "kiwix_zim": KiwixZimSourceConnector,
        "local_folder": LocalFolderConnector,
        "internal_crawler": InternalCrawlerConnector,
        "sunduk_mock": SundukMockConnector,
        "docsmart_mock": DocSmartMockConnector,
    }
    connector_type = mapping.get(kind)
    if connector_type is None:
        raise ConnectorError("CONNECTOR_KIND_UNSUPPORTED", "connector kind is not supported")
    return connector_type(config, credentials)


def connector_http_options(config: dict[str, Any]) -> dict[str, Any]:
    ca_bundle = _optional_str(config.get("ca_bundle_path"))
    cert_file = _optional_str(config.get("mtls_cert_path"))
    key_file = _optional_str(config.get("mtls_key_path"))
    options: dict[str, Any] = {"timeout": float(config.get("timeout_seconds") or 30), "verify": ca_bundle or True}
    if cert_file and key_file:
        options["cert"] = (cert_file, key_file)
    elif cert_file or key_file:
        raise ConnectorError("CONNECTOR_MTLS_INVALID", "mTLS requires both certificate and key paths")
    if options["verify"] is False:
        raise ConnectorError("CONNECTOR_TLS_INVALID", "TLS verification must not be disabled")
    return options


def _auth_headers(credentials: dict[str, Any]) -> dict[str, str]:
    token = _optional_str(credentials.get("token") or credentials.get("bearer_token"))
    if token:
        return {"Authorization": f"Bearer {token}"}
    username = _optional_str(credentials.get("username"))
    password = _optional_str(credentials.get("password"))
    if username and password:
        raw = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {raw}"}
    cookie = _optional_str(credentials.get("cookie"))
    if cookie:
        return {"Cookie": cookie}
    return {}


def _make_client(config: dict[str, Any], credentials: dict[str, Any]) -> httpx.AsyncClient:
    options = connector_http_options(config)
    return httpx.AsyncClient(
        follow_redirects=True,
        headers=_auth_headers(credentials),
        **options,
    )


def _safe_base_url(config: dict[str, Any]) -> str:
    base_url = _optional_str(config.get("base_url"))
    if not base_url:
        raise ConnectorError("CONNECTOR_CONFIG_INVALID", "base_url is required")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConnectorError("CONNECTOR_URL_INVALID", "connector base_url must be http or https")
    if not _is_local_network_host(parsed.hostname or ""):
        raise ConnectorError("CONNECTOR_URL_NOT_LOCAL", "connector base_url must point to a local network host")
    return base_url.rstrip("/")


def _is_local_network_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in {"localhost"} or normalized.endswith(".local") or normalized.endswith(".internal"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return "." not in normalized
    return bool(address.is_private or address.is_loopback or address.is_link_local)


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()
    lines: list[str] = []
    for element in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "code", "td", "th"]):
        text = element.get_text(" ", strip=True)
        if not text:
            continue
        name = str(element.name)
        if name in {"h1", "h2", "h3", "h4"}:
            level = int(name[1])
            lines.append(f"{'#' * level} {text}")
        elif name in {"td", "th"}:
            lines.append(f"| {text} |")
        else:
            lines.append(text)
    return "\n\n".join(lines) or soup.get_text("\n", strip=True)


def _text_document(
    *,
    external_id: str,
    title: str,
    source_uri: str,
    source_url: str,
    source_version: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> SourceDocument:
    content = text.encode("utf-8")
    return SourceDocument(
        external_id=external_id,
        title=title,
        source_uri=source_uri,
        source_url=source_url,
        source_version=source_version,
        content_hash=sha256_hex(content),
        content_bytes=content,
        content_type="text/plain; charset=utf-8",
        metadata=metadata or {},
    )


def _optional_str(value: Any) -> str:
    return str(value).strip() if value is not None and str(value).strip() else ""


class LocalFolderConnector(SourceConnector):
    async def healthcheck(self) -> ConnectorHealth:
        await asyncio.to_thread(_local_folder_root, self.config)
        return ConnectorHealth(status="ok", details={"root_configured": True})

    async def sync(
        self,
        *,
        mode: str,
        cursor: dict[str, Any],
        known_external_ids: set[str],
    ) -> SourceSyncPayload:
        del cursor
        return await asyncio.to_thread(
            _scan_local_folder,
            self.config,
            mode,
            known_external_ids,
        )


def _local_folder_root(config: dict[str, Any]) -> Path:
    root = Path(_optional_str(config.get("root_path"))).resolve()
    if not root.is_dir():
        raise ConnectorError("LOCAL_FOLDER_UNAVAILABLE", "local folder is not available")
    return root


def _scan_local_folder(config: dict[str, Any], mode: str, known_external_ids: set[str]) -> SourceSyncPayload:
    root = _local_folder_root(config)
    max_files = int(config.get("max_files") or 1000)
    configured_extensions = {str(item).lower() for item in config.get("extensions", []) if str(item).startswith(".")}
    extensions = configured_extensions or SUPPORTED_TEXT_EXTENSIONS
    documents: list[SourceDocument] = []
    seen: set[str] = set()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        if len(documents) >= max_files:
            break
        if path.suffix.lower() not in extensions:
            continue
        resolved = path.resolve()
        if root not in resolved.parents and resolved != root:
            continue
        rel = resolved.relative_to(root).as_posix()
        data = resolved.read_bytes()
        content_hash = sha256_hex(data)
        stat = resolved.stat()
        source_version = stable_hash([rel, stat.st_size, stat.st_mtime_ns, content_hash], 32)
        seen.add(rel)
        documents.append(
            SourceDocument(
                external_id=rel,
                title=path.name,
                source_uri=f"file://{rel}",
                source_url=f"file://{rel}",
                source_version=source_version,
                content_hash=content_hash,
                content_bytes=data,
                content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                metadata={"relative_path": rel, "size_bytes": stat.st_size},
            )
        )
    tombstones = [
        SourceTombstone(external_id=external_id, source_version="deleted", metadata={"source": "full_reconcile"})
        for external_id in sorted(known_external_ids - seen)
        if mode == "full"
    ]
    return SourceSyncPayload(
        documents=documents,
        tombstones=tombstones,
        next_cursor={"last_full_scan": mode == "full"},
        stats={"seen": len(seen), "documents": len(documents), "tombstones": len(tombstones)},
    )


class ConfluenceDataCenterConnector(SourceConnector):
    async def healthcheck(self) -> ConnectorHealth:
        base_url = _safe_base_url(self.config)
        async with _make_client(self.config, self.credentials) as client:
            response = await client.get(f"{base_url}/rest/api/space", params={"limit": 1})
            response.raise_for_status()
        return ConnectorHealth(status="ok")

    async def sync(
        self,
        *,
        mode: str,
        cursor: dict[str, Any],
        known_external_ids: set[str],
    ) -> SourceSyncPayload:
        del mode, known_external_ids
        base_url = _safe_base_url(self.config)
        space = _optional_str(self.config.get("space"))
        limit = int(self.config.get("limit") or 50)
        start = int(cursor.get("start") or 0)
        documents: list[SourceDocument] = []
        async with _make_client(self.config, self.credentials) as client:
            while len(documents) < limit:
                params: dict[str, Any] = {
                    "type": "page",
                    "status": "current",
                    "expand": "body.storage,version,space,metadata.labels,history,ancestors",
                    "limit": min(25, limit - len(documents)),
                    "start": start,
                }
                if space:
                    params["spaceKey"] = space
                response = await client.get(f"{base_url}/rest/api/content", params=params)
                response.raise_for_status()
                payload = response.json()
                results = payload.get("results") if isinstance(payload, dict) else None
                if not isinstance(results, list) or not results:
                    break
                for page in results:
                    if isinstance(page, dict):
                        documents.append(_confluence_page_document(base_url, page))
                start += len(results)
                if not payload.get("_links", {}).get("next"):
                    break
        return SourceSyncPayload(
            documents=documents,
            next_cursor={"start": start},
            stats={"documents": len(documents), "tombstones": 0},
        )


def _confluence_page_document(base_url: str, page: dict[str, Any]) -> SourceDocument:
    page_id = str(page.get("id") or stable_hash([page], 16))
    title = str(page.get("title") or page_id)
    body: dict[str, Any] = page["body"] if isinstance(page.get("body"), dict) else {}
    storage: dict[str, Any] = body["storage"] if isinstance(body.get("storage"), dict) else {}
    html = str(storage.get("value") or "")
    version: dict[str, Any] = page["version"] if isinstance(page.get("version"), dict) else {}
    space: dict[str, Any] = page["space"] if isinstance(page.get("space"), dict) else {}
    metadata: dict[str, Any] = page["metadata"] if isinstance(page.get("metadata"), dict) else {}
    label_payload: dict[str, Any] = metadata["labels"] if isinstance(metadata.get("labels"), dict) else {}
    labels: list[Any] = label_payload["results"] if isinstance(label_payload.get("results"), list) else []
    text = f"# {title}\n\n{_html_to_text(html)}"
    source_version = str(version.get("number") or version.get("when") or stable_hash([html], 32))
    webui = page.get("_links", {}).get("webui") if isinstance(page.get("_links"), dict) else None
    return _text_document(
        external_id=page_id,
        title=title,
        source_uri=f"confluence://{space.get('key', '')}/{page_id}",
        source_url=urljoin(base_url, str(webui or f"/pages/{page_id}")),
        source_version=source_version,
        text=text,
        metadata={
            "space": space.get("key"),
            "version": version.get("number"),
            "updated_at": version.get("when"),
            "labels": [label.get("name") for label in labels if isinstance(label, dict)],
            "source_type": "confluence_dc",
        },
    )


class JiraDataCenterConnector(SourceConnector):
    async def healthcheck(self) -> ConnectorHealth:
        base_url = _safe_base_url(self.config)
        async with _make_client(self.config, self.credentials) as client:
            response = await client.get(f"{base_url}/rest/api/2/serverInfo")
            response.raise_for_status()
        return ConnectorHealth(status="ok")

    async def sync(
        self,
        *,
        mode: str,
        cursor: dict[str, Any],
        known_external_ids: set[str],
    ) -> SourceSyncPayload:
        del known_external_ids
        base_url = _safe_base_url(self.config)
        overlap_minutes = int(self.config.get("updated_overlap_minutes") or 5)
        base_jql = _optional_str(self.config.get("jql")) or "ORDER BY updated ASC"
        updated_from = _optional_str(cursor.get("updated_until"))
        if mode == "incremental" and updated_from:
            base_jql = f'updated >= "{updated_from}" ORDER BY updated ASC'
        max_results = int(self.config.get("limit") or 50)
        fields = "summary,description,comment,attachment,status,project,labels,issuelinks,updated"
        documents: list[SourceDocument] = []
        async with _make_client(self.config, self.credentials) as client:
            response = await client.get(
                f"{base_url}/rest/api/2/search",
                params={"jql": base_jql, "maxResults": max_results, "fields": fields},
            )
            response.raise_for_status()
            payload = response.json()
        issues = payload.get("issues") if isinstance(payload, dict) else []
        latest_updated = updated_from
        for issue in issues if isinstance(issues, list) else []:
            if isinstance(issue, dict):
                document = _jira_issue_document(base_url, issue)
                documents.append(document)
                latest_updated = str(document.metadata.get("updated_at") or latest_updated or "")
        cursor_value = latest_updated
        if cursor_value and overlap_minutes > 0:
            cursor_value = cursor_value
        return SourceSyncPayload(
            documents=documents,
            next_cursor={"updated_until": cursor_value, "overlap_minutes": overlap_minutes},
            stats={"documents": len(documents), "tombstones": 0},
        )


def _jira_issue_document(base_url: str, issue: dict[str, Any]) -> SourceDocument:
    key = str(issue.get("key") or issue.get("id") or stable_hash([issue], 16))
    fields: dict[str, Any] = issue["fields"] if isinstance(issue.get("fields"), dict) else {}
    status: dict[str, Any] = fields["status"] if isinstance(fields.get("status"), dict) else {}
    project: dict[str, Any] = fields["project"] if isinstance(fields.get("project"), dict) else {}
    comments_payload: dict[str, Any] = fields["comment"] if isinstance(fields.get("comment"), dict) else {}
    comments: list[Any] = comments_payload["comments"] if isinstance(comments_payload.get("comments"), list) else []
    attachments: list[Any] = fields["attachment"] if isinstance(fields.get("attachment"), list) else []
    links: list[Any] = fields["issuelinks"] if isinstance(fields.get("issuelinks"), list) else []
    updated = str(fields.get("updated") or "")
    text_parts = [
        f"# {key}: {fields.get('summary') or key}",
        str(fields.get("description") or ""),
        "## Comments",
        "\n\n".join(str(comment.get("body") or "") for comment in comments if isinstance(comment, dict)),
    ]
    source_version = stable_hash([updated, fields.get("summary"), fields.get("description"), comments], 32)
    return _text_document(
        external_id=key,
        title=f"{key}: {fields.get('summary') or key}",
        source_uri=f"jira://{key}",
        source_url=f"{base_url}/browse/{quote(key)}",
        source_version=source_version,
        text="\n\n".join(text_parts),
        metadata={
            "status": status.get("name"),
            "project": project.get("key"),
            "labels": fields.get("labels") if isinstance(fields.get("labels"), list) else [],
            "updated_at": updated,
            "attachments": [
                {"filename": item.get("filename"), "id": item.get("id")}
                for item in attachments
                if isinstance(item, dict)
            ],
            "links_count": len(links),
            "source_type": "jira_dc",
        },
    )


class GitLabSelfManagedConnector(SourceConnector):
    async def healthcheck(self) -> ConnectorHealth:
        base_url = _safe_base_url(self.config)
        async with _make_client(self.config, self.credentials) as client:
            response = await client.get(f"{base_url}/api/v4/version")
            response.raise_for_status()
        return ConnectorHealth(status="ok")

    async def sync(
        self,
        *,
        mode: str,
        cursor: dict[str, Any],
        known_external_ids: set[str],
    ) -> SourceSyncPayload:
        del mode, cursor, known_external_ids
        base_url = _safe_base_url(self.config)
        project_id = quote(str(self.config.get("project_id") or ""), safe="")
        if not project_id:
            raise ConnectorError("GITLAB_PROJECT_REQUIRED", "project_id is required")
        ref = _optional_str(self.config.get("ref")) or "HEAD"
        path_allowlist = [str(item) for item in self.config.get("path_allowlist", [])]
        max_files = int(self.config.get("max_files") or 100)
        documents: list[SourceDocument] = []
        async with _make_client(self.config, self.credentials) as client:
            tree = await client.get(
                f"{base_url}/api/v4/projects/{project_id}/repository/tree",
                params={"recursive": "true", "per_page": max_files, "ref": ref},
            )
            tree.raise_for_status()
            entries = tree.json()
            for entry in entries if isinstance(entries, list) else []:
                if len(documents) >= max_files or not isinstance(entry, dict) or entry.get("type") != "blob":
                    continue
                path = str(entry.get("path") or "")
                if not _gitlab_path_allowed(path, path_allowlist):
                    continue
                file_response = await client.get(
                    f"{base_url}/api/v4/projects/{project_id}/repository/files/{quote(path, safe='')}",
                    params={"ref": ref},
                )
                file_response.raise_for_status()
                payload = file_response.json()
                encoded = str(payload.get("content") or "")
                content = base64.b64decode(encoded, validate=False)
                documents.append(
                    SourceDocument(
                        external_id=path,
                        title=Path(path).name,
                        source_uri=f"gitlab://{project_id}/{path}",
                        source_url=f"{base_url}/{project_id}/-/blob/{quote(ref)}/{quote(path)}",
                        source_version=str(payload.get("blob_id") or entry.get("id") or sha256_hex(content)),
                        content_hash=sha256_hex(content),
                        content_bytes=content,
                        content_type=mimetypes.guess_type(path)[0] or "text/plain; charset=utf-8",
                        metadata={"path": path, "ref": ref, "source_type": "gitlab_self_managed"},
                    )
                )
        return SourceSyncPayload(
            documents=documents,
            next_cursor={"ref": ref},
            stats={"documents": len(documents), "tombstones": 0},
        )


def _gitlab_path_allowed(path: str, allowlist: list[str]) -> bool:
    suffix_allowed = Path(path).suffix.lower() in {".md", ".adoc", ".asc", ".txt", ".rst"}
    basename_allowed = Path(path).name.lower() in {"readme", "readme.md", "readme.adoc"}
    prefix_allowed = not allowlist or any(
        path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in allowlist
    )
    return prefix_allowed and (suffix_allowed or basename_allowed)


class InternalCrawlerConnector(SourceConnector):
    async def healthcheck(self) -> ConnectorHealth:
        base_url = _safe_base_url(self.config)
        async with _make_client(self.config, self.credentials) as client:
            response = await client.get(base_url)
            response.raise_for_status()
        return ConnectorHealth(status="ok")

    async def sync(
        self,
        *,
        mode: str,
        cursor: dict[str, Any],
        known_external_ids: set[str],
    ) -> SourceSyncPayload:
        del mode, cursor, known_external_ids
        base_url = _safe_base_url(self.config)
        allowed_domains = {str(item).lower() for item in self.config.get("allowed_domains", [])}
        allowed_domains.add(urlparse(base_url).hostname or "")
        max_pages = int(self.config.get("max_pages") or 50)
        max_depth = int(self.config.get("max_depth") or 2)
        excludes = [re.compile(str(item)) for item in self.config.get("exclude_url_patterns", [])]
        seed_urls = await self._seed_urls(base_url)
        queue: deque[tuple[str, int]] = deque((url, 0) for url in seed_urls)
        seen: set[str] = set()
        documents: list[SourceDocument] = []
        async with _make_client(self.config, self.credentials) as client:
            while queue and len(documents) < max_pages:
                url, depth = queue.popleft()
                if url in seen or depth > max_depth or any(pattern.search(url) for pattern in excludes):
                    continue
                parsed = urlparse(url)
                if (parsed.hostname or "").lower() not in allowed_domains:
                    continue
                seen.add(url)
                response = await client.get(url)
                response.raise_for_status()
                html = response.text
                title = _html_title(html) or url
                etag = response.headers.get("ETag", "")
                last_modified = response.headers.get("Last-Modified", "")
                text = _html_to_text(html)
                source_version = stable_hash([etag, last_modified, sha256_hex(text.encode("utf-8"))], 32)
                documents.append(
                    _text_document(
                        external_id=url,
                        title=title,
                        source_uri=f"web://{url}",
                        source_url=url,
                        source_version=source_version,
                        text=text,
                        metadata={"etag": etag, "last_modified": last_modified, "source_type": "internal_crawler"},
                    )
                )
                if depth < max_depth:
                    for link in _html_links(html, url):
                        queue.append((link, depth + 1))
        return SourceSyncPayload(
            documents=documents,
            next_cursor={"seen_urls": len(seen)},
            stats={"documents": len(documents), "tombstones": 0},
        )

    async def _seed_urls(self, base_url: str) -> list[str]:
        sitemap_url = _optional_str(self.config.get("sitemap_url"))
        if not sitemap_url:
            return [base_url]
        async with _make_client(self.config, self.credentials) as client:
            response = await client.get(sitemap_url)
            response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        urls = [element.get_text(strip=True) for element in soup.find_all("loc") if element.get_text(strip=True)]
        return urls or [base_url]


def _html_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    heading = soup.find(["h1", "h2"])
    return heading.get_text(" ", strip=True) if heading else ""


def _html_links(html: str, base_url: str) -> Iterable[str]:
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("a"):
        href = link.get("href")
        if isinstance(href, str) and href:
            yield urljoin(base_url, href)


class KiwixZimSourceConnector(SourceConnector):
    async def healthcheck(self) -> ConnectorHealth:
        zim_path = _zim_path(self.config)
        if not zim_path.exists():
            raise ConnectorError("ZIM_NOT_FOUND", "ZIM file is not available")
        source_version = await asyncio.to_thread(_zim_source_version, zim_path)
        return ConnectorHealth(status="ok", details={"source_version": source_version})

    async def sync(
        self,
        *,
        mode: str,
        cursor: dict[str, Any],
        known_external_ids: set[str],
    ) -> SourceSyncPayload:
        del mode, known_external_ids
        zim_path = _zim_path(self.config)
        if not zim_path.exists():
            raise ConnectorError("ZIM_NOT_FOUND", "ZIM file is not available")
        source_version = await asyncio.to_thread(_zim_source_version, zim_path)
        changed = cursor.get("source_version") != source_version
        return SourceSyncPayload(
            next_cursor={"source_version": source_version, "changed": changed},
            stats={"documents": 0, "tombstones": 0, "zim_changed": int(changed)},
        )


def _zim_path(config: dict[str, Any]) -> Path:
    explicit = _optional_str(config.get("zim_path"))
    if explicit:
        return Path(explicit)
    zim_dir = Path(_optional_str(config.get("zim_dir")) or "zim")
    filename = _optional_str(config.get("zim_filename"))
    return zim_dir / filename


def _zim_source_version(path: Path) -> str:
    stat = path.stat()
    digest = _file_sha256_hex(path)
    return stable_hash([path.name, stat.st_size, stat.st_mtime_ns, digest], 32)


def _file_sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class SundukMockConnector(SourceConnector):
    async def sync(
        self,
        *,
        mode: str,
        cursor: dict[str, Any],
        known_external_ids: set[str],
    ) -> SourceSyncPayload:
        del mode, cursor, known_external_ids
        query = str(self.config.get("query") or "резервное копирование")
        payload = {
            "external_id": "123",
            "title": "Инструкция",
            "text": f"Найденный фрагмент: {query}",
            "url": "https://sunduk.local/docs/123",
            "score": 12.5,
            "metadata": {},
        }
        document = _text_document(
            external_id=str(payload["external_id"]),
            title=str(payload["title"]),
            source_uri=f"sunduk://{payload['external_id']}",
            source_url=str(payload["url"]),
            source_version=stable_hash([payload], 32),
            text=str(payload["text"]),
            metadata={"contract": "sunduk_search_v1", "score": payload["score"]},
        )
        return SourceSyncPayload(
            documents=[document],
            next_cursor={"mock": True},
            stats={"documents": 1, "tombstones": 0},
        )


class DocSmartMockConnector(SourceConnector):
    async def sync(
        self,
        *,
        mode: str,
        cursor: dict[str, Any],
        known_external_ids: set[str],
    ) -> SourceSyncPayload:
        del mode, cursor, known_external_ids
        record = {
            "document_id": "456",
            "title": "Регламент",
            "text": "Полный текст",
            "file_path": "/documents/456.pdf",
            "updated_at": "2026-08-01T12:00:00Z",
        }
        document = _text_document(
            external_id=str(record["document_id"]),
            title=str(record["title"]),
            source_uri=f"docsmart://{record['document_id']}",
            source_url=f"file://{record['file_path']}",
            source_version=str(record["updated_at"]),
            text=str(record["text"]),
            metadata={"contract": "docsmart_record_v1", "file_path": record["file_path"]},
        )
        return SourceSyncPayload(
            documents=[document],
            next_cursor={"mock": True},
            stats={"documents": 1, "tombstones": 0},
        )

    async def poll_changes(self) -> list[dict[str, Any]]:
        return [{"document_id": "456", "updated_at": "2026-08-01T12:00:00Z"}]

    async def fetch_document(self, document_id: str) -> dict[str, Any]:
        return {
            "document_id": document_id,
            "title": "Регламент",
            "text": "Полный текст",
            "file_path": f"/documents/{document_id}.pdf",
            "updated_at": "2026-08-01T12:00:00Z",
        }

    async def search(self, query: str, filters: dict[str, Any], limit: int) -> dict[str, Any]:
        return {
            "results": [
                {
                    "external_id": "456",
                    "title": "Регламент",
                    "text": query,
                    "url": "file:///documents/456.pdf",
                    "score": float(limit),
                    "metadata": filters,
                }
            ]
        }
