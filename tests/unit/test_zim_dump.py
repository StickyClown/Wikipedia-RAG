from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from wikipediarag.zim_dump import ZimArchiveAdapter, build_kiwix_source_url, chunks_for_zim_page

libzim_writer = pytest.importorskip("libzim.writer")


class _MiniItem(libzim_writer.Item):  # type: ignore[name-defined, misc]
    def __init__(self, path: str, title: str, content: str) -> None:
        self.path = path
        self.title = title
        self.content = content
        self.provider = libzim_writer.StringProvider(content)
        self.hints = {libzim_writer.Hint.FRONT_ARTICLE: True}

    def get_path(self) -> str:
        return self.path

    def get_title(self) -> str:
        return self.title

    def get_mimetype(self) -> str:
        return "text/html"

    def get_contentprovider(self) -> object:
        return self.provider

    def get_hints(self) -> dict[object, bool]:
        return self.hints


def test_kiwix_source_url_uses_exact_path() -> None:
    url = build_kiwix_source_url("http://localhost:8083/", "wikipedia_ru_all", "A/Санкт Петербург")

    assert url == (
        "http://localhost:8083/content/wikipedia_ru_all/A/"
        "%D0%A1%D0%B0%D0%BD%D0%BA%D1%82%20%D0%9F%D0%B5%D1%82%D0%B5%D1%80%D0%B1%D1%83%D1%80%D0%B3"
    )


def test_zim_adapter_skips_redirects_when_counting_articles(tmp_path: Path) -> None:
    from libzim.writer import Creator, Hint

    article_html = (
        "<html><body><h1>Россия</h1><p>"
        "Россия государство в Восточной Европе и Северной Азии. Москва столица России. " * 12
    )
    article_html += "</p></body></html>"
    second_html = "<html><body><h1>Москва</h1><p>Москва столица России и крупный город. " * 12
    second_html += "</p></body></html>"
    with TemporaryDirectory(prefix=f"{tmp_path.name}-", dir=Path.cwd()) as workspace_tmp:
        zim_path = Path(workspace_tmp) / "mini.zim"
        with Creator(zim_path) as creator:
            creator.add_metadata("Name", "mini_ru")
            creator.add_metadata("Title", "Mini RU")
            creator.set_mainpath("A/Россия")
            creator.add_item(_MiniItem("A/Россия", "Россия", article_html))
            creator.add_item(_MiniItem("A/Москва", "Москва", second_html))
            creator.add_redirection("A/РФ", "РФ", "A/Россия", {Hint.FRONT_ARTICLE: True})

        adapter = ZimArchiveAdapter(zim_path, public_base_url="http://localhost:8083")
        items = list(adapter.iter_items())
        pages = [item.page for item in items if item.page is not None]
        redirects = [item.redirect for item in items if item.redirect is not None]

        pages_by_title = {page.title: page for page in pages}
        assert sorted(pages_by_title) == ["Москва", "Россия"]
        assert len(redirects) == 1
        assert pages_by_title["Россия"].source_url.endswith("/content/mini/A/%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F")
        chunks = chunks_for_zim_page(
            pages_by_title["Россия"],
            snapshot_id="snapshot",
            dimensions=4,
            child_tokens_min=10,
            child_tokens_max=20,
            parent_tokens_min=30,
            parent_tokens_max=40,
        )
        assert chunks
        assert all(chunk.metadata["zim_entry_path"] == "A/Россия" for chunk in chunks)
