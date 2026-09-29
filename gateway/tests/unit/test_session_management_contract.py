import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import sessions


def test_session_title_rejects_whitespace_after_normalization():
    class NeverCalled:
        async def scalar(self, _query):
            raise AssertionError("whitespace title must fail before querying")

    async def run() -> None:
        with pytest.raises(HTTPException) as error:
            await sessions.update_session(
                uuid4(),
                sessions.SessionTitleInput(title="   "),
                SimpleNamespace(id=uuid4()),
                NeverCalled(),
            )
        assert error.value.status_code == 422
        assert error.value.detail == "invalid_session_title"

    asyncio.run(run())


def test_rendered_session_keeps_legacy_fields_and_exposes_ui_metadata():
    session_id = uuid4()
    workspace_id = uuid4()
    profile_id = uuid4()
    run_id = uuid4()
    rendered = sessions.render_session(
        SimpleNamespace(
            id=session_id,
            status="ready",
            title="Plan UI",
            workspace_id=workspace_id,
            profile_id=profile_id,
            created_at="created",
            updated_at="updated",
            total_tokens=12,
            context_tokens=7,
        ),
        ["kb-b", "kb-a"],
        SimpleNamespace(
            id=run_id,
            status="completed",
            error=None,
            started_at="started",
            finished_at="finished",
        ),
    )
    assert rendered["id"] == str(session_id)
    assert rendered["knowledge_base_ids"] == ["kb-a", "kb-b"]
    assert rendered["workspace_id"] == str(workspace_id)
    assert rendered["profile_id"] == str(profile_id)
    assert rendered["total_tokens"] == 12
    assert rendered["context_tokens"] == 7
    assert rendered["latest_run"] == {
        "id": str(run_id),
        "status": "completed",
        "error": None,
        "started_at": "started",
        "finished_at": "finished",
    }


def test_rendered_session_exposes_workspace_model_configuration_and_run_snapshot():
    binding_id = uuid4()
    rendered = sessions.render_session(
        SimpleNamespace(
            id=uuid4(), status="ready", title=None, workspace_id=uuid4(), profile_id=None,
            provider_binding_id=binding_id, model_id="faux-1", thinking_level="low",
            created_at="created", updated_at="updated",
            total_tokens=0, context_tokens=0,
        ),
        [],
        SimpleNamespace(
            id=uuid4(), status="completed", error=None, started_at="started", finished_at="finished",
            provider_binding_id=binding_id, provider_id="faux", model_id="faux-1", thinking_level="low",
        ),
    )
    assert rendered["profile_id"] is None
    assert rendered["provider_binding_id"] == str(binding_id)
    assert rendered["model_configured"] is True
    assert rendered["latest_run"]["provider_id"] == "faux"
    assert rendered["latest_run"]["thinking_level"] == "low"


def test_token_snapshot_rejects_invalid_or_out_of_range_values():
    assert sessions.read_token_snapshot({"total_tokens": 0, "context_tokens": 0}) == (0, 0)
    assert sessions.read_token_snapshot({"total_tokens": 2**63 - 1, "context_tokens": 1}) == (2**63 - 1, 1)
    for value in (True, -1, 1.5, "3", 2**63):
        with pytest.raises(ValueError, match="invalid_session_token_stats"):
            sessions.read_token_snapshot({"total_tokens": value, "context_tokens": 0})
    with pytest.raises(ValueError, match="invalid_session_token_stats"):
        sessions.read_token_snapshot({"total_tokens": 1})


def test_idle_session_delete_removes_projection_rows_without_runtime(monkeypatch):
    session = SimpleNamespace(id=uuid4(), pi_session_file_key=None)

    class Database:
        def __init__(self) -> None:
            self.executed = 0
            self.deleted = None
            self.committed = False

        async def scalar(self, _query):
            return None

        async def scalars(self, _query):
            return SimpleNamespace(all=lambda: [])

        async def execute(self, _query):
            self.executed += 1

        async def delete(self, value):
            self.deleted = value

        async def commit(self):
            self.committed = True

    database = Database()
    monkeypatch.setattr(sessions, "owned", AsyncMock(return_value=session))

    async def run() -> None:
        assert await sessions.delete_session(session.id, SimpleNamespace(id=uuid4()), database) is None

    asyncio.run(run())
    assert database.executed == 3
    assert database.deleted is session
    assert database.committed
