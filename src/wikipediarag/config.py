from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://rag:change-me-local-only@localhost:5432/rag"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "local-minio"
    minio_secret_key: str = "change-me-local-only"  # noqa: S105
    minio_bucket: str = "rag-artifacts"
    opensearch_url: str = "http://localhost:9200"
    model_gateway_url: str = "http://localhost:8081"
    mock_provider_url: str = "http://localhost:8082"
    model_provider: str = "mock"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    models_config_path: Path = Path("config/models.yaml")
    retrieval_config_path: Path = Path("config/retrieval.yaml")
    retrieval_profile: str = "test_mock"

    wiki_xml_path: Path = Field(default=Path("zip/ruwiki-20260701-pages-articles-multistream.xml.bz2"))
    wiki_index_path: Path = Field(default=Path("zip/ruwiki-20260701-pages-articles-multistream-index.txt.bz2"))
    wiki_snapshot_id: str = "ruwiki-20260701"
    embedding_dimensions: int = 64
    zim_dir: Path = Path("zim")
    zim_filename: str = ""
    kiwix_public_base_url: str = "http://localhost:8083"
    kiwix_internal_base_url: str = "http://kiwix:8080"
    kiwix_book_name: str = ""

    default_tenant_id: str = "11111111-1111-4111-8111-111111111111"
    default_user_id: str = "22222222-2222-4222-8222-222222222222"
    default_kb_id: str = "33333333-3333-4333-8333-333333333333"

    @field_validator("models_config_path", "retrieval_config_path", mode="after")
    @classmethod
    def resolve_container_config_path_locally(cls, value: Path) -> Path:
        if value.exists() or not value.as_posix().startswith("/app/"):
            return value
        return Path(value.as_posix().removeprefix("/app/"))

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
