# ruff: noqa: F403, F405
from .common import *  # noqa: F403
from .knowledge_bases import sanitized_stored_filename
from app.db.rag.repositories import RagRepository


@router.post(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents",
    status_code=200,
)
async def upload_documents(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    files: list[UploadFile] = File(),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id)
    settings = get_settings()
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    if not files or len(files) > settings.rag_upload_max_files:
        raise HTTPException(422, "invalid_upload_file_count")
    results: list[dict] = []
    try:
        for file in files:
            original, stored, extension = sanitized_stored_filename(
                user.id, file.filename or "unnamed"
            )
            try:
                if extension not in SUPPORTED_EXTENSIONS:
                    raise ValueError(f"unsupported_document_type:{extension or 'none'}")
                content = await file.read(
                    settings.rag_document_max_mb * 1024 * 1024 + 1
                )
                if not content:
                    raise ValueError("empty_document")
                if len(content) > settings.rag_document_max_mb * 1024 * 1024:
                    raise ValueError("document_too_large")
                document = RagDocument(
                    knowledge_base_id=kb.id,
                    original_filename=original,
                    stored_filename=stored,
                    content_type=file.content_type or "application/octet-stream",
                    extension=extension,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                    storage_backend=kb.file_backend,
                    storage_key=uuid4().hex,
                    chunking_strategy="fixed",
                    chunking_config=default_chunk_config("fixed"),
                )
                db.add(document)
                await db.flush()
                await get_file_storage(kb.file_backend, db).put(document, content)
                results.append(
                    {
                        "original_filename": original,
                        "status": "uploaded",
                        "document": document,
                    }
                )
            except ValueError as exc:
                results.append(
                    {
                        "original_filename": original,
                        "status": "failed",
                        "error_code": str(exc),
                        "message": str(exc),
                        "retryable": not str(exc).startswith(
                            "unsupported_document_type:"
                        ),
                    }
                )
        await db.commit()
        for item in results:
            if item["status"] == "uploaded":
                await db.refresh(item["document"])
        return {
            "uploaded": sum(item["status"] == "uploaded" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
            "items": [
                {
                    **item,
                    **(
                        {"document": render_document(item["document"])}
                        if item["status"] == "uploaded"
                        else {}
                    ),
                }
                for item in results
            ],
        }
    except Exception:
        await db.rollback()
        raise
    finally:
        for file in files:
            await file.close()


@router.get("/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents")
async def list_documents(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id)
    rows = (
        await db.scalars(
            select(RagDocument)
            .where(RagDocument.knowledge_base_id == kb.id)
            .order_by(RagDocument.created_at)
        )
    ).all()
    return {"items": [render_document(item) for item in rows]}


async def owned_document(
    db: AsyncSession, kb_id: UUID, document_id: UUID, *, lock: bool = False
) -> RagDocument:
    document = await RagRepository(db).document(kb_id, document_id, lock=lock)
    if document is None:
        raise HTTPException(404, "document_not_found")
    return document


@router.get(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
)
async def get_document(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id)
    return render_document(await owned_document(db, kb.id, document_id))


async def remove_document_cache(kb_id: UUID, document_id: UUID) -> None:
    cache = RagCache()
    try:
        await cache.delete_document(kb_id, document_id)
    finally:
        await cache.close()


async def remove_document_cache_kind(kb_id: UUID, document_id: UUID, kind: str) -> None:
    cache = RagCache()
    try:
        await cache.delete_kind(kb_id, document_id, kind)
    finally:
        await cache.close()


async def ensure_no_active_document_jobs(
    db: AsyncSession, document_id: UUID, kinds: tuple[str, ...]
) -> None:
    active = await db.scalar(
        select(ProcessingJob.id)
        .where(
            ProcessingJob.document_id == document_id,
            ProcessingJob.kind.in_(kinds),
            ProcessingJob.status.in_(("queued", "running")),
        )
        .limit(1)
    )
    if active:
        raise HTTPException(409, "document_processing")


@router.delete(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}",
    status_code=204,
)
async def delete_document(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    document = await owned_document(db, kb.id, document_id, lock=True)
    document.status = "deleting"
    await db.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.document_id == document.id,
            ProcessingJob.status.in_(("queued", "running")),
        )
        .values(
            status="failed",
            error="document_deleted",
            message="cancelled because document was deleted",
            leased_by=None,
            lease_expires_at=None,
            finished_at=func.now(),
        )
    )
    await delete_graph_for_document(db, document)
    await remove_document_cache(kb.id, document.id)
    await get_file_storage(document.storage_backend, db).delete(document)
    await db.delete(document)
    await db.commit()


async def delete_vectors_for_document(db: AsyncSession, document: RagDocument) -> None:
    await db.execute(delete(ChunkVector).where(ChunkVector.document_id == document.id))
    document.vectorization_status = "not_started"
    document.vectorization_progress = 0
    document.vectorization_message = None
    document.vectorization_error = None
    document.vectorization_updated_at = func.now()


async def delete_graph_for_document(db: AsyncSession, document: RagDocument) -> None:
    document_artifacts = list(
        await db.scalars(
            select(GraphArtifact).where(
                GraphArtifact.knowledge_base_id == document.knowledge_base_id,
                GraphArtifact.source_document_id == document.id,
            )
        )
    )
    artifact_ids = [item.id for item in document_artifacts]
    all_merged = (
        await db.scalars(
            select(GraphArtifact).where(
                GraphArtifact.knowledge_base_id == document.knowledge_base_id,
                GraphArtifact.kind == "merged",
            )
        )
    ).all()
    dependent_ids = {str(value) for value in artifact_ids}
    merged_artifacts = []
    changed = True
    while changed:
        changed = False
        for artifact in all_merged:
            if artifact in merged_artifacts:
                continue
            if dependent_ids.intersection(artifact.source_graph_ids):
                merged_artifacts.append(artifact)
                dependent_ids.add(str(artifact.id))
                changed = True
    artifacts = document_artifacts + merged_artifacts
    for artifact in artifacts:
        await db.delete(artifact)
    document.graph_status = "not_started"
    document.graph_progress = 0
    document.graph_message = None
    document.graph_error = None
    document.graph_updated_at = func.now()


@router.delete(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}/blocks",
    status_code=204,
)
async def delete_document_blocks(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    document = await owned_document(db, kb.id, document_id, lock=True)
    await ensure_no_active_document_jobs(
        db,
        document.id,
        ("parsing", "chunking", "vectorization", "graph_extraction"),
    )
    await delete_vectors_for_document(db, document)
    await delete_graph_for_document(db, document)
    await db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    await db.execute(
        delete(DocumentBlock).where(DocumentBlock.document_id == document.id)
    )
    document.processing_generation += 1
    for prefix in ("parsing", "chunking"):
        setattr(document, f"{prefix}_status", "not_started")
        setattr(document, f"{prefix}_progress", 0)
        setattr(document, f"{prefix}_message", None)
        setattr(document, f"{prefix}_error", None)
        setattr(document, f"{prefix}_updated_at", func.now())
    document.parsing_backend = None
    await remove_document_cache(kb.id, document.id)
    await db.commit()


@router.put(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}/chunking-config"
)
async def set_document_chunking_config(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
    body: ChunkingConfigInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    document = await owned_document(db, kb.id, document_id, lock=True)
    if document.chunking_status in {"queued", "running", "succeeded"}:
        raise HTTPException(409, "document_chunking_config_locked")
    document.chunking_strategy = body.strategy
    document.chunking_config = validate_chunk_config(body.strategy, body.config)
    await db.commit()
    await db.refresh(document)
    return render_document(document)


@router.delete(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}/vectors",
    status_code=204,
)
async def delete_document_vectors(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    document = await owned_document(db, kb.id, document_id, lock=True)
    await ensure_no_active_document_jobs(db, document.id, ("vectorization",))
    await delete_vectors_for_document(db, document)
    await remove_document_cache_kind(kb.id, document.id, "vector")
    await db.commit()


@router.delete(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}/graph",
    status_code=204,
)
async def delete_document_graph(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    document = await owned_document(db, kb.id, document_id, lock=True)
    await ensure_no_active_document_jobs(db, document.id, ("graph_extraction",))
    await delete_graph_for_document(db, document)
    await remove_document_cache_kind(kb.id, document.id, "graph-v1")
    await db.commit()


@router.delete(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}/chunks",
    status_code=204,
)
async def delete_document_chunks(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    document = await owned_document(db, kb.id, document_id, lock=True)
    await ensure_no_active_document_jobs(
        db, document.id, ("chunking", "vectorization", "graph_extraction")
    )
    await delete_vectors_for_document(db, document)
    await delete_graph_for_document(db, document)
    await db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    document.processing_generation += 1
    document.chunking_status = "not_started"
    document.chunking_progress = 0
    document.chunking_message = None
    document.chunking_error = None
    document.chunking_updated_at = func.now()
    await remove_document_cache(kb.id, document.id)
    await db.commit()
