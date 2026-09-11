from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).parents[3] / ".env", extra="ignore")
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
    jwt_issuer: str = "pi-saas"
    jwt_audience: str = "pi-saas-api"

    @field_validator("workspace_max_per_user", mode="before")
    @classmethod
    def parse_workspace_max_per_user(cls, value):
        if value is None or (isinstance(value, str) and value.strip().lower() == "unlimited"):
            return None
        return value

    @model_validator(mode="after")
    def validate_secrets(self):
        insecure = {"", "development-only-secret-must-be-replaced", "shared-dev", "replace-with-a-long-runtime-secret"}
        if self.environment != "development" and (self.jwt_secret in insecure or self.runtime_shared_secret in insecure):
            raise ValueError("JWT_SECRET and RUNTIME_SHARED_SECRET must be configured")
        if self.environment != "development" and (not self.encryption_key or self.encryption_key == "replace-with-32-byte-url-safe-base64-key"):
            raise ValueError("ENCRYPTION_KEY must be a configured 32-byte url-safe base64 key")
        if self.environment != "development" and not self.postgres_password:
            raise ValueError("POSTGRES_PASSWORD must be configured")
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
    def project_root(self) -> Path:
        return Path(__file__).parents[3]

    @property
    def resolved_runtime_source_dir(self) -> Path:
        path = self.runtime_source_dir
        return (self.project_root / path).resolve() if not path.is_absolute() else path.resolve()

    @property
    def resolved_runtime_data_host_root(self) -> Path:
        path = self.runtime_data_host_root
        return (self.project_root / path).resolve() if not path.is_absolute() else path.resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
