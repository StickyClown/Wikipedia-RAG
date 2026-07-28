from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def stable_json_hash(value: Any, length: int = 64) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cached_file_sha256(path: Path, cache_path: Path) -> str:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    stat = path.stat()
    cache: dict[str, Any] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    item = cache.get(key)
    if (
        isinstance(item, dict)
        and item.get("mtime_ns") == stat.st_mtime_ns
        and item.get("size") == stat.st_size
        and isinstance(item.get("sha256"), str)
    ):
        return str(item["sha256"])
    checksum = file_sha256(path)
    cache[key] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "sha256": checksum}
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return checksum
