"""Extensible dispatch for document processing stages."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.rag.models import ProcessingJob


class StageHandler(Protocol):
    def execute(self, db: AsyncSession, job: ProcessingJob) -> Awaitable[None]: ...


class MethodStageHandler:
    """Adapter while stage implementations are incrementally extracted."""

    def __init__(
        self, method: Callable[[AsyncSession, ProcessingJob], Awaitable[None]]
    ):
        self._method = method

    def execute(self, db: AsyncSession, job: ProcessingJob) -> Awaitable[None]:
        return self._method(db, job)


def stage_handlers(worker) -> dict[str, StageHandler]:
    """The single registration point for supported durable job kinds."""
    return {
        "parsing": MethodStageHandler(worker.process_parsing),
        "chunking": MethodStageHandler(worker.process_chunking),
        "vectorization": MethodStageHandler(worker.process_vectorization),
        "graph_extraction": MethodStageHandler(worker.process_graph),
    }
