# ruff: noqa: F401
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import current_user
from app.core.config import get_settings
from app.core.encryption import encrypt
from app.db.models import User, Workspace
from app.db.rag.models import (
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
from app.db.rag.repositories import RagRepository
from app.db.session import get_db
from app.domain.rag.graph import canonical_key, merge_property_maps
from app.domain.rag.schemas import (
    GraphMergeInput,
    JobSubmit,
    ChunkingConfigInput,
    KnowledgeBaseCopy,
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    ModelConfigInput,
    RerankerModelConfigInput,
    ParsingJobSubmit,
    RetrievalInput,
)
from app.integrations.rag.cache import RagCache
from app.integrations.rag.file_storage import get_file_storage
from app.integrations.rag.model_clients import (
    model_fingerprint,
    verify_embedding,
    verify_llm,
)
from app.integrations.rag.reranker import RerankerError, verify_reranker
from app.integrations.rag.processors import SUPPORTED_EXTENSIONS
from app.services.rag.retrieval import retrieve

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
    workspace = await RagRepository(db).workspace(workspace_id, user_id)
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
    kb = await RagRepository(db).knowledge_base(
        workspace_id, knowledge_base_id, user_id, lock=lock
    )
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
