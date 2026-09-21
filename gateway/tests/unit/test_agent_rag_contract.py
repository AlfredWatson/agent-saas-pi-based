import asyncio
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.internal.rag import RuntimeRetrievalInput, authenticated_runtime_user
from app.api.v1.sessions import SessionInput
from app.db.models import AgentSessionKnowledgeBase
from app.main import app


def test_session_contract_keeps_rag_binding_optional_and_bounded():
    profile_id = uuid4()
    assert SessionInput(profile_id=profile_id).knowledge_base_ids == []
    knowledge_base_id = uuid4()
    assert SessionInput(
        profile_id=profile_id, knowledge_base_ids=[knowledge_base_id]
    ).knowledge_base_ids == [knowledge_base_id]
    with pytest.raises(ValueError, match="duplicate_knowledge_base_id"):
        SessionInput(profile_id=profile_id, knowledge_base_ids=[knowledge_base_id] * 2)
    assert "knowledge_base_ids" in app.openapi()["components"]["schemas"]["SessionInput"]["properties"]


def test_internal_runtime_route_is_not_part_of_public_openapi():
    assert "/internal/v1/runtime-rag/retrieve" not in app.openapi()["paths"]


def test_runtime_retrieval_defaults_to_bounded_hybrid_search():
    payload = RuntimeRetrievalInput(
        session_id=uuid4(), knowledge_base_id=uuid4(), query="where is the guide"
    )
    assert payload.mode == "hybrid"
    assert payload.top_k == 5
    with pytest.raises(ValueError):
        RuntimeRetrievalInput(
            session_id=uuid4(), knowledge_base_id=uuid4(), query="x", top_k=21
        )


def test_session_kb_binding_has_cascade_foreign_keys():
    table = AgentSessionKnowledgeBase.__table__
    foreign_keys = {str(item.target_fullname) for item in table.foreign_keys}
    assert foreign_keys == {
        "platform.agent_sessions.id",
        "rag.knowledge_bases.id",
    }
    assert all(item.ondelete == "CASCADE" for item in table.foreign_keys)


def test_runtime_secret_authentication_is_tenant_scoped():
    tenant_id = uuid4()
    secret = "per-runtime-secret"

    class Db:
        def __init__(self) -> None:
            self.items = [
                SimpleNamespace(rag_secret_digest=sha256(secret.encode()).digest()),
                SimpleNamespace(id=tenant_id, status="active"),
            ]

        async def scalar(self, _statement):
            return self.items.pop(0)

    async def run() -> None:
        user = await authenticated_runtime_user(
            SimpleNamespace(scheme="Bearer", credentials=secret), tenant_id, Db()
        )
        assert user.id == tenant_id
        with pytest.raises(HTTPException) as error:
            await authenticated_runtime_user(
                SimpleNamespace(scheme="Bearer", credentials="wrong"), tenant_id,
                Db(),
            )
        assert error.value.status_code == 401

    asyncio.run(run())
