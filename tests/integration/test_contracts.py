from __future__ import annotations

from pathlib import Path


def test_openrouter_secret_file_is_ignored() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "openrouter_key.txt" in gitignore
    assert "zip/" in gitignore


def test_wikipedia_xml_adr_exists() -> None:
    architecture = Path("docs/architecture.md").read_text(encoding="utf-8")
    assert "XML multistream fallback remains supported" in architecture
    assert "monotonic non-decreasing offsets" in architecture


def test_zim_demo_adr_and_configs_exist() -> None:
    architecture = Path("docs/architecture.md").read_text(encoding="utf-8")
    retrieval = Path("config/retrieval.yaml").read_text(encoding="utf-8")
    models = Path("config/models.yaml").read_text(encoding="utf-8")

    assert "ZIM/libzim + Kiwix is the primary local Wikipedia path" in architecture
    assert "sota_mvp" in retrieval
    assert "qwen/qwen3-embedding-8b" in models


def test_retrieval_contract_exec_plan_and_docs_exist() -> None:
    architecture = Path("docs/architecture.md").read_text(encoding="utf-8")

    assert "KB_NOT_READY" in architecture
    assert "index_contract_id" in architecture
    assert "metadata.index_contract_id" in architecture
