from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text

from .api.v1 import auth, profiles, providers, sessions, workspaces
from .clients.agent_runtime import RuntimeClient
from .db.session import engine

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Schema changes are Alembic-owned; do not silently synthesize tables here.
    gateway_root = Path(__file__).parents[1]
    alembic = Config(str(gateway_root / "alembic.ini"))
    alembic.set_main_option("script_location", str(gateway_root / "migrations"))
    # migrations/env.py intentionally imports the gateway's top-level `app`
    # package, including when Uvicorn is launched from the repository root.
    alembic.set_main_option("prepend_sys_path", str(gateway_root))
    await asyncio.to_thread(command.upgrade, alembic, "head")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
        # Old prototype databases may have a lingering running run after a crash.
        await connection.execute(text("UPDATE platform.agent_runs SET status = 'interrupted', finished_at = now() WHERE status = 'running'"))
    await RuntimeClient().health()
    yield

app = FastAPI(title="Pi SaaS Gateway", lifespan=lifespan)
for route in (auth.router, providers.router, profiles.router, workspaces.router, sessions.router): app.include_router(route, prefix="/api/v1")
