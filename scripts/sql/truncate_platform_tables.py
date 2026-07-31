"""Remove all rows from every base table in the platform schema.

Table definitions remain, but this also clears platform.alembic_version when
present. Run only with the explicit confirmation token described by --help.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "gateway"))

from app.core.config import get_settings

CONFIRMATION = "TRUNCATE_PLATFORM_TABLES"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="perform the destructive operation")
    parser.add_argument("--confirm", help=f"required exact confirmation token: {CONFIRMATION}")
    return parser.parse_args()


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def main() -> None:
    args = parse_args()
    if not args.execute or args.confirm != CONFIRMATION:
        raise SystemExit(f"Refusing to truncate tables: pass --execute --confirm {CONFIRMATION}.")

    settings = get_settings()
    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        database=settings.postgres_database,
    )
    try:
        tables = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'platform' ORDER BY tablename"
        )
        if tables:
            qualified_tables = ", ".join(f"platform.{quote_identifier(row['tablename'])}" for row in tables)
            async with connection.transaction():
                await connection.execute(f"TRUNCATE TABLE {qualified_tables} RESTART IDENTITY")
        print(f"Truncated {len(tables)} table(s) in platform.")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
