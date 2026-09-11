from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .cache import RagCache
from .models import KnowledgeBase, ProcessingJob, RagDocument


async def delete_workspace_rag_data(
    db: AsyncSession, workspace_id: UUID, user_id: UUID
) -> None:
    knowledge_bases = list(
        (
            await db.scalars(
                select(KnowledgeBase)
                .where(
                    KnowledgeBase.workspace_id == workspace_id,
                    KnowledgeBase.user_id == user_id,
                )
                .with_for_update()
            )
        ).all()
    )
    if not knowledge_bases:
        return
    kb_ids = [item.id for item in knowledge_bases]
    for item in knowledge_bases:
        item.status = "deleting"
    await db.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.knowledge_base_id.in_(kb_ids),
            ProcessingJob.status.in_(("queued", "running")),
        )
        .values(
            status="failed",
            error="workspace_deleted",
            message="cancelled because workspace was deleted",
            leased_by=None,
            lease_expires_at=None,
            finished_at=func.now(),
        )
    )
    rows = (
        await db.execute(
            select(RagDocument.knowledge_base_id, RagDocument.id).where(
                RagDocument.knowledge_base_id.in_(kb_ids)
            )
        )
    ).all()
    cache = RagCache()
    try:
        for knowledge_base_id, document_id in rows:
            await cache.delete_document(knowledge_base_id, document_id)
    finally:
        await cache.close()
    await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids)))
