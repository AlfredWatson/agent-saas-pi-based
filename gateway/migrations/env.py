from alembic import context
from sqlalchemy import engine_from_config, pool
from app.core.config import get_settings
from app.db.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("+asyncpg", ""))

def run_migrations_online() -> None:
    engine = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, include_schemas=True)
        with context.begin_transaction(): context.run_migrations()

run_migrations_online()
