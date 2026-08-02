from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://rag:change-me-local-only@localhost:5432/rag"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "http://localhost:9000"
    minio_public_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "local-minio"
    minio_secret_key: str = "change-me-local-only"  # noqa: S105
    minio_bucket: str = "rag-artifacts"
    opensearch_url: str = "http://localhost:9200"
    model_gateway_url: str = "http://localhost:8081"
    mock_provider_url: str = "http://localhost:8082"
    model_provider: str = "mock"
    model_gateway_startup_smoke: Literal["required", "warn", "off"] = "required"
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
    api_public_base_url: str = "http://localhost:8000"

    xberg_url: str = "http://localhost:8091"
    xberg_urls: str = ""
    docling_url: str = "http://localhost:8092"
    docling_urls: str = ""
    metadata_service_url: str = "http://localhost:8090"
    document_parser_services_required: bool = False
    document_parser_timeout_seconds: int = 180
    document_parser_xberg_concurrency: int = 2
    document_parser_docling_concurrency: int = 1
    document_ingestion_item_concurrency: int = 2
    document_soft_delete_retention_days: int = 30
    upload_session_ttl_seconds: int = 900
    upload_max_bytes: int = 100 * 1024 * 1024
    upload_json_max_depth: int = 32

    default_tenant_id: str = "11111111-1111-4111-8111-111111111111"
    default_user_id: str = "22222222-2222-4222-8222-222222222222"
    default_kb_id: str = "33333333-3333-4333-8333-333333333333"

    auth_disabled: bool = False
    auth_mode: Literal["local", "oidc", "hybrid", "test"] = "local"
    app_secret_file: Path = Path("/run/secrets/app_secret")
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = "admin"  # noqa: S105
    bootstrap_admin_password_file: Path = Path("/run/secrets/bootstrap_admin_password")
    session_cookie_name: str = "wikipediarag_session"
    session_idle_seconds: int = 30 * 24 * 60 * 60
    session_max_seconds: int = 30 * 24 * 60 * 60
    remember_me_idle_seconds: int = 30 * 24 * 60 * 60
    remember_me_max_seconds: int = 90 * 24 * 60 * 60
    session_cookie_secure: bool = True
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    oidc_discovery_url: str = ""
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret_file: Path = Path("/run/secrets/oidc_client_secret")
    oidc_redirect_uri: str = "http://localhost:8000/api/v1/auth/oidc/callback"
    oidc_scope: str = "openid profile email"
    oidc_claim_sub: str = "sub"
    oidc_claim_username: str = "preferred_username"
    oidc_claim_name: str = "name"
    oidc_claim_email: str = "email"
    oidc_claim_email_verified: str = "email_verified"
    oidc_claim_groups: str = "groups"
    oidc_claim_realm_roles: str = "realm_access.roles"
    oidc_auto_provision_domains: str = ""
    oidc_auto_provision_groups: str = ""
    oidc_auto_provision_roles: str = ""
    oidc_platform_admin_roles: str = ""
    oidc_group_catalog_sync_enabled: bool = False

    eval_auth_mode: Literal["none", "local"] = "local"
    eval_auth_username: str = "admin"
    eval_auth_password: str = "admin"  # noqa: S105

    telemetry_content_capture: Literal["off", "masked"] = "off"
    telemetry_max_text_chars: int = 256
    telemetry_retention_days: int = 30

    @field_validator("models_config_path", "retrieval_config_path", mode="after")
    @classmethod
    def resolve_container_config_path_locally(cls, value: Path) -> Path:
        if value.exists() or not value.as_posix().startswith("/app/"):
            return value
        return Path(value.as_posix().removeprefix("/app/"))

    @model_validator(mode="after")
    def validate_auth_mode(self) -> "Settings":
        if self.auth_mode == "test" and self.app_env != "test":
            raise ValueError("AUTH_MODE=test is allowed only when APP_ENV=test")
        return self

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
