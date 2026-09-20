# ruff: noqa: F403, F405
from .common import *  # noqa: F403
from app.integrations.rag.vector_store import get_vector_store


@router.get("/rag/capabilities")
async def capabilities(_: User = Depends(current_user)):
    settings = get_settings()
    return {
        "file_backends": ["postgresql"],
        "block_backends": ["postgresql"],
        "chunk_backends": ["postgresql"],
        "document_processing_backends": ["default"],
        "vector_backends": list(settings.enabled_vector_backends),
        "graph_backends": ["postgresql"],
        "document_extensions": sorted(SUPPORTED_EXTENSIONS),
        "chunking_strategies": {
            strategy: default_chunk_config(strategy)
            for strategy in ("fixed", "regex", "semantic")
        },
    }


@router.post("/workspaces/{workspace_id}/knowledge-bases", status_code=201)
async def create_knowledge_base(
    workspace_id: UUID,
    body: KnowledgeBaseCreate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await owned_workspace(db, workspace_id, user.id)
    settings = get_settings()
    if body.vector_backend not in settings.enabled_vector_backends:
        raise HTTPException(422, "vector_backend_not_enabled")
    kb = KnowledgeBase(
        user_id=user.id,
        workspace_id=workspace_id,
        name=body.name.strip(),
        file_backend=body.file_backend,
        block_backend=body.block_backend,
        chunk_backend=body.chunk_backend,
        vector_backend=body.vector_backend,
        graph_backend=body.graph_backend,
        parsing_concurrency=settings.rag_default_parsing_concurrency,
        chunking_concurrency=settings.rag_default_chunking_concurrency,
        embedding_concurrency=settings.rag_default_embedding_concurrency,
        graph_concurrency=settings.rag_default_graph_concurrency,
    )
    db.add(kb)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "knowledge_base_exists") from exc
    return render_kb(kb)


@router.get("/workspaces/{workspace_id}/knowledge-bases")
async def list_knowledge_bases(
    workspace_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await owned_workspace(db, workspace_id, user.id)
    rows = (
        await db.scalars(
            select(KnowledgeBase)
            .where(
                KnowledgeBase.workspace_id == workspace_id,
                KnowledgeBase.user_id == user.id,
            )
            .order_by(KnowledgeBase.created_at)
        )
    ).all()
    return {"items": [render_kb(item) for item in rows]}


@router.get("/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}")
async def get_knowledge_base(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return render_kb(await owned_kb(db, workspace_id, knowledge_base_id, user.id))


@router.patch("/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}")
async def update_knowledge_base(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: KnowledgeBaseUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    settings = get_settings()
    maxima = {
        "parsing_concurrency": settings.rag_max_parsing_concurrency,
        "chunking_concurrency": settings.rag_max_chunking_concurrency,
        "embedding_concurrency": settings.rag_max_embedding_concurrency,
        "graph_concurrency": settings.rag_max_graph_concurrency,
    }
    values = body.model_dump(exclude_unset=True)
    for field, maximum in maxima.items():
        if field in values and values[field] > maximum:
            raise HTTPException(422, f"{field}_exceeds_system_maximum")
    for field in (
        "name",
        "parsing_concurrency",
        "chunking_concurrency",
        "embedding_concurrency",
        "graph_concurrency",
    ):
        if field in values:
            setattr(
                kb, field, values[field].strip() if field == "name" else values[field]
            )
    kb.version += 1
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "knowledge_base_exists") from exc
    return render_kb(kb)


async def set_model(
    kind: str,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: ModelConfigInput,
    user: User,
    db: AsyncSession,
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    active_kind = "vectorization" if kind == "embedding" else "graph_extraction"
    if await db.scalar(
        select(ProcessingJob.id)
        .where(
            ProcessingJob.knowledge_base_id == kb.id,
            ProcessingJob.kind == active_kind,
            ProcessingJob.status.in_(("queued", "running")),
        )
        .limit(1)
    ):
        raise HTTPException(409, f"{kind}_jobs_active")
    if kind == "embedding":
        semantic_jobs = (
            await db.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.knowledge_base_id == kb.id,
                    ProcessingJob.kind == "chunking",
                    ProcessingJob.status.in_(("queued", "running")),
                )
            )
        ).all()
        if any(
            job.config_snapshot.get("strategy") == "semantic" for job in semantic_jobs
        ):
            raise HTTPException(409, "semantic_chunking_jobs_active")
    if kind == "embedding" and await get_vector_store(kb, db).has_vectors():
        raise HTTPException(409, "embedding_model_locked_by_vectors")
    if kind == "embedding":
        dimension = await verify_embedding(body)
    else:
        await verify_llm(body)
        dimension = None
    config = await db.scalar(
        select(RagModelConfig).where(
            RagModelConfig.knowledge_base_id == kb.id, RagModelConfig.kind == kind
        )
    )
    if config is None:
        config = RagModelConfig(
            knowledge_base_id=kb.id,
            kind=kind,
            protocol=body.protocol,
            base_url=body.base_url.rstrip("/"),
            model_name=body.model_name,
            thinking_effort=body.thinking_effort,
            ciphertext=b"",
            nonce=b"",
            fingerprint=model_fingerprint(kind, body),
            embedding_dimension=dimension,
        )
        db.add(config)
        await db.flush()
    else:
        config.protocol = body.protocol
        config.base_url = body.base_url.rstrip("/")
        config.model_name = body.model_name
        config.thinking_effort = body.thinking_effort
        config.fingerprint = model_fingerprint(kind, body)
        config.embedding_dimension = dimension
        config.verified_at = func.now()
    aad = f"rag:{kb.id}:{kind}:{config.id}:{body.protocol}".encode()
    config.ciphertext, config.nonce = encrypt(body.api_key, aad)
    kb.version += 1
    document_ids = (
        await db.scalars(
            select(RagDocument.id).where(RagDocument.knowledge_base_id == kb.id)
        )
    ).all()
    cache = RagCache()
    try:
        for document_id in document_ids:
            await cache.delete_kind(
                kb.id, document_id, "vector" if kind == "embedding" else "graph-v1"
            )
    finally:
        await cache.close()
    await db.commit()
    return {
        "kind": kind,
        "protocol": config.protocol,
        "base_url": config.base_url,
        "model_name": config.model_name,
        "thinking_effort": config.thinking_effort,
        "embedding_dimension": config.embedding_dimension,
        "fingerprint": config.fingerprint,
        "verified_at": config.verified_at,
    }


@router.put(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/embedding-model"
)
async def set_embedding_model(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: ModelConfigInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.protocol != "openai":
        raise HTTPException(422, "embedding_protocol_must_be_openai")
    try:
        return await set_model(
            "embedding", workspace_id, knowledge_base_id, body, user, db
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/llm-model")
async def set_llm_model(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: ModelConfigInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await set_model("llm", workspace_id, knowledge_base_id, body, user, db)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/models")
async def list_rag_models(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id)
    rows = (
        await db.scalars(
            select(RagModelConfig).where(RagModelConfig.knowledge_base_id == kb.id)
        )
    ).all()
    return {
        "items": [
            {
                "kind": item.kind,
                "protocol": item.protocol,
                "base_url": item.base_url,
                "model_name": item.model_name,
                "thinking_effort": item.thinking_effort,
                "embedding_dimension": item.embedding_dimension,
                "fingerprint": item.fingerprint,
                "verified_at": item.verified_at,
            }
            for item in rows
        ]
    }


def sanitized_stored_filename(user_id: UUID, raw_filename: str) -> tuple[str, str, str]:
    """Return client filename, safe persisted filename, and normalized suffix."""
    original = Path((raw_filename or "unnamed").replace("\\", "/")).name or "unnamed"
    suffix = Path(original).suffix.lower()
    stem = Path(original).stem
    stem = re.sub(r"[^\w.-]+", "_", stem, flags=re.UNICODE)
    stem = re.sub(r"_+", "_", stem).strip("._") or "unnamed"
    # Keep persisted names bounded even for a maximal UTF-8 client filename.
    stem = stem[:512]
    date = datetime.now(UTC).strftime("%y%m%d")
    return original, f"{user_id}_{date}_{stem}{suffix}", suffix
