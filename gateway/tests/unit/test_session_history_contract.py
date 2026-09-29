import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.api.v1.sessions import MessageInput, consume_run, owned, project_message_end, public_compaction_event, public_tool_event, stream


class CapturingSession:
    def __init__(self) -> None:
        self.items = []

    def add(self, item) -> None:
        self.items.append(item)


def test_projection_uses_message_end_order_and_tool_association():
    async def run() -> tuple[int, CapturingSession]:
        db = CapturingSession()
        session_id = uuid4()
        run_id = uuid4()
        sequence = await project_message_end(db, session_id, run_id, {
            "role": "assistant",
            "content": [{"type": "text", "content": "checking"}, {"type": "tool_call", "tool_call_id": "call-1", "tool_name": "read", "args": {"path": "a.txt"}}],
        }, 1, ("provider-key",))
        sequence = await project_message_end(db, session_id, run_id, {
            "role": "tool_result", "tool_call_id": "call-1", "tool_name": "read", "content": "ok", "result": {"value": "ok"}, "is_error": False,
        }, sequence, ("provider-key",))
        return sequence, db

    sequence, db = asyncio.run(run())

    assert sequence == 4
    assert [item.role for item in db.items] == ["assistant", "tool_call", "tool_result"]
    assert db.items[1].tool_call_id == db.items[2].tool_call_id == "call-1"
    assert db.items[2].result == {"value": "ok"}


def test_foreign_session_is_not_visible_as_a_distinct_error():
    class NoSession:
        async def scalar(self, _query):
            return None

    async def run() -> None:
        with pytest.raises(HTTPException) as error:
            await owned(uuid4(), SimpleNamespace(id=uuid4()), NoSession())
        assert error.value.status_code == 404

    asyncio.run(run())


def test_second_running_run_is_reported_as_session_busy_when_flush_fails(monkeypatch):
    class BusySession:
        def __init__(self) -> None:
            self.scalar_calls = 0
            self.rolled_back = False

        async def scalar(self, _query):
            self.scalar_calls += 1
            # The route locks the Session's Workspace before it reads sequence.
            if self.scalar_calls == 1:
                return SimpleNamespace(id=session_id, workspace_id=workspace_id)
            if self.scalar_calls == 2:
                return SimpleNamespace(status="active")
            if self.scalar_calls == 3:
                return None  # no running parent or child Run
            return 3

        def add(self, _item) -> None:
            pass

        async def flush(self) -> None:
            raise IntegrityError("insert", {}, Exception("duplicate running session"))

        async def rollback(self) -> None:
            self.rolled_back = True

    async def run_test() -> None:
        db = BusySession()
        with pytest.raises(HTTPException) as error:
            await stream(session_id, MessageInput(content="next message"), SimpleNamespace(id=uuid4()), db)
        assert error.value.status_code == 409
        assert error.value.detail == "session_busy"
        assert db.rolled_back

    session_id = uuid4()
    workspace_id = uuid4()
    # This unit exercises the run uniqueness boundary; quota traversal has its
    # own contract tests and would otherwise start a real worker thread.
    monkeypatch.setattr(
        "app.api.v1.sessions.enforce_workspace_storage_limit", AsyncMock()
    )
    asyncio.run(run_test())


def test_public_sse_tool_names_remain_compatible_and_payloads_are_safe():
    started = public_tool_event({
        "type": "tool_started", "toolCallId": "call-1", "toolName": "read", "args": {"token": "provider-key"},
    }, ("provider-key",))
    completed = public_tool_event({
        "type": "tool_completed", "toolCallId": "call-1", "toolName": "read", "result": {"value": "provider-key"}, "isError": True,
    }, ("provider-key",))

    assert started == ("tool.started", {"tool": "read", "toolCallId": "call-1", "toolName": "read", "args": {"token": "[REDACTED]"}, "payload_truncated": False})
    assert completed == ("tool.completed", {"tool": "read", "toolCallId": "call-1", "toolName": "read", "result": {"value": "[REDACTED]"}, "isError": True, "payload_truncated": False})


def test_public_compaction_events_keep_order_and_drop_private_fields():
    events = [
        {"type": "compaction_started", "reason": "threshold", "summary": "private context"},
        {"type": "compaction_ended", "reason": "threshold", "status": "completed", "errorMessage": "provider-key"},
        {"type": "compaction_ended", "reason": "overflow", "status": "failed", "errorMessage": "provider-key"},
    ]
    projected = [public_compaction_event(event) for event in events]
    assert projected == [
        ("compaction.started", {"reason": "threshold"}),
        ("compaction.ended", {"reason": "threshold", "status": "completed"}),
        ("compaction.ended", {"reason": "overflow", "status": "failed"}),
    ]
    assert "provider-key" not in repr(projected)
    assert "private context" not in repr(projected)
    assert public_compaction_event({"type": "compaction_ended", "reason": "threshold", "status": "unknown"}) is None
    assert public_compaction_event({"type": "compaction_started", "reason": ["threshold"]}) is None


@pytest.mark.parametrize(
    ("assistant_stops", "expected_status", "expected_error"),
    [(["length"], "failed", "output_token_limit"), (["length", None], "completed", None)],
)
def test_run_reports_only_terminal_output_limit(monkeypatch, assistant_stops, expected_status, expected_error):
    session = SimpleNamespace(provider_binding_id=uuid4(), parent_session_id=None)
    run = SimpleNamespace(status="running", error=None, finished_at=None)
    binding = SimpleNamespace(verified_at=None)
    published = []

    class FakeDb:
        def __init__(self):
            self.results = iter([session, 0, binding])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def scalar(self, _query):
            return next(self.results)

        async def get(self, _model, _id):
            return run

        async def refresh(self, _item):
            pass

        async def scalars(self, _query):
            return SimpleNamespace(all=lambda: [])

        def add(self, _item):
            pass

        async def commit(self):
            pass

    class FakeRuntime:
        async def ensure_session(self, *_args):
            return {"pi_session_id": "pi-1", "session_file_key": "session.jsonl", "total_tokens": 0, "context_tokens": 0}

        async def stream_chat(self, *_args):
            for stop in assistant_stops:
                yield {"type": "message_end", "message": {"role": "assistant", "content": [], "stop_reason": stop}}
            yield {"type": "agent_settled"}

    async def capture(_run_id, name, data):
        published.append((name, data))

    monkeypatch.setattr("app.api.v1.sessions.SessionLocal", FakeDb)
    monkeypatch.setattr("app.api.v1.sessions.RuntimeClient", FakeRuntime)
    monkeypatch.setattr("app.api.v1.sessions.runtime_payload", AsyncMock(return_value={"api_key": ""}))
    monkeypatch.setattr("app.api.v1.sessions.publish", capture)
    asyncio.run(consume_run(uuid4(), uuid4(), uuid4(), "hello"))

    assert run.status == expected_status
    assert run.error == expected_error
    assert published[-2] == (
        ("message.failed", {"error": "output_token_limit"})
        if expected_error else ("message.completed", {})
    )
