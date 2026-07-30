from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import text

from .api.v1 import auth, profiles, providers, sessions
from .db.models import Base
from .db.session import engine

@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as connection:
        await connection.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
        await connection.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(title="Pi SaaS Gateway", lifespan=lifespan)
for route in (auth.router, providers.router, profiles.router, sessions.router): app.include_router(route, prefix="/api/v1")
