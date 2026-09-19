"""Clear application data while preserving tables and migration history.

This truncates every base table in the ``platform`` and ``rag`` schemas, then
deletes only ``rag:*`` keys from the Redis database configured by ``.env``.
It never uses Redis ``FLUSHDB`` or ``FLUSHALL``, so unrelated Redis data is
left intact. Stop the Gateway and RAG worker before running this command:
data written concurrently may otherwise survive the cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
from redis.asyncio import Redis

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "gateway"))

from app.core.config import Settings, get_settings  # noqa: E402

APPLICATION_SCHEMAS = ("platform", "rag")
CACHE_PATTERN = "rag:*"
CONFIRMATION = "CLEAR_APPLICATION_DATA"
DELETE_BATCH_SIZE = 1_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="perform the destructive operation")
    parser.add_argument("--confirm", help=f"required exact confirmation token: {CONFIRMATION}")
    return parser.parse_args()


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def application_tables(connection: asyncpg.Connection) -> list[asyncpg.Record]:
    return await connection.fetch(
        """
        SELECT schemaname, tablename
        FROM pg_tables
        WHERE schemaname = ANY($1::text[])
        ORDER BY schemaname, tablename
        """,
        list(APPLICATION_SCHEMAS),
    )


async def truncate_application_tables(connection: asyncpg.Connection) -> int:
    tables = await application_tables(connection)
    if not tables:
        return 0

    qualified_tables = ", ".join(
        f"{quote_identifier(row['schemaname'])}.{quote_identifier(row['tablename'])}" for row in tables
    )
    async with connection.transaction():
        # All application tables are named in one statement. Omitting CASCADE
        # prevents an unexpected table outside these schemas from being cleared.
        await connection.execute(f"TRUNCATE TABLE {qualified_tables} RESTART IDENTITY")
    return len(tables)


async def redis_keys(client: Redis) -> AsyncIterator[str]:
    async for key in client.scan_iter(match=CACHE_PATTERN, count=DELETE_BATCH_SIZE):
        yield key


async def clear_rag_cache(client: Redis) -> int:
    deleted = 0
    batch: list[str] = []
    async for key in redis_keys(client):
        batch.append(key)
        if len(batch) == DELETE_BATCH_SIZE:
            deleted += await client.delete(*batch)
            batch.clear()
    if batch:
        deleted += await client.delete(*batch)
    return deleted


async def clear_redis_cache(settings: Settings) -> int:
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        return await clear_rag_cache(client)
    finally:
        await client.aclose()


async def main() -> None:
    args = parse_args()
    if not args.execute or args.confirm != CONFIRMATION:
        raise SystemExit(
            f"Refusing to clear application data: pass --execute --confirm {CONFIRMATION}."
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
        table_count = await truncate_application_tables(connection)
    finally:
        await connection.close()

    redis_key_count = await clear_redis_cache(settings)
    print(
        f"Truncated {table_count} table(s) in {', '.join(APPLICATION_SCHEMAS)}; "
        f"deleted {redis_key_count} {CACHE_PATTERN!r} Redis key(s) from database "
        f"{settings.redis_database}."
    )


if __name__ == "__main__":
    asyncio.run(main())
