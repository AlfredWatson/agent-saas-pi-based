from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).parents[3] / ".env", extra="ignore")
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, gt=0, le=65535)
    postgres_user: str = "hpc_ai_tech"
    postgres_password: str
    postgres_database: str = "pi_saas"
    postgres_max_connections: int = Field(default=10, gt=0)
    jwt_secret: str = "development-only-secret-must-be-replaced"
    encryption_key: str = ""
    runtime_url: str = "http://127.0.0.1:3000"
    runtime_shared_secret: str = "shared-dev"
    jwt_issuer: str = "pi-saas"
    jwt_audience: str = "pi-saas-api"

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
