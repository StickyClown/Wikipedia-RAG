from __future__ import annotations

from pathlib import Path


def test_compose_and_ui_contract_exist() -> None:
    assert Path("compose.yaml").exists()
    assert Path("services/ui/src/App.tsx").exists()
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    assert "opensearch" in compose
    assert "model-gateway" in compose
    assert "mock-provider" in compose
    assert "kiwix" in compose
    assert "./zim:/zim:ro" in compose
