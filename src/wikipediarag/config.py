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
    mock_provider_chat_delay_seconds: float = Field(default=0, ge=0, le=60)
    mock_provider_chat_delay_requests: int = Field(default=0, ge=0, le=100)
    mock_provider_output_mode: str = Field(
        default="normal", pattern="^(normal|malformed_json|truncated_json|schema_mismatch)$"
    )
    model_provider: str = "mock"
    model_gateway_startup_smoke: Literal["required", "warn", "off"] = "required"
    openrouter_api_key: str = ""
    openrouter_api_key_file: Path | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_endpoint_host_allowlist: str = ""
    model_provider_timeout_seconds: float = 180
    model_client_chat_timeout_seconds: float = 300
    model_client_embedding_timeout_seconds: float = 240
    model_client_rerank_timeout_seconds: float = 240
    chat_run_deadline_seconds: float = 300
    operation_heartbeat_seconds: int = 10
    dependency_circuit_failure_threshold: int = 3
    dependency_circuit_cooldown_seconds: float = 15
    idempotency_record_ttl_seconds: int = 24 * 60 * 60
    safe_external_retry_attempts: int = 2
    worker_research_concurrency: int = 1
    worker_background_concurrency: int = 1
    worker_job_lease_seconds: int = 180
    worker_job_heartbeat_seconds: int = 30
    search_projection_reconcile_interval_seconds: int = 300
    search_projection_reconcile_batch_size: int = 25
    search_projection_reconcile_mutation_batch_size: int = 100
    # OpenSearch defaults to a 10,000-result window.  The repair reads one
    # extra record to prove a complete exact set, so 9,999 is the largest safe
    # bounded value without switching to a scroll cursor.
    search_projection_reconcile_max_chunks_per_document: int = 9_999
    search_projection_event_retention_days: int = 30
    search_projection_event_retention_batch_size: int = 100
    search_projection_ready_max_age_seconds: int = 600
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
    # Kept out of artifacts; required to attest that a runtime eval binding was
    # created by this local deployment rather than edited after ingestion.
    eval_binding_signing_key: str = ""

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

    default_user_id: str = "22222222-2222-4222-8222-222222222222"
    default_kb_id: str = "33333333-3333-4333-8333-333333333333"
    # A full reset is intentionally impossible unless an operator enables it
    # in the environment as well as passing the CLI confirmation flag.
    workspace_reset_enabled: bool = False

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
    # Workspace resource grants use live PostgreSQL membership.  Synchronizing
    # an OIDC claim is intentionally opt-in; unknown groups are ignored unless
    # an operator explicitly enables JIT creation.
    oidc_group_sync_enabled: bool = False
    oidc_group_jit_creation_enabled: bool = False
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

    @field_validator("openrouter_api_key_file", mode="before")
    @classmethod
    def empty_openrouter_key_file_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

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


def resolve_openrouter_api_key(settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    key = resolved.openrouter_api_key.strip()
    if key:
        return key
    key_file = resolved.openrouter_api_key_file
    if key_file is None:
        return ""
    try:
        return key_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise RuntimeError("OPENROUTER_API_KEY_FILE could not be read") from exc
