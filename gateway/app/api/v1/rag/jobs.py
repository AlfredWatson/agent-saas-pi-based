# ruff: noqa: F403, F405
from .common import *  # noqa: F403


async def submit_jobs(
    kind: str,
    status_prefix: str,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: JobSubmit,
    user: User,
    db: AsyncSession,
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    documents = list(
        (
            await db.scalars(
                select(RagDocument)
                .where(
                    RagDocument.knowledge_base_id == kb.id,
                    RagDocument.id.in_(set(body.document_ids)),
                    RagDocument.status == "active",
                )
                .with_for_update()
            )
        ).all()
    )
    if len(documents) != len(set(body.document_ids)):
        raise HTTPException(404, "document_not_found")
    jobs = []
    for document in documents:
        current_status = getattr(document, f"{status_prefix}_status")
        if current_status in {"queued", "running", "succeeded"}:
            raise HTTPException(409, f"document_{status_prefix}_already_processed")
        if kind == "chunking" and (
            document.parsing_status != "succeeded"
            or not await db.scalar(
                select(DocumentBlock.id)
                .where(DocumentBlock.document_id == document.id)
                .limit(1)
            )
        ):
            raise HTTPException(409, "document_not_parsed")
        if kind in {"vectorization", "graph_extraction"} and (
            document.chunking_status != "succeeded"
            or not await db.scalar(
                select(Chunk.id).where(Chunk.document_id == document.id).limit(1)
            )
        ):
            raise HTTPException(409, "document_not_chunked")
        snapshot = {
            "generation": document.processing_generation,
            "knowledge_base_version": kb.version,
        }
        if kind == "chunking":
            snapshot.update(
                strategy=document.chunking_strategy, config=document.chunking_config
            )
        model_kind = (
            "embedding"
            if kind == "vectorization"
            or (kind == "chunking" and document.chunking_strategy == "semantic")
            else "llm"
        )
        if kind != "chunking" or document.chunking_strategy == "semantic":
            model = await db.scalar(
                select(RagModelConfig).where(
                    RagModelConfig.knowledge_base_id == kb.id,
                    RagModelConfig.kind == model_kind,
                )
            )
            if model is None:
                raise HTTPException(409, f"{model_kind}_model_required")
            snapshot["model_fingerprint"] = model.fingerprint
        job = await db.scalar(
            select(ProcessingJob)
            .where(
                ProcessingJob.document_id == document.id,
                ProcessingJob.kind == kind,
                ProcessingJob.status == "failed",
            )
            .order_by(ProcessingJob.created_at.desc())
            .limit(1)
        )
        if job is not None and job.config_snapshot == snapshot:
            job.status = "queued"
            job.progress_current = 0
            job.progress_total = 0
            job.message = "queued for retry"
            job.error = None
            job.leased_by = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.finished_at = None
        else:
            job = ProcessingJob(
                knowledge_base_id=kb.id,
                document_id=document.id,
                kind=kind,
                status="queued",
                config_snapshot=snapshot,
                idempotency_key=uuid4().hex,
                message="queued",
            )
            db.add(job)
        setattr(document, f"{status_prefix}_status", "queued")
        setattr(document, f"{status_prefix}_progress", 0)
        setattr(document, f"{status_prefix}_message", "queued")
        setattr(document, f"{status_prefix}_error", None)
        setattr(document, f"{status_prefix}_updated_at", func.now())
        jobs.append(job)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "processing_job_already_active") from exc
    return {"job_ids": [str(job.id) for job in jobs]}


@router.post(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/jobs/parsing",
    status_code=202,
)
async def submit_parsing(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: ParsingJobSubmit,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    requested = {item.document_id: item.processor_backend for item in body.items}
    if len(requested) != len(body.items):
        raise HTTPException(422, "duplicate_document_id")
    documents = list(
        (
            await db.scalars(
                select(RagDocument)
                .where(
                    RagDocument.knowledge_base_id == kb.id,
                    RagDocument.id.in_(requested),
                    RagDocument.status == "active",
                )
                .with_for_update()
            )
        ).all()
    )
    if len(documents) != len(requested):
        raise HTTPException(404, "document_not_found")
    jobs = []
    settings = get_settings()
    for document in documents:
        if document.parsing_status in {"queued", "running", "succeeded"}:
            raise HTTPException(409, "document_parsing_already_processed")
        if await db.scalar(
            select(DocumentBlock.id)
            .where(DocumentBlock.document_id == document.id)
            .limit(1)
        ):
            raise HTTPException(409, "document_blocks_already_exist")
        backend = requested[document.id] or settings.document_processing_service
        if backend != settings.document_processing_service:
            raise HTTPException(422, "unsupported_document_processor")
        snapshot = {
            "generation": document.processing_generation,
            "knowledge_base_version": kb.version,
            "processor_backend": backend,
        }
        failed = await db.scalar(
            select(ProcessingJob)
            .where(
                ProcessingJob.document_id == document.id,
                ProcessingJob.kind == "parsing",
                ProcessingJob.status == "failed",
            )
            .order_by(ProcessingJob.created_at.desc())
            .limit(1)
        )
        if failed is not None and failed.config_snapshot == snapshot:
            failed.status = "queued"
            failed.progress_current = failed.progress_total = 0
            failed.message, failed.error = "queued for retry", None
            failed.leased_by = failed.lease_expires_at = failed.heartbeat_at = None
            failed.finished_at = None
            job = failed
        else:
            job = ProcessingJob(
                knowledge_base_id=kb.id,
                document_id=document.id,
                kind="parsing",
                status="queued",
                config_snapshot=snapshot,
                idempotency_key=uuid4().hex,
                message="queued",
            )
            db.add(job)
        document.parsing_backend = backend
        document.parsing_status = "queued"
        document.parsing_progress = 0
        document.parsing_message = "queued"
        document.parsing_error = None
        document.parsing_updated_at = func.now()
        jobs.append(job)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "processing_job_already_active") from exc
    return {"job_ids": [str(job.id) for job in jobs]}


@router.post(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/jobs/chunking",
    status_code=202,
)
async def submit_chunking(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: JobSubmit,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return await submit_jobs(
        "chunking", "chunking", workspace_id, knowledge_base_id, body, user, db
    )


@router.post(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/jobs/vectorization",
    status_code=202,
)
async def submit_vectorization(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: JobSubmit,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return await submit_jobs(
        "vectorization",
        "vectorization",
        workspace_id,
        knowledge_base_id,
        body,
        user,
        db,
    )


@router.post(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/jobs/graph-extraction",
    status_code=202,
)
async def submit_graph_extraction(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: JobSubmit,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return await submit_jobs(
        "graph_extraction", "graph", workspace_id, knowledge_base_id, body, user, db
    )


@router.post(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/jobs/{kind}:batch",
    status_code=202,
)
async def submit_batch_jobs(
    kind: str,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: JobSubmit,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Best-effort workbench submission with an outcome for every row.

    The existing stage endpoints remain all-or-nothing for API compatibility;
    this explicit workbench endpoint queues each selected document separately.
    """
    if kind not in {"parsing", "chunking", "vectorization", "graph-extraction"}:
        raise HTTPException(404, "job_kind_not_found")
    items: list[dict] = []
    for document_id in body.document_ids:
        try:
            if kind == "parsing":
                result = await submit_parsing(
                    workspace_id,
                    knowledge_base_id,
                    ParsingJobSubmit(items=[{"document_id": document_id}]),
                    user,
                    db,
                )
            elif kind == "chunking":
                result = await submit_chunking(
                    workspace_id, knowledge_base_id, JobSubmit(document_ids=[document_id]), user, db
                )
            elif kind == "vectorization":
                result = await submit_vectorization(
                    workspace_id, knowledge_base_id, JobSubmit(document_ids=[document_id]), user, db
                )
            else:
                result = await submit_graph_extraction(
                    workspace_id, knowledge_base_id, JobSubmit(document_ids=[document_id]), user, db
                )
            items.append({"document_id": str(document_id), "status": "queued", "job_ids": result["job_ids"]})
        except HTTPException as exc:
            await db.rollback()
            items.append({"document_id": str(document_id), "status": "failed", "error_code": str(exc.detail)})
    return {
        "queued": sum(item["status"] == "queued" for item in items),
        "failed": sum(item["status"] == "failed" for item in items),
        "items": items,
    }


@router.get("/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/jobs")
async def list_jobs(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id)
    jobs = (
        await db.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.knowledge_base_id == kb.id)
            .order_by(ProcessingJob.created_at.desc())
        )
    ).all()
    return {
        "items": [
            {
                "id": str(job.id),
                "document_id": str(job.document_id),
                "kind": job.kind,
                "status": job.status,
                "attempt": job.attempt,
                "progress": {
                    "current": job.progress_current,
                    "total": job.progress_total,
                },
                "message": job.message,
                "error": job.error,
            }
            for job in jobs
        ]
    }
