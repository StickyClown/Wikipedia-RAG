"""Server-owned resolution of local import files.

HTTP callers may select a file *name* from an operator-managed directory, but
must never provide an arbitrary filesystem path.  Workers resolve the same
logical name again immediately before they read it.
"""

from __future__ import annotations

from pathlib import Path


class ImportFileNameError(ValueError):
    """A supplied import filename is not safe or not present under its root."""


def resolve_import_filename(root: Path, filename: str) -> Path:
    """Return an existing regular file contained by the resolved trusted root."""
    name = filename.strip()
    candidate_name = Path(name)
    if not name or name in {".", ".."} or candidate_name.name != name or "/" in name or "\\" in name:
        raise ImportFileNameError("invalid import filename")
    try:
        resolved_root = root.resolve(strict=True)
        candidate = (resolved_root / name).resolve(strict=True)
    except OSError as exc:
        raise ImportFileNameError("import file is unavailable") from exc
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ImportFileNameError("invalid import filename") from exc
    if not candidate.is_file():
        raise ImportFileNameError("import file is unavailable")
    return candidate


def configured_or_requested_filename(requested: str | None, configured: Path) -> str:
    """Validate a client name or the operator configured default file."""
    return resolve_import_filename(configured.parent, requested or configured.name).name
