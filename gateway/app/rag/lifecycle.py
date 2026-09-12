from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .cache import RagCache
from .file_storage import get_file_storage
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
    documents = (
        await db.scalars(
            select(RagDocument).where(RagDocument.knowledge_base_id.in_(kb_ids))
        )
    ).all()
    cache = RagCache()
    try:
        for document in documents:
            await cache.delete_document(document.knowledge_base_id, document.id)
            await get_file_storage(document.storage_backend, db).delete(document)
    finally:
        await cache.close()
    await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids)))
