from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.rag.models import RagDocument


class FileStorageBackend(Protocol):
    async def put(self, document: RagDocument, content: bytes) -> None: ...

    async def read(self, document: RagDocument) -> bytes: ...

    async def delete(self, document: RagDocument) -> None: ...

    async def copy(self, source: RagDocument, target: RagDocument) -> None: ...


class PostgresFileStorage:
    """PostgreSQL bytea file store, intentionally hidden behind an adapter."""

    def __init__(self, _: AsyncSession):
        pass

    async def put(self, document: RagDocument, content: bytes) -> None:
        document.content = content

    async def read(self, document: RagDocument) -> bytes:
        if document.content is None:
            raise RuntimeError("postgresql_document_content_missing")
        return document.content

    async def delete(self, document: RagDocument) -> None:
        document.content = None

    async def copy(self, source: RagDocument, target: RagDocument) -> None:
        if source.content is None:
            raise RuntimeError("postgresql_document_content_missing")
        target.content = source.content


def get_file_storage(backend: str, db: AsyncSession) -> FileStorageBackend:
    factories: dict[str, type[PostgresFileStorage]] = {
        "postgresql": PostgresFileStorage
    }
    try:
        return factories[backend](db)
    except KeyError as exc:
        raise ValueError(f"unsupported_file_backend:{backend}") from exc
