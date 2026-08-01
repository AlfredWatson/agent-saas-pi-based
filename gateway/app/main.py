from contextlib import asynccontextmanager
import asyncio
import logging
from pathlib import Path
from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .api.v1 import auth, profiles, providers, runtime, sessions, workspaces
from .clients.agent_runtime import RuntimeClient
from .core.config import get_settings
from .db.session import engine
from .services.runtime_locator import RuntimeUnavailableError

# Uvicorn configures this logger at INFO by default; application-module loggers
# otherwise inherit the root WARNING level and their startup messages are hidden.
logger = logging.getLogger("uvicorn.error")

@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    logger.info(
        "Business database connection: %s (schema=platform)",
        settings.database_url.render_as_string(hide_password=True),
    )
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


@app.exception_handler(RuntimeUnavailableError)
async def runtime_unavailable(_: Request, __: RuntimeUnavailableError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "runtime_unavailable"})


for route in (auth.router, providers.router, profiles.router, workspaces.router, runtime.router, sessions.router):
    app.include_router(route, prefix="/api/v1")
