from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from ..session import engine


def _migration_head() -> str:
    # This module lives at gateway/app/db/rag/.  Keep the Alembic root tied to
    # the Gateway package rather than the module's former app/rag location.
    gateway_root = Path(__file__).parents[3]
    config = Config(str(gateway_root / "alembic.ini"))
    config.set_main_option("script_location", str(gateway_root / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


async def verify_rag_database() -> None:
    """Fail worker/Gateway startup before accepting RAG work on a bad store."""
    async with engine.connect() as connection:
        version_num = int(await connection.scalar(text("SHOW server_version_num")))
        if version_num < 170000:
            raise RuntimeError("rag_requires_postgresql_17_or_newer")
        if not await connection.scalar(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ):
            raise RuntimeError("rag_pgvector_extension_missing")
        revision = await connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
    if revision != _migration_head():
        raise RuntimeError("rag_database_not_at_alembic_head")
