from __future__ import annotations

from pathlib import Path


def test_openrouter_secret_file_is_ignored() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "openrouter_key.txt" in gitignore
    assert "zip/" in gitignore


def test_wikipedia_xml_adr_exists() -> None:
    adr = Path("docs/decisions/ADR-007-wikipedia-xml-multistream.md").read_text(encoding="utf-8")
    assert "monotonic non-decreasing" in adr
