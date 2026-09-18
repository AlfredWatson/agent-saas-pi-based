"""Narrow PostgreSQL queries shared by RAG application use cases."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Workspace
from app.db.rag.models import KnowledgeBase, RagDocument


class RagRepository:
    """RAG persistence access; transaction ownership stays with its caller."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace | None:
        return await self.db.scalar(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.user_id == user_id,
                Workspace.status == "active",
            )
        )

    async def knowledge_base(
        self,
        workspace_id: UUID,
        knowledge_base_id: UUID,
        user_id: UUID,
        *,
        lock: bool = False,
    ) -> KnowledgeBase | None:
        statement = select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.workspace_id == workspace_id,
            KnowledgeBase.user_id == user_id,
        )
        if lock:
            statement = statement.with_for_update()
        return await self.db.scalar(statement)

    async def document(
        self, knowledge_base_id: UUID, document_id: UUID, *, lock: bool = False
    ) -> RagDocument | None:
        statement = select(RagDocument).where(
            RagDocument.id == document_id,
            RagDocument.knowledge_base_id == knowledge_base_id,
        )
        if lock:
            statement = statement.with_for_update()
        return await self.db.scalar(statement)
