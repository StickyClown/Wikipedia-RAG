"""Executable guards for the Model Gateway ownership boundary."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path("src/wikipediarag")
DRIVER_ALLOWLIST = {
    "api/routers/model_control.py",
    "model_drivers.py",
}
OPENROUTER_KEY_ALLOWLIST = {"config.py", "gateway_app.py", "cli.py"}


def _imports(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((name.name, "") for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.extend((node.module or "", name.name) for name in node.names)
    return found


def test_only_gateway_control_plane_uses_provider_driver_module() -> None:
    offenders: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if relative in DRIVER_ALLOWLIST:
            continue
        if any(module == "wikipediarag.model_drivers" for module, _name in _imports(path)):
            offenders.append(relative)
    assert offenders == []


def test_openrouter_key_resolution_has_a_small_explicit_allowlist() -> None:
    offenders: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if relative in OPENROUTER_KEY_ALLOWLIST:
            continue
        if any(name == "resolve_openrouter_api_key" for _module, name in _imports(path)):
            offenders.append(relative)
    assert offenders == []
