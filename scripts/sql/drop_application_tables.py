"""Remove the PostgreSQL table structures for this application.

This drops the ``rag`` and ``platform`` schemas, including every table,
sequence, index, and foreign-key constraint inside them. It also removes the
project's ``public.alembic_version`` record so the next Gateway startup can
recreate the schemas by applying migrations from the beginning.

This command intentionally does not clear Redis. Run
``clear_application_data.py`` first when the RAG cache must be removed too.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "gateway"))

from app.core.config import get_settings  # noqa: E402

APPLICATION_SCHEMAS = ("rag", "platform")
CONFIRMATION = "DROP_APPLICATION_TABLES"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="perform the destructive operation")
    parser.add_argument("--confirm", help=f"required exact confirmation token: {CONFIRMATION}")
    return parser.parse_args()


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def drop_application_structures(connection: asyncpg.Connection) -> None:
    async with connection.transaction():
        for schema in APPLICATION_SCHEMAS:
            await connection.execute(f"DROP SCHEMA IF EXISTS {quote_identifier(schema)} CASCADE")
        # Alembic's configured version table is in public. Keeping it after
        # dropping the application schemas would incorrectly report migrations
        # as already applied and leave the next Gateway startup without tables.
        await connection.execute("DROP TABLE IF EXISTS public.alembic_version")


async def main() -> None:
    args = parse_args()
    if not args.execute or args.confirm != CONFIRMATION:
        raise SystemExit(
            f"Refusing to drop application tables: pass --execute --confirm {CONFIRMATION}."
        )

    settings = get_settings()
    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        database=settings.postgres_database,
    )
    try:
        await drop_application_structures(connection)
    finally:
        await connection.close()

    print(
        f"Dropped application structures in {', '.join(APPLICATION_SCHEMAS)} and "
        "removed public.alembic_version."
    )


if __name__ == "__main__":
    asyncio.run(main())
