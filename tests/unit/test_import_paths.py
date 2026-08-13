from __future__ import annotations

from pathlib import Path

import pytest

from wikipediarag.import_paths import ImportFileNameError, configured_or_requested_filename, resolve_import_filename


def test_requested_import_filename_is_resolved_only_under_the_server_root(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    root.mkdir()
    allowed = root / "wiki.zim"
    allowed.write_bytes(b"zim")

    assert resolve_import_filename(root, "wiki.zim") == allowed.resolve()
    assert configured_or_requested_filename(None, allowed) == "wiki.zim"


@pytest.mark.parametrize("name", ["../outside.zim", "nested/wiki.zim", r"nested\wiki.zim", "/unsafe/wiki.zim", "."])
def test_requested_import_filename_rejects_paths_and_traversal(tmp_path: Path, name: str) -> None:
    root = tmp_path / "imports"
    root.mkdir()
    (root / "wiki.zim").write_bytes(b"zim")

    with pytest.raises(ImportFileNameError):
        resolve_import_filename(root, name)


def test_requested_import_filename_rejects_symlink_escape_and_missing_file(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    root.mkdir()
    outside = tmp_path / "outside.zim"
    outside.write_bytes(b"zim")
    try:
        (root / "escape.zim").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available to this test process")

    with pytest.raises(ImportFileNameError):
        resolve_import_filename(root, "escape.zim")
    with pytest.raises(ImportFileNameError):
        resolve_import_filename(root, "missing.zim")
