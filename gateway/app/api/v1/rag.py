from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...core.config import get_settings
from ...core.encryption import encrypt
from ...db.models import User, Workspace
from ...db.session import get_db
from ...rag.cache import RagCache
from ...rag.file_storage import get_file_storage
from ...rag.graph import canonical_key, merge_property_maps
from ...rag.model_clients import model_fingerprint, verify_embedding, verify_llm
from ...rag.models import (
    Chunk,
    ChunkVector,
    DocumentBlock,
    GraphArtifact,
    GraphEdge,
    GraphEvidence,
    GraphNode,
    KnowledgeBase,
    ProcessingJob,
    RagDocument,
    RagModelConfig,
    RagOperation,
)
from ...rag.processors import SUPPORTED_EXTENSIONS
from ...rag.retrieval import retrieve
from ...rag.schemas import (
    GraphMergeInput,
    JobSubmit,
    ChunkingConfigInput,
    KnowledgeBaseCopy,
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    ModelConfigInput,
    ParsingJobSubmit,
    RetrievalInput,
)

router = APIRouter(tags=["rag"])


def default_chunk_config(strategy: str) -> dict:
    settings = get_settings()
    if strategy == "fixed":
        return {
            "max_token_size": settings.chunk_max_token_size,
            "overlap_token_size": settings.chunk_overlap_token_size,
            "split_by_character": settings.chunk_split_by_character,
        }
    if strategy == "regex":
        return {
            "max_token_size": settings.chunk_max_token_size,
            "re_expression": settings.chunk_re_expression,
        }
    return {
        "max_token_size": settings.chunk_max_token_size,
        "breakpoint_threshold": settings.chunk_breakpoint_threshold,
    }


def validate_chunk_config(strategy: str, supplied: dict | None) -> dict:
    config = {**default_chunk_config(strategy), **(supplied or {})}
    expected = {
        "fixed": {"max_token_size", "overlap_token_size", "split_by_character"},
        "regex": {"max_token_size", "re_expression"},
        "semantic": {"max_token_size", "breakpoint_threshold"},
    }[strategy]
    if set(config) != expected:
        raise HTTPException(422, "invalid_chunking_config")
    try:
        maximum = int(config["max_token_size"])
        if maximum < 32:
            raise ValueError
        if strategy == "fixed":
            overlap = int(config["overlap_token_size"])
            if (
                overlap < 0
                or overlap >= maximum
                or not str(config["split_by_character"])
            ):
                raise ValueError
        elif strategy == "regex":
            re.compile(str(config["re_expression"]))
        else:
            threshold = float(config["breakpoint_threshold"])
            if not 0 < threshold < 100:
                raise ValueError
    except (TypeError, ValueError, KeyError, re.error) as exc:
        raise HTTPException(422, "invalid_chunking_config") from exc
    return config


async def owned_workspace(
    db: AsyncSession, workspace_id: UUID, user_id: UUID
) -> Workspace:
    workspace = await db.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.user_id == user_id,
            Workspace.status == "active",
        )
    )
    if workspace is None:
        raise HTTPException(404, "workspace_not_found")
    return workspace


async def owned_kb(
    db: AsyncSession,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    user_id: UUID,
    *,
    lock: bool = False,
) -> KnowledgeBase:
    statement = select(KnowledgeBase).where(
        KnowledgeBase.id == knowledge_base_id,
        KnowledgeBase.workspace_id == workspace_id,
        KnowledgeBase.user_id == user_id,
    )
    if lock:
        statement = statement.with_for_update()
    kb = await db.scalar(statement)
    if kb is None:
        raise HTTPException(404, "knowledge_base_not_found")
    return kb


def render_kb(kb: KnowledgeBase) -> dict:
    return {
        "id": str(kb.id),
        "workspace_id": str(kb.workspace_id),
        "name": kb.name,
        "status": kb.status,
        "version": kb.version,
        "file_backend": kb.file_backend,
        "block_backend": kb.block_backend,
        "chunk_backend": kb.chunk_backend,
        "vector_backend": kb.vector_backend,
        "graph_backend": kb.graph_backend,
        "concurrency": {
            "parsing": kb.parsing_concurrency,
            "chunking": kb.chunking_concurrency,
            "embedding": kb.embedding_concurrency,
            "graph": kb.graph_concurrency,
        },
    }


def render_document(document: RagDocument) -> dict:
    def stage(name: str) -> dict:
        return {
            "status": getattr(document, f"{name}_status"),
            "progress": getattr(document, f"{name}_progress"),
            "message": getattr(document, f"{name}_message"),
            "error": getattr(document, f"{name}_error"),
            "updated_at": getattr(document, f"{name}_updated_at"),
        }

    return {
        "id": str(document.id),
        "original_filename": document.original_filename,
        "stored_filename": document.stored_filename,
        "storage_backend": document.storage_backend,
        "storage_key": document.storage_key,
        "content_type": document.content_type,
        "extension": document.extension,
        "sha256": document.sha256,
        "size_bytes": document.size_bytes,
        "status": document.status,
        "parsing_backend": document.parsing_backend,
        "chunking_strategy": document.chunking_strategy,
        "chunking_config": document.chunking_config,
        "stages": {
            "parsing": stage("parsing"),
            "chunking": stage("chunking"),
            "vectorization": stage("vectorization"),
            "graph": stage("graph"),
        },
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }


@router.get("/rag/capabilities")
async def capabilities(_: User = Depends(current_user)):
    return {
        "file_backends": ["postgresql"],
        "block_backends": ["postgresql"],
        "chunk_backends": ["postgresql"],
        "document_processing_backends": ["default"],
        "vector_backends": ["postgresql"],
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
    if kind == "embedding" and await db.scalar(
        select(ChunkVector.id).where(ChunkVector.knowledge_base_id == kb.id).limit(1)
    ):
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
    statement = select(RagDocument).where(
        RagDocument.id == document_id, RagDocument.knowledge_base_id == kb_id
    )
    if lock:
        statement = statement.with_for_update()
    document = await db.scalar(statement)
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


@router.post("/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/retrieve")
async def retrieve_from_knowledge_base(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: RetrievalInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id)
    if body.document_ids:
        count = await db.scalar(
            select(func.count())
            .select_from(RagDocument)
            .where(
                RagDocument.knowledge_base_id == kb.id,
                RagDocument.id.in_(set(body.document_ids)),
            )
        )
        if count != len(set(body.document_ids)):
            raise HTTPException(422, "document_filter_outside_knowledge_base")
    try:
        return await retrieve(db, kb.id, body)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/graphs:merge",
    status_code=201,
)
async def merge_graphs(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: GraphMergeInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    source_ids = set(body.graph_ids)
    sources = list(
        (
            await db.scalars(
                select(GraphArtifact).where(
                    GraphArtifact.knowledge_base_id == kb.id,
                    GraphArtifact.id.in_(source_ids),
                    GraphArtifact.status == "ready",
                )
            )
        ).all()
    )
    if len(sources) != len(source_ids):
        raise HTTPException(422, "graph_outside_knowledge_base")
    artifact = GraphArtifact(
        knowledge_base_id=kb.id,
        kind="merged",
        name=body.name,
        source_graph_ids=[str(value) for value in sorted(source_ids, key=str)],
    )
    db.add(artifact)
    await db.flush()
    node_map: dict[str, GraphNode] = {}
    source_node_to_key: dict[UUID, str] = {}
    for node in (
        await db.scalars(select(GraphNode).where(GraphNode.artifact_id.in_(source_ids)))
    ).all():
        key = canonical_key(node.entity_type, node.name)
        source_node_to_key[node.id] = key
        if key not in node_map:
            merged = GraphNode(
                artifact_id=artifact.id,
                canonical_key=key,
                name=node.name,
                entity_type=node.entity_type,
                description=node.description,
                properties=node.properties,
            )
            db.add(merged)
            node_map[key] = merged
        else:
            node_map[key].properties = merge_property_maps(
                node_map[key].properties, node.properties
            )
            descriptions = [
                value
                for value in (node_map[key].description, node.description)
                if value
            ]
            node_map[key].description = "\n".join(dict.fromkeys(descriptions))
    await db.flush()
    edge_map: dict[tuple[str, str, str], GraphEdge] = {}
    source_edge_to_merged: dict[UUID, GraphEdge] = {}
    for edge in (
        await db.scalars(select(GraphEdge).where(GraphEdge.artifact_id.in_(source_ids)))
    ).all():
        source_key = source_node_to_key[edge.source_node_id]
        target_key = source_node_to_key[edge.target_node_id]
        key = (source_key, edge.relation.casefold().strip(), target_key)
        if key not in edge_map:
            merged_edge = GraphEdge(
                artifact_id=artifact.id,
                source_node_id=node_map[source_key].id,
                target_node_id=node_map[target_key].id,
                relation=edge.relation,
                description=edge.description,
                properties=edge.properties,
            )
            db.add(merged_edge)
            edge_map[key] = merged_edge
        else:
            merged_edge = edge_map[key]
            merged_edge.properties = merge_property_maps(
                merged_edge.properties, edge.properties
            )
        source_edge_to_merged[edge.id] = edge_map[key]
    await db.flush()
    evidence_rows = (
        await db.scalars(
            select(GraphEvidence).where(GraphEvidence.artifact_id.in_(source_ids))
        )
    ).all()
    seen = set()
    for evidence in evidence_rows:
        node_id = (
            node_map[source_node_to_key[evidence.node_id]].id
            if evidence.node_id
            else None
        )
        edge_id = (
            source_edge_to_merged[evidence.edge_id].id if evidence.edge_id else None
        )
        key = (node_id, edge_id, evidence.document_id, evidence.chunk_id)
        if key in seen:
            continue
        seen.add(key)
        db.add(
            GraphEvidence(
                artifact_id=artifact.id,
                node_id=node_id,
                edge_id=edge_id,
                document_id=evidence.document_id,
                chunk_id=evidence.chunk_id,
                model_fingerprint=evidence.model_fingerprint,
            )
        )
    await db.commit()
    return {
        "id": str(artifact.id),
        "name": artifact.name,
        "kind": artifact.kind,
        "source_graph_ids": artifact.source_graph_ids,
    }


@router.get("/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/graphs")
async def list_graphs(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id)
    rows = (
        await db.scalars(
            select(GraphArtifact)
            .where(GraphArtifact.knowledge_base_id == kb.id)
            .order_by(GraphArtifact.created_at)
        )
    ).all()
    return {
        "items": [
            {
                "id": str(item.id),
                "name": item.name,
                "kind": item.kind,
                "source_document_id": str(item.source_document_id)
                if item.source_document_id
                else None,
                "source_graph_ids": item.source_graph_ids,
                "status": item.status,
            }
            for item in rows
        ]
    }


@router.get(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/graphs/{graph_id}"
)
async def get_graph(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    graph_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id)
    artifact = await db.scalar(
        select(GraphArtifact).where(
            GraphArtifact.id == graph_id, GraphArtifact.knowledge_base_id == kb.id
        )
    )
    if artifact is None:
        raise HTTPException(404, "graph_not_found")
    nodes = (
        await db.scalars(select(GraphNode).where(GraphNode.artifact_id == artifact.id))
    ).all()
    edges = (
        await db.scalars(select(GraphEdge).where(GraphEdge.artifact_id == artifact.id))
    ).all()
    return {
        "id": str(artifact.id),
        "name": artifact.name,
        "kind": artifact.kind,
        "nodes": [
            {
                "id": str(node.id),
                "name": node.name,
                "entity_type": node.entity_type,
                "description": node.description,
                "properties": node.properties,
            }
            for node in nodes
        ],
        "edges": [
            {
                "id": str(edge.id),
                "source_node_id": str(edge.source_node_id),
                "relation": edge.relation,
                "target_node_id": str(edge.target_node_id),
                "description": edge.description,
                "properties": edge.properties,
            }
            for edge in edges
        ],
    }


@router.delete(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/graphs/{graph_id}",
    status_code=204,
)
async def delete_graph(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    graph_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    artifact = await db.scalar(
        select(GraphArtifact)
        .where(GraphArtifact.id == graph_id, GraphArtifact.knowledge_base_id == kb.id)
        .with_for_update()
    )
    if artifact is None:
        raise HTTPException(404, "graph_not_found")
    if artifact.source_document_id:
        document = await db.get(RagDocument, artifact.source_document_id)
        if document:
            document.graph_status = "not_started"
            document.graph_progress = 0
            document.graph_message = None
            document.graph_error = None
            document.graph_updated_at = func.now()
    merged = (
        await db.scalars(
            select(GraphArtifact).where(
                GraphArtifact.knowledge_base_id == kb.id,
                GraphArtifact.kind == "merged",
            )
        )
    ).all()
    dependent_ids = {str(artifact.id)}
    dependent = []
    changed = True
    while changed:
        changed = False
        for item in merged:
            if item in dependent:
                continue
            if dependent_ids.intersection(item.source_graph_ids):
                dependent.append(item)
                dependent_ids.add(str(item.id))
                changed = True
    for item in dependent:
        await db.delete(item)
    await db.delete(artifact)
    await db.commit()


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
            ProcessingJob.status == "running",
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
