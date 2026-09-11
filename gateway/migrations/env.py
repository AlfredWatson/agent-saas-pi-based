import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from app.core.config import get_settings
from app.db.models import Base
from app.rag import models as rag_models  # noqa: F401

config = context.config
config.set_main_option(
    "sqlalchemy.url", get_settings().database_url.render_as_string(hide_password=False)
)


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection, target_metadata=Base.metadata, include_schemas=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


asyncio.run(run_migrations_online())
