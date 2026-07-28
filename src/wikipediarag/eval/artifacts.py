from __future__ import annotations

import json
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from wikipediarag.eval.hashing import stable_json_hash
from wikipediarag.eval.schemas import EvalDatasetManifest, EvalTask

ARTIFACT_ROOT = Path("artifacts/eval")
DATASET_NAME = "generated-wikipedia-v1"
DATASET_VERSION = "2026.07.1"


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    write_json_atomic(path, payload)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def read_json(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def write_jsonl(path: Path, rows: Iterable[BaseModel | dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for row in rows:
        payload = row.model_dump(mode="json") if isinstance(row, BaseModel) else row
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def append_jsonl(path: Path, row: BaseModel | dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = row.model_dump(mode="json") if isinstance(row, BaseModel) else row
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def read_jsonl[T: BaseModel](path: Path, model: type[T]) -> list[T]:
    rows: list[T] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(model.model_validate_json(line))
    return rows


def dataset_hash(tasks: list[EvalTask]) -> str:
    return stable_json_hash([task.model_dump(mode="json") for task in tasks])


def dataset_paths(dataset_name: str = DATASET_NAME) -> dict[str, Path]:
    base = ARTIFACT_ROOT / "datasets" / dataset_name
    return {
        "base": base,
        "latest": base / "latest.json",
    }


def write_dataset(tasks: list[EvalTask], manifest: EvalDatasetManifest) -> None:
    jsonl_path = Path(manifest.jsonl_path)
    write_jsonl(jsonl_path, tasks)
    manifest_path = jsonl_path.with_suffix(".manifest.json")
    write_json(manifest_path, manifest.model_dump(mode="json"))
    write_json(dataset_paths(manifest.dataset_name)["latest"], manifest.model_dump(mode="json"))


def load_latest_dataset(dataset_name: str = DATASET_NAME) -> tuple[EvalDatasetManifest, list[EvalTask]]:
    latest = dataset_paths(dataset_name)["latest"]
    if not latest.exists():
        raise FileNotFoundError(f"no generated eval dataset found for suite {dataset_name}")
    manifest = EvalDatasetManifest.model_validate(read_json(latest))
    tasks = read_jsonl(Path(manifest.jsonl_path), EvalTask)
    return manifest, tasks
