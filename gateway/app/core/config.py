from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).parents[3] / ".env", extra="ignore"
    )
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, gt=0, le=65535)
    postgres_user: str = "hpc_ai_tech"
    postgres_password: str = ""
    postgres_database: str = "pi_saas"
    postgres_max_connections: int = Field(default=10, gt=0)
    environment: str = "development"
    gateway_host: str = "127.0.0.1"
    gateway_port: int = Field(default=8000, gt=0, le=65535)
    jwt_secret: str = "development-only-secret-must-be-replaced"
    encryption_key: str = ""
    runtime_shared_secret: str = "shared-dev"
    runtime_docker_image: str = "pi-saas-agent-runtime:0.82.1-dev"
    runtime_docker_network: str = "pi-saas-runtime"
    runtime_source_dir: Path = Path("agent-runtime/src")
    runtime_data_host_root: Path = Path(".runtime-data")
    runtime_start_timeout_seconds: int = Field(default=30, gt=0, le=300)
    runtime_stop_timeout_seconds: int = Field(default=10, gt=0, le=120)
    runtime_memory_limit: str = "1g"
    runtime_nano_cpus: int = Field(default=1_000_000_000, gt=0)
    runtime_pids_limit: int = Field(default=256, gt=0)
    workspace_max_per_user: int | None = Field(default=None, ge=1)
    workspace_storage_limit_mb: int = Field(default=1024, ge=1)
    workspace_file_max_mb: int = Field(default=100, ge=1)
    file_base: str = "p"
    document_processing_service: str = "default"
    vector_base: str = "p"
    graph_base: str = "p"
    redis_host: str = "127.0.0.1"
    redis_port: int = Field(default=6379, gt=0, le=65535)
    redis_username: str = Field(default="admin", pattern=r"^[A-Za-z0-9_-]+$")
    redis_password: str = "development-redis-password"
    redis_database: int = Field(default=0, ge=0)
    redis_max_connections: int = Field(default=20, gt=0)
    rag_cache_ttl_seconds: int = Field(default=604800, ge=60)
    rag_job_lease_seconds: int = Field(default=120, ge=30)
    rag_worker_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    rag_operation_max_attempts: int = Field(default=3, ge=1, le=20)
    rag_default_parsing_concurrency: int = Field(default=2, ge=1)
    rag_default_chunking_concurrency: int = Field(default=2, ge=1)
    rag_default_embedding_concurrency: int = Field(default=2, ge=1)
    rag_default_graph_concurrency: int = Field(default=1, ge=1)
    rag_max_chunking_concurrency: int = Field(default=8, ge=1)
    rag_max_parsing_concurrency: int = Field(default=8, ge=1)
    rag_max_embedding_concurrency: int = Field(default=8, ge=1)
    rag_max_graph_concurrency: int = Field(default=4, ge=1)
    rag_document_max_mb: int = Field(default=100, ge=1)
    rag_upload_max_files: int = Field(default=20, ge=1, le=100)
    chunk_max_token_size: int = Field(default=512, ge=32)
    chunk_overlap_token_size: int = Field(default=64, ge=0)
    chunk_split_by_character: str = "\n\n"
    chunk_re_expression: str = r"\n{2,}|(?<=[。！？.!?])\s+"
    chunk_breakpoint_threshold: float = Field(default=95.0, gt=0, lt=100)
    rag_model_base_url_allow_private: bool = False
    rag_model_request_timeout_seconds: int = Field(default=300, ge=1, le=600)
    rag_model_max_retries: int = Field(default=1, ge=0, le=10)
    rag_graph_max_output_tokens: int = Field(default=4096, ge=64, le=8192)
    rag_openai_chat_template_disable_thinking: bool = False
    langsmith_api_key: str | None = None
    langsmith_tracing: bool = False
    langsmith_project: str = "pi-saas-rag"
    jwt_issuer: str = "pi-saas"
    jwt_audience: str = "pi-saas-api"

    @field_validator("workspace_max_per_user", mode="before")
    @classmethod
    def parse_workspace_max_per_user(cls, value):
        if value is None or (
            isinstance(value, str) and value.strip().lower() == "unlimited"
        ):
            return None
        return value

    @model_validator(mode="after")
    def validate_secrets(self):
        insecure = {
            "",
            "development-only-secret-must-be-replaced",
            "shared-dev",
            "replace-with-a-long-runtime-secret",
        }
        if self.environment != "development" and (
            self.jwt_secret in insecure or self.runtime_shared_secret in insecure
        ):
            raise ValueError("JWT_SECRET and RUNTIME_SHARED_SECRET must be configured")
        if self.environment != "development" and (
            not self.encryption_key
            or self.encryption_key == "replace-with-32-byte-url-safe-base64-key"
        ):
            raise ValueError(
                "ENCRYPTION_KEY must be a configured 32-byte url-safe base64 key"
            )
        if self.environment != "development" and not self.postgres_password:
            raise ValueError("POSTGRES_PASSWORD must be configured")
        if self.environment != "development" and self.redis_password in {
            "",
            "development-redis-password",
            "replace-with-a-long-redis-password",
        }:
            raise ValueError("REDIS_PASSWORD must be configured")
        if self.document_processing_service != "default":
            raise ValueError(
                "DOCUMENT_PROCESSING_SERVICE currently supports only 'default'"
            )
        if self.file_base != "p":
            raise ValueError("FILE_BASE currently supports only 'p' (postgresql)")
        if self.vector_base != "p":
            raise ValueError("VECTOR_BASE currently supports only 'p' (postgresql)")
        if self.graph_base != "p":
            raise ValueError("GRAPH_BASE currently supports only 'p' (postgresql)")
        if self.chunk_overlap_token_size >= self.chunk_max_token_size:
            raise ValueError(
                "CHUNK_OVERLAP_TOKEN_SIZE must be smaller than CHUNK_MAX_TOKEN_SIZE"
            )
        for default_value, maximum, label in (
            (
                self.rag_default_parsing_concurrency,
                self.rag_max_parsing_concurrency,
                "parsing",
            ),
            (
                self.rag_default_chunking_concurrency,
                self.rag_max_chunking_concurrency,
                "chunking",
            ),
            (
                self.rag_default_embedding_concurrency,
                self.rag_max_embedding_concurrency,
                "embedding",
            ),
            (
                self.rag_default_graph_concurrency,
                self.rag_max_graph_concurrency,
                "graph",
            ),
        ):
            if default_value > maximum:
                raise ValueError(f"default {label} concurrency exceeds its maximum")
        return self

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_database,
        )

    @property
    def redis_url(self) -> str:
        username = quote(self.redis_username, safe="")
        password = quote(self.redis_password, safe="")
        return f"redis://{username}:{password}@{self.redis_host}:{self.redis_port}/{self.redis_database}"

    @property
    def project_root(self) -> Path:
        return Path(__file__).parents[3]

    @property
    def resolved_runtime_source_dir(self) -> Path:
        path = self.runtime_source_dir
        return (
            (self.project_root / path).resolve()
            if not path.is_absolute()
            else path.resolve()
        )

    @property
    def resolved_runtime_data_host_root(self) -> Path:
        path = self.runtime_data_host_root
        return (
            (self.project_root / path).resolve()
            if not path.is_absolute()
            else path.resolve()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
