"""Authenticated Runtime access to the existing RAG retrieval service."""

from __future__ import annotations

from hashlib import sha256
from hmac import compare_digest
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import (
    AgentSession,
    AgentSessionKnowledgeBase,
    RuntimeInstance,
    User,
)
from ...db.rag.models import KnowledgeBase, RagDocument
from ...db.session import get_db
from ...domain.rag.schemas import RetrievalInput
from ...services.rag.retrieval import retrieve


router = APIRouter(
    prefix="/internal/v1/runtime-rag", tags=["internal"], include_in_schema=False
)
bearer = HTTPBearer(auto_error=False)


class RuntimeRetrievalInput(BaseModel):
    session_id: UUID
    knowledge_base_id: UUID
    query: str = Field(min_length=1, max_length=10_000)
    mode: str = Field(default="hybrid", pattern="^(vector|hybrid|graph)$")
    rerank: bool = True
    document_ids: list[UUID] | None = Field(default=None, max_length=100)
    top_k: int = Field(default=5, ge=1, le=20)


async def authenticated_runtime_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    tenant_id: UUID | None = Header(default=None, alias="X-Tenant-ID"),
    db: AsyncSession = Depends(get_db),
) -> User:
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or tenant_id is None
    ):
        raise HTTPException(401, "invalid_runtime_credential")
    instance = await db.scalar(
        select(RuntimeInstance).where(RuntimeInstance.user_id == tenant_id)
    )
    expected = instance.rag_secret_digest if instance else None
    actual = sha256(credentials.credentials.encode()).digest()
    if expected is None or not compare_digest(expected, actual):
        raise HTTPException(401, "invalid_runtime_credential")
    user = await db.scalar(
        select(User).where(User.id == tenant_id, User.status == "active")
    )
    if user is None:
        raise HTTPException(401, "invalid_runtime_credential")
    return user


@router.post("/retrieve")
async def retrieve_for_runtime(
    body: RuntimeRetrievalInput,
    user: User = Depends(authenticated_runtime_user),
    db: AsyncSession = Depends(get_db),
):
    session = await db.scalar(
        select(AgentSession).where(
            AgentSession.id == body.session_id,
            AgentSession.user_id == user.id,
        )
    )
    if session is None:
        raise HTTPException(404, "rag_resource_not_found")
    binding = await db.scalar(
        select(AgentSessionKnowledgeBase).where(
            AgentSessionKnowledgeBase.session_id == session.id,
            AgentSessionKnowledgeBase.knowledge_base_id == body.knowledge_base_id,
        )
    )
    knowledge_base = await db.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == body.knowledge_base_id,
            KnowledgeBase.user_id == user.id,
            KnowledgeBase.workspace_id == session.workspace_id,
            KnowledgeBase.status == "active",
        )
    )
    if binding is None or knowledge_base is None:
        raise HTTPException(404, "rag_resource_not_found")
    if body.document_ids:
        count = await db.scalar(
            select(func.count())
            .select_from(RagDocument)
            .where(
                RagDocument.knowledge_base_id == knowledge_base.id,
                RagDocument.id.in_(set(body.document_ids)),
            )
        )
        if count != len(set(body.document_ids)):
            raise HTTPException(422, "document_filter_outside_knowledge_base")
    try:
        result = await retrieve(
            db,
            knowledge_base.id,
            RetrievalInput(
                query=body.query,
                mode=body.mode,
                rerank=body.rerank,
                document_ids=body.document_ids,
                top_k=body.top_k,
            ),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "knowledge_base": {"id": str(knowledge_base.id), "name": knowledge_base.name},
        "result": result,
    }
