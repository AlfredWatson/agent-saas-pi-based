import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import sessions
from app.api.v1.rag import operations


class DeleteDatabase:
    def __init__(self, bound_session_id=None):
        self.bound_session_id = bound_session_id
        self.statements = []
        self.executed = []
        self.added = []
        self.committed = False

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.bound_session_id

    async def execute(self, statement):
        self.executed.append(statement)

    def add(self, item):
        if item.kind == "delete":
            item.id = uuid4()
            item.status = "queued"
        self.added.append(item)

    async def commit(self):
        self.committed = True


def test_delete_rejects_a_knowledge_base_bound_to_a_session(monkeypatch):
    kb = SimpleNamespace(id=uuid4(), status="active")
    db = DeleteDatabase(bound_session_id=uuid4())
    monkeypatch.setattr(operations, "owned_kb", AsyncMock(return_value=kb))

    async def run():
        with pytest.raises(HTTPException) as error:
            await operations.delete_knowledge_base(
                uuid4(), kb.id, SimpleNamespace(id=uuid4()), db
            )
        assert error.value.status_code == 409
        assert error.value.detail == "knowledge_base_in_use"

    asyncio.run(run())
    assert kb.status == "active"
    assert db.executed == []
    assert db.added == []
    assert db.committed is False
    assert operations.owned_kb.await_args.kwargs["lock"] is True


def test_delete_unbound_knowledge_base_queues_operation(monkeypatch):
    kb = SimpleNamespace(id=uuid4(), status="active")
    db = DeleteDatabase()
    monkeypatch.setattr(operations, "owned_kb", AsyncMock(return_value=kb))

    async def run():
        return await operations.delete_knowledge_base(
            uuid4(), kb.id, SimpleNamespace(id=uuid4()), db
        )

    result = asyncio.run(run())
    assert kb.status == "deleting"
    assert len(db.executed) == 1
    assert len(db.added) == 1
    assert db.added[0].kind == "delete"
    assert db.committed is True
    assert result["status"] == "queued"


def test_session_creation_and_deletion_serialize_on_knowledge_base_lock(monkeypatch):
    workspace = SimpleNamespace(id=uuid4(), status="active")
    user = SimpleNamespace(id=uuid4())
    kb = SimpleNamespace(id=uuid4(), status="active")
    lock = asyncio.Lock()
    held = asyncio.Event()
    release_creation = asyncio.Event()
    bindings = []
    observed = []

    class SessionDatabase:
        async def scalar(self, statement):
            return workspace

        async def scalars(self, statement):
            assert statement._for_update_arg is not None
            assert list(statement._order_by_clauses)
            observed.append("create_locked")
            await lock.acquire()
            held.set()
            await release_creation.wait()
            return SimpleNamespace(all=lambda: [kb])

        def add(self, session):
            self.session = session

        async def flush(self):
            self.session.id = uuid4()

        def add_all(self, rows):
            self.rows = list(rows)

        async def commit(self):
            bindings.extend(self.rows)
            observed.append("create_committed")
            lock.release()

        async def refresh(self, session):
            pass

    class ConcurrentDeleteDatabase(DeleteDatabase):
        async def scalar(self, statement):
            observed.append("delete_checked_bindings")
            return bindings[0].session_id if bindings else None

    async def owned_kb(*args, **kwargs):
        assert kwargs["lock"] is True
        await lock.acquire()
        observed.append("delete_locked")
        return kb

    monkeypatch.setattr(sessions, "get_settings", lambda: SimpleNamespace(runtime_gateway_base_url="http://runtime"))
    monkeypatch.setattr(sessions, "render_session", lambda *args: {"id": str(args[0].id)})
    monkeypatch.setattr(operations, "owned_kb", owned_kb)

    async def run():
        create = asyncio.create_task(sessions.create_session(
            sessions.SessionInput(workspace_id=workspace.id, knowledge_base_ids=[kb.id]),
            user, SessionDatabase(),
        ))
        await held.wait()
        delete = asyncio.create_task(operations.delete_knowledge_base(
            workspace.id, kb.id, user, ConcurrentDeleteDatabase()
        ))
        await asyncio.sleep(0)
        assert not delete.done()
        release_creation.set()
        await create
        with pytest.raises(HTTPException) as error:
            await delete
        assert error.value.detail == "knowledge_base_in_use"

    asyncio.run(run())
    assert observed == ["create_locked", "create_committed", "delete_locked", "delete_checked_bindings"]
    assert kb.status == "active"
