# ruff: noqa: F403, F405
from .common import *  # noqa: F403


@router.post(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/copy",
    status_code=202,
)
async def copy_knowledge_base(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: KnowledgeBaseCopy,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    source = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if source.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    if await db.scalar(
        select(ProcessingJob.id)
        .where(
            ProcessingJob.knowledge_base_id == source.id,
            ProcessingJob.status.in_(("queued", "running")),
        )
        .limit(1)
    ):
        raise HTTPException(409, "knowledge_base_processing")
    target = KnowledgeBase(
        user_id=source.user_id,
        workspace_id=source.workspace_id,
        name=body.name.strip(),
        status="copying",
        version=source.version,
        file_backend=source.file_backend,
        block_backend=source.block_backend,
        chunk_backend=source.chunk_backend,
        vector_backend=source.vector_backend,
        graph_backend=source.graph_backend,
        parsing_concurrency=source.parsing_concurrency,
        chunking_concurrency=source.chunking_concurrency,
        embedding_concurrency=source.embedding_concurrency,
        graph_concurrency=source.graph_concurrency,
    )
    source.status = "copying"
    db.add(target)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "knowledge_base_exists") from exc
    operation = RagOperation(
        user_id=user.id,
        workspace_id=workspace_id,
        knowledge_base_id=source.id,
        kind="copy",
        payload={"target_knowledge_base_id": str(target.id)},
    )
    db.add(operation)
    await db.commit()
    return {
        "operation_id": str(operation.id),
        "target_knowledge_base_id": str(target.id),
        "status": operation.status,
    }


@router.delete(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}", status_code=202
)
async def delete_knowledge_base(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    kb.status = "deleting"
    await db.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.knowledge_base_id == kb.id,
            ProcessingJob.status.in_(("queued", "running")),
        )
        .values(
            status="failed",
            error="knowledge_base_deleted",
            message="cancelled because knowledge base was deleted",
            leased_by=None,
            lease_expires_at=None,
            finished_at=func.now(),
        )
    )
    operation = RagOperation(
        user_id=user.id,
        workspace_id=workspace_id,
        knowledge_base_id=kb.id,
        kind="delete",
        payload={},
    )
    db.add(operation)
    await db.commit()
    return {"operation_id": str(operation.id), "status": operation.status}


@router.get("/rag/operations/{operation_id}")
async def get_operation(
    operation_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    operation = await db.scalar(
        select(RagOperation).where(
            RagOperation.id == operation_id, RagOperation.user_id == user.id
        )
    )
    if operation is None:
        raise HTTPException(404, "operation_not_found")
    return {
        "id": str(operation.id),
        "kind": operation.kind,
        "status": operation.status,
        "attempt": operation.attempt,
        "message": operation.message,
        "error": operation.error,
        "payload": operation.payload,
    }
