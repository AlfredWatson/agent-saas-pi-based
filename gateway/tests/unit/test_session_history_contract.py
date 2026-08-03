import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1.sessions import owned, project_message_end, public_tool_event


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


def test_public_sse_tool_names_remain_compatible_and_payloads_are_safe():
    started = public_tool_event({
        "type": "tool_started", "toolCallId": "call-1", "toolName": "read", "args": {"token": "provider-key"},
    }, ("provider-key",))
    completed = public_tool_event({
        "type": "tool_completed", "toolCallId": "call-1", "toolName": "read", "result": {"value": "provider-key"}, "isError": True,
    }, ("provider-key",))

    assert started == ("tool.started", {"tool": "read", "toolCallId": "call-1", "toolName": "read", "args": {"token": "[REDACTED]"}, "payload_truncated": False})
    assert completed == ("tool.completed", {"tool": "read", "toolCallId": "call-1", "toolName": "read", "result": {"value": "[REDACTED]"}, "isError": True, "payload_truncated": False})
