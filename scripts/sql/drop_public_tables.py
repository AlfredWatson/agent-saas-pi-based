"""Drop every base table from PostgreSQL's public schema.

This is destructive, including for public.alembic_version. Run only with the
explicit confirmation token described by --help.
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

CONFIRMATION = "DROP_PUBLIC_TABLES"


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
        raise SystemExit(f"Refusing to drop tables: pass --execute --confirm {CONFIRMATION}.")

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
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        async with connection.transaction():
            for row in tables:
                await connection.execute(f"DROP TABLE public.{quote_identifier(row['tablename'])} CASCADE")
        print(f"Dropped {len(tables)} table(s) from public.")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
