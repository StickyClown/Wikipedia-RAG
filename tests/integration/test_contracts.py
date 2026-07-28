from __future__ import annotations

from pathlib import Path


def test_openrouter_secret_file_is_ignored() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "openrouter_key.txt" in gitignore
    assert "zip/" in gitignore


def test_wikipedia_xml_adr_exists() -> None:
    adr = Path("docs/decisions/ADR-007-wikipedia-xml-multistream.md").read_text(encoding="utf-8")
    assert "monotonic non-decreasing" in adr


def test_zim_demo_adr_and_configs_exist() -> None:
    adr = Path("docs/decisions/ADR-008-zim-kiwix-demo-source.md").read_text(encoding="utf-8")
    retrieval = Path("config/retrieval.yaml").read_text(encoding="utf-8")
    models = Path("config/models.yaml").read_text(encoding="utf-8")

    assert "ZIM/libzim" in adr
    assert "sota_mvp" in retrieval
    assert "qwen/qwen3-embedding-8b" in models


def test_retrieval_contract_exec_plan_and_docs_exist() -> None:
    plan = Path("docs/exec-plans/14-retrieval-contract-and-kb-readiness.md").read_text(encoding="utf-8")
    api = Path("docs/contracts/API_CONTRACT.md").read_text(encoding="utf-8")
    database = Path("docs/contracts/DATABASE_CONTRACT.md").read_text(encoding="utf-8")

    assert "KB_NOT_READY" in plan
    assert "index_contract_id" in api
    assert "metadata.index_contract_id" in database
