import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.internal.subagents import BatchInput, TaskInput, create_batch
from app.api.v1 import sessions
from app.api.v1.sessions import AgentConfigUpdateInput, owned, set_agent_config
from app.db.models import AgentRun, AgentSession, AgentSessionKnowledgeBase


def test_batch_creation_snapshots_sixteen_independent_children_and_retries():
    user = SimpleNamespace(id=uuid4())
    parent = SimpleNamespace(id=uuid4(), user_id=user.id, workspace_id=uuid4(), tools=["call_subagents"])
    parent_run = SimpleNamespace(id=uuid4(), provider_binding_id=uuid4(), provider_id="faux", model_id="faux-1", thinking_level="low")
    definition = SimpleNamespace(name="worker", description="Do work", system_prompt="Child instructions", tools=["read"])
    kb_id = uuid4()

    class Database:
        def __init__(self):
            self.children = []
            self.runs = []
            self.bindings = []
            self.scalar_calls = 0
            self.scalars_calls = 0
            self.commits = 0

        async def scalar(self, _query):
            self.scalar_calls += 1
            return parent if self.scalar_calls % 2 else parent_run

        async def scalars(self, _query):
            self.scalars_calls += 1
            position = (self.scalars_calls - 1) % (1 if self.children else 3)
            if self.children:
                values = self.children
            else:
                values = [[], [kb_id], [definition]][position]
            return SimpleNamespace(all=lambda: values)

        def add(self, value):
            if isinstance(value, AgentSession):
                self.children.append(value)
            elif isinstance(value, AgentRun):
                self.runs.append(value)

        def add_all(self, values):
            self.bindings.extend(values)

        async def flush(self):
            self.children[-1].id = uuid4()

        async def commit(self):
            self.commits += 1

    db = Database()
    body = BatchInput(parent_session_id=parent.id, tool_call_id="call-1", tasks=[TaskInput(name="worker", task=f"task {i}") for i in range(16)])

    async def run():
        first = await create_batch(body, user, db)
        second = await create_batch(body, user, db)
        return first, second

    first, second = asyncio.run(run())
    assert first == second
    assert len(first["tasks"]) == 16
    assert len(db.children) == len(db.runs) == len(db.bindings) == 16
    assert db.commits == 1
    assert {child.task_index for child in db.children} == set(range(16))
    assert all(child.parent_run_id == parent_run.id and child.tools == ["read"] for child in db.children)
    assert all(child.subagent_snapshot["system_prompt"] == "Child instructions" for child in db.children)
    assert all(run.status == "queued" for run in db.runs)
    assert all(isinstance(binding, AgentSessionKnowledgeBase) and binding.knowledge_base_id == kb_id for binding in db.bindings)


def test_public_session_lookup_excludes_children():
    class Database:
        async def scalar(self, query):
            assert "parent_session_id IS NULL" in str(query)
            return None

    with pytest.raises(HTTPException) as error:
        asyncio.run(owned(uuid4(), SimpleNamespace(id=uuid4()), Database()))
    assert error.value.status_code == 404


def test_configuration_update_rejects_stale_version_before_writing(monkeypatch):
    session = SimpleNamespace(id=uuid4(), config_version=2)

    async def fake_owned(*_args, **_kwargs):
        return session

    monkeypatch.setattr("app.api.v1.sessions.owned", fake_owned)
    body = AgentConfigUpdateInput(tools=["read"], expected_config_version=1)
    with pytest.raises(HTTPException) as error:
        asyncio.run(set_agent_config(session.id, body, SimpleNamespace(id=uuid4()), object()))
    assert error.value.status_code == 409
    assert error.value.detail == "session_config_conflict"


def test_child_run_timeout_marks_failure_and_aborts_runtime(monkeypatch):
    session = SimpleNamespace(parent_session_id=uuid4(), provider_binding_id=uuid4())
    run = SimpleNamespace(status="running", error=None, finished_at=None)
    aborted = []
    published = []

    class Database:
        def __init__(self):
            self.scalar_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def scalar(self, _query):
            self.scalar_calls += 1
            return session if self.scalar_calls == 1 else 1 if self.scalar_calls == 2 else None

        async def get(self, _model, _id):
            return run

        async def refresh(self, _item):
            pass

        async def commit(self):
            pass

        async def scalars(self, _query):
            return SimpleNamespace(all=lambda: [])

    class Runtime:
        async def ensure_session(self, *_args):
            return {"pi_session_id": "pi", "session_file_key": "child.jsonl", "total_tokens": 0, "context_tokens": 0}

        async def stream_chat(self, *_args):
            await asyncio.sleep(1)
            yield {"type": "agent_settled"}

        async def control(self, *_args):
            aborted.append(True)

    async def capture(_run_id, name, data):
        published.append((name, data))

    real_timeout = asyncio.timeout
    monkeypatch.setattr(sessions.asyncio, "timeout", lambda delay: real_timeout(0.001 if delay else None))
    monkeypatch.setattr(sessions, "SessionLocal", Database)
    monkeypatch.setattr(sessions, "RuntimeClient", Runtime)
    monkeypatch.setattr(sessions, "runtime_payload", lambda *_args: asyncio.sleep(0, result={"api_key": ""}))
    monkeypatch.setattr(sessions, "publish", capture)
    asyncio.run(sessions.consume_run(uuid4(), uuid4(), uuid4(), "task"))
    assert run.status == "failed"
    assert run.error == "subagent_timeout"
    assert aborted == [True]
    assert ("message.failed", {"error": "subagent_timeout"}) in published


def test_cancelled_child_run_is_not_overwritten_when_runtime_settles(monkeypatch):
    session = SimpleNamespace(parent_session_id=uuid4(), provider_binding_id=uuid4())
    run = SimpleNamespace(status="running", error="subagent_cancelled", finished_at=None)

    class Database:
        def __init__(self):
            self.scalar_calls = 0
            self.refresh_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def scalar(self, _query):
            self.scalar_calls += 1
            return session if self.scalar_calls == 1 else 1 if self.scalar_calls == 2 else None

        async def get(self, _model, _id):
            return run

        async def refresh(self, _item):
            self.refresh_calls += 1
            if self.refresh_calls == 2:
                run.status = "cancelled"

        async def commit(self):
            pass

        async def scalars(self, _query):
            return SimpleNamespace(all=lambda: [])

    class Runtime:
        async def ensure_session(self, *_args):
            return {"pi_session_id": "pi", "session_file_key": "child.jsonl", "total_tokens": 0, "context_tokens": 0}

        async def stream_chat(self, *_args):
            yield {"type": "agent_settled"}

    async def capture(*_args):
        pass

    monkeypatch.setattr(sessions, "SessionLocal", Database)
    monkeypatch.setattr(sessions, "RuntimeClient", Runtime)
    monkeypatch.setattr(sessions, "runtime_payload", lambda *_args: asyncio.sleep(0, result={"api_key": ""}))
    monkeypatch.setattr(sessions, "publish", capture)
    asyncio.run(sessions.consume_run(uuid4(), uuid4(), uuid4(), "task"))
    assert run.status == "cancelled"
    assert run.error == "subagent_cancelled"
