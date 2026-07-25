from __future__ import annotations

import bz2
from pathlib import Path

import pytest

from wikipediarag.wiki_dump import (
    chunks_for_page,
    iter_stream_groups,
    parse_pages_fragment,
    validate_index,
)


def test_index_validation_accepts_non_decreasing_repeated_offsets(tmp_path: Path) -> None:
    xml_path = tmp_path / "dump.xml.bz2"
    index_path = tmp_path / "index.txt.bz2"
    first = bz2.compress(
        b"  <page><title>A</title><ns>0</ns><id>1</id><revision>"
        b"<id>10</id><timestamp>2026-01-01T00:00:00Z</timestamp>"
        b"<text>Alpha</text></revision></page>"
    )
    second_offset = len(first)
    second = bz2.compress(
        b"  <page><title>C</title><ns>0</ns><id>3</id><revision>"
        b"<id>30</id><timestamp>2026-01-01T00:00:00Z</timestamp>"
        b"<text>Gamma</text></revision></page>"
    )
    xml_path.write_bytes(first + second)
    index_path.write_bytes(bz2.compress(f"0:1:A\n0:2:B\n{second_offset}:3:C\n".encode()))

    stats = validate_index(index_path, xml_path)

    assert stats["line_count"] == 3
    assert stats["unique_stream_count"] == 2


def test_index_validation_rejects_decreasing_offsets(tmp_path: Path) -> None:
    xml_path = tmp_path / "dump.xml.bz2"
    index_path = tmp_path / "index.txt.bz2"
    xml_path.write_bytes(bz2.compress(b"<page />") + bz2.compress(b"<page />"))
    index_path.write_bytes(bz2.compress(b"10:1:A\n0:2:B\n"))

    with pytest.raises(ValueError, match="monotonic non-decreasing"):
        validate_index(index_path, xml_path)


def test_stream_grouping_keeps_repeated_offsets(tmp_path: Path) -> None:
    index_path = tmp_path / "index.txt.bz2"
    index_path.write_bytes(bz2.compress(b"0:1:A\n0:2:B\n42:3:C\n"))

    groups = list(iter_stream_groups(index_path))

    assert groups[0].offset == 0
    assert groups[0].page_ids == (1, 2)
    assert groups[1].offset == 42


def test_parse_pages_and_deterministic_chunks() -> None:
    fragment = """
      <page>
        <title>Россия</title>
        <ns>0</ns>
        <id>9</id>
        <revision>
          <id>100</id>
          <timestamp>2026-01-01T00:00:00Z</timestamp>
          <text>Россия — государство.\\n\\n== География ==\\n[[Москва]] — столица.</text>
        </revision>
      </page>
    """.encode()

    page = parse_pages_fragment(fragment)[0]
    chunks_a = chunks_for_page(page, "ruwiki-test", dimensions=16)
    chunks_b = chunks_for_page(page, "ruwiki-test", dimensions=16)

    assert page.title == "Россия"
    assert page.namespace == 0
    assert chunks_a
    assert [chunk.id for chunk in chunks_a] == [chunk.id for chunk in chunks_b]
    assert "Москва" in " ".join(chunk.content for chunk in chunks_a)
