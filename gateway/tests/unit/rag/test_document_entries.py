import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1.rag import documents
from app.api.v1.rag import operations
from app.domain.rag.chunking import content_hash, token_count
from app.domain.rag.schemas import DerivedTextUpdate, KnowledgeBaseCopy


def test_edit_rejects_processed_or_busy_document(monkeypatch):
    kb = SimpleNamespace(id=uuid4(), status="active")
    document = SimpleNamespace(
        id=uuid4(),
        status="active",
        vectorization_status="succeeded",
        graph_status="not_started",
    )
    owned_kb = AsyncMock(return_value=kb)
    owned_document = AsyncMock(return_value=document)
    ensure_jobs = AsyncMock()
    monkeypatch.setattr(documents, "owned_kb", owned_kb)
    monkeypatch.setattr(documents, "owned_document", owned_document)
    monkeypatch.setattr(documents, "ensure_no_active_document_jobs", ensure_jobs)
    db = AsyncMock()
    workspace_id, user_id = uuid4(), uuid4()

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            documents.editable_document(db, workspace_id, kb.id, document.id, user_id)
        )
    assert (error.value.status_code, error.value.detail) == (
        409,
        "derived_data_edit_locked",
    )
    owned_kb.assert_awaited_once_with(db, workspace_id, kb.id, user_id, lock=True)
    owned_document.assert_awaited_once_with(db, kb.id, document.id, lock=True)

    document.vectorization_status = "not_started"
    document.graph_status = "failed"
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            documents.editable_document(db, workspace_id, kb.id, document.id, user_id)
        )
    assert error.value.detail == "derived_data_edit_locked"

    document.graph_status = "not_started"
    ensure_jobs.side_effect = HTTPException(409, "document_processing")
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            documents.editable_document(db, workspace_id, kb.id, document.id, user_id)
        )
    assert error.value.detail == "document_processing"


def test_block_edit_invalidates_chunks_and_preserves_parsing(monkeypatch):
    kb = SimpleNamespace(id=uuid4())
    document = SimpleNamespace(
        id=uuid4(),
        parsing_status="succeeded",
        chunking_status="succeeded",
        chunking_progress=100,
        chunking_message="done",
        chunking_error=None,
        processing_generation=3,
    )
    block = SimpleNamespace(
        id=uuid4(), ordinal=0, text="old", metadata_={}, content_hash="old"
    )
    db = AsyncMock()
    db.scalar.return_value = block
    monkeypatch.setattr(
        documents, "editable_document", AsyncMock(return_value=(kb, document))
    )
    cache = AsyncMock()
    monkeypatch.setattr(documents, "remove_document_cache", cache)

    result = asyncio.run(
        documents.update_document_block(
            uuid4(),
            kb.id,
            document.id,
            block.id,
            DerivedTextUpdate(text="new block"),
            SimpleNamespace(id=uuid4()),
            db,
        )
    )
    assert result["text"] == "new block"
    assert result["content_hash"] == content_hash("new block")
    assert document.parsing_status == "succeeded"
    assert document.chunking_status == "not_started"
    assert document.chunking_progress == 0
    assert document.processing_generation == 4
    db.execute.assert_awaited_once()
    cache.assert_awaited_once_with(kb.id, document.id)
    db.commit.assert_awaited_once()


def test_chunk_edit_recomputes_hash_and_token_count(monkeypatch):
    kb = SimpleNamespace(id=uuid4())
    document = SimpleNamespace(
        id=uuid4(), chunking_status="succeeded", processing_generation=2
    )
    chunk = SimpleNamespace(
        id=uuid4(),
        ordinal=0,
        block_id=uuid4(),
        text="old",
        token_count=1,
        metadata_={},
        strategy_snapshot={},
        content_hash="old",
    )
    db = AsyncMock()
    db.scalar.return_value = chunk
    monkeypatch.setattr(
        documents, "editable_document", AsyncMock(return_value=(kb, document))
    )
    cache = AsyncMock()
    monkeypatch.setattr(documents, "remove_document_cache", cache)

    result = asyncio.run(
        documents.update_document_chunk(
            uuid4(),
            kb.id,
            document.id,
            chunk.id,
            DerivedTextUpdate(text="new chunk body"),
            SimpleNamespace(id=uuid4()),
            db,
        )
    )
    assert result["content_hash"] == content_hash("new chunk body")
    assert result["token_count"] == token_count("new chunk body")
    assert document.processing_generation == 2
    cache.assert_awaited_once_with(kb.id, document.id)
    db.commit.assert_awaited_once()


def test_entry_from_another_document_cannot_be_edited(monkeypatch):
    kb = SimpleNamespace(id=uuid4())
    document = SimpleNamespace(id=uuid4(), parsing_status="succeeded")
    monkeypatch.setattr(
        documents, "editable_document", AsyncMock(return_value=(kb, document))
    )
    db = AsyncMock()
    db.scalar.return_value = None
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            documents.update_document_block(
                uuid4(),
                kb.id,
                document.id,
                uuid4(),
                DerivedTextUpdate(text="changed"),
                SimpleNamespace(id=uuid4()),
                db,
            )
        )
    assert (error.value.status_code, error.value.detail) == (404, "block_not_found")
    db.commit.assert_not_awaited()


def test_copy_rejects_queued_processing_jobs(monkeypatch):
    source = SimpleNamespace(id=uuid4(), status="active")
    monkeypatch.setattr(operations, "owned_kb", AsyncMock(return_value=source))
    db = AsyncMock()
    db.scalar.return_value = uuid4()
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            operations.copy_knowledge_base(
                uuid4(),
                source.id,
                KnowledgeBaseCopy(name="发布版"),
                SimpleNamespace(id=uuid4()),
                db,
            )
        )
    assert (error.value.status_code, error.value.detail) == (
        409,
        "knowledge_base_processing",
    )
    statement = str(
        db.scalar.await_args.args[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "'queued'" in statement and "'running'" in statement
    assert source.status == "active"
    db.commit.assert_not_awaited()
