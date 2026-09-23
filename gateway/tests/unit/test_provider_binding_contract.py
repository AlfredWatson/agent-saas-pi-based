import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import providers, sessions, workspaces
from app.clients.agent_runtime import RuntimeWorkspaceFileError


class ScalarDatabase:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.statements = []
        self.committed = False

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.responses.pop(0)

    async def commit(self):
        self.committed = True


def test_binding_name_is_required_before_runtime_validation():
    async def run():
        with pytest.raises(HTTPException) as error:
            await providers.create_binding(
                SimpleNamespace(id=uuid4()),
                providers.BindingInput(provider_id="faux", api_key="unused"),
                SimpleNamespace(id=uuid4()),
                SimpleNamespace(),
            )
        assert error.value.status_code == 422
        assert error.value.detail == "invalid_binding_name"

        with pytest.raises(HTTPException) as whitespace:
            await providers.create_binding(
                SimpleNamespace(id=uuid4()),
                providers.BindingInput(provider_id="faux", display_name=" \t ", api_key="unused"),
                SimpleNamespace(id=uuid4()),
                SimpleNamespace(),
            )
        assert whitespace.value.detail == "invalid_binding_name"

    asyncio.run(run())


def test_binding_delete_rejects_current_session_reference(monkeypatch):
    binding = SimpleNamespace(id=uuid4(), status="active")
    database = ScalarDatabase(binding, uuid4())
    monkeypatch.setattr(providers, "owned_workspace", AsyncMock())

    async def run():
        with pytest.raises(HTTPException) as error:
            await providers.disable_workspace_binding(
                uuid4(), binding.id, SimpleNamespace(id=uuid4()), database
            )
        assert error.value.status_code == 409
        assert error.value.detail == "provider_binding_in_use"

    asyncio.run(run())
    assert binding.status == "active"
    assert database.committed is False


def test_binding_delete_allows_switched_sessions_and_is_idempotent(monkeypatch):
    binding = SimpleNamespace(id=uuid4(), status="active")
    database = ScalarDatabase(binding, None)
    monkeypatch.setattr(providers, "owned_workspace", AsyncMock())

    async def run():
        await providers.disable_workspace_binding(
            uuid4(), binding.id, SimpleNamespace(id=uuid4()), database
        )

    asyncio.run(run())
    assert binding.status == "disabled"
    assert database.committed is True


def test_session_model_validation_locks_active_binding(monkeypatch):
    binding = SimpleNamespace(id=uuid4(), provider_id="faux")
    database = ScalarDatabase(binding)

    class Runtime:
        async def models(self, _user_id, _provider_id):
            return [{"id": "faux-1", "thinking_levels": []}]

    monkeypatch.setattr(sessions, "RuntimeClient", Runtime)

    async def run():
        result = await sessions.validate_model_config(
            database, uuid4(), uuid4(),
            sessions.SessionModelConfigInput(provider_binding_id=binding.id, model_id="faux-1"),
        )
        assert result is binding

    asyncio.run(run())
    assert database.statements[0]._for_update_arg is not None


def test_workspace_download_returns_attachment_stream(monkeypatch):
    workspace = SimpleNamespace(storage_key="ws-test", status="active")
    monkeypatch.setattr(workspaces, "owned_workspace", AsyncMock(return_value=workspace))

    class Runtime:
        async def download_workspace_file(self, _user_id, _key, _path):
            async def content():
                yield b"hello"
            return 5, content()

    monkeypatch.setattr(workspaces, "RuntimeClient", Runtime)

    async def run():
        response = await workspaces.download_workspace_file(
            uuid4(), "nested/hello world.txt", SimpleNamespace(id=uuid4()), SimpleNamespace()
        )
        assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''hello%20world.txt"
        assert response.headers["content-length"] == "5"
        assert b"".join([chunk async for chunk in response.body_iterator]) == b"hello"

    asyncio.run(run())


def test_workspace_download_preserves_runtime_file_errors(monkeypatch):
    workspace = SimpleNamespace(storage_key="ws-test", status="active")
    monkeypatch.setattr(workspaces, "owned_workspace", AsyncMock(return_value=workspace))

    class Runtime:
        async def download_workspace_file(self, _user_id, _key, _path):
            raise RuntimeWorkspaceFileError(404, "file_not_found")

    monkeypatch.setattr(workspaces, "RuntimeClient", Runtime)

    async def run():
        with pytest.raises(HTTPException) as error:
            await workspaces.download_workspace_file(
                uuid4(), "missing.txt", SimpleNamespace(id=uuid4()), SimpleNamespace()
            )
        assert error.value.status_code == 404
        assert error.value.detail == "file_not_found"

    asyncio.run(run())
