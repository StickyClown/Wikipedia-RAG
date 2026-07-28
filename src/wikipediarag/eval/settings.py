from __future__ import annotations

from pathlib import Path

from wikipediarag.config import Settings


def adapt_eval_settings(settings: Settings) -> Settings:
    if _looks_like_container():
        return settings
    updates: dict[str, object] = {}
    if "@postgres:" in settings.database_url:
        updates["database_url"] = settings.database_url.replace("@postgres:", "@localhost:")
    if settings.model_gateway_url.rstrip("/") == "http://model-gateway:8080":
        updates["model_gateway_url"] = "http://localhost:8081"
    if settings.mock_provider_url.rstrip("/") == "http://mock-provider:8080":
        updates["mock_provider_url"] = "http://localhost:8082"
    if _is_container_zim_path(settings.zim_dir) and Path("zim").exists():
        updates["zim_dir"] = Path("zim")
    if settings.kiwix_internal_base_url.rstrip("/") == "http://kiwix:8080":
        updates["kiwix_internal_base_url"] = settings.kiwix_public_base_url
    return settings.model_copy(update=updates) if updates else settings


def _looks_like_container() -> bool:
    return Path("/app").exists()


def _is_container_zim_path(path: Path) -> bool:
    return path.as_posix() == "/zim" or (path.is_absolute() and path.name == "zim" and not path.exists())
