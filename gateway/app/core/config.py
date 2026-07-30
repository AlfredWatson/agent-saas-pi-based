from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).parents[3] / ".env", extra="ignore")
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/pi_saas"
    jwt_secret: str = "development-only-secret-must-be-replaced"
    encryption_key: str = ""
    runtime_url: str = "http://127.0.0.1:3000"
    runtime_shared_secret: str = "shared-dev"
    jwt_issuer: str = "pi-saas"
    jwt_audience: str = "pi-saas-api"


@lru_cache
def get_settings() -> Settings:
    return Settings()
