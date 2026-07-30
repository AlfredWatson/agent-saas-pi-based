from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
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
    jwt_secret: str = "development-only-secret-must-be-replaced"
    encryption_key: str = ""
    runtime_url: str = "http://127.0.0.1:3000"
    runtime_shared_secret: str = "shared-dev"
    jwt_issuer: str = "pi-saas"
    jwt_audience: str = "pi-saas-api"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
