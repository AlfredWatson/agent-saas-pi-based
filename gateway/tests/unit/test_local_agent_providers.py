import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import providers, sessions
from app.clients import agent_runtime
from app.clients.agent_runtime import LocalModelServiceError


class Database:
    def __init__(self, scalars=(), rows=()):
        self.scalar_values = list(scalars)
        self.rows = list(rows)
        self.added = []
        self.committed = False

    async def scalar(self, _statement):
        return self.scalar_values.pop(0)

    async def scalars(self, _statement):
        return SimpleNamespace(all=lambda: self.rows)

    def add(self, value):
        self.added.append(value)
        if hasattr(value, "id") and value.id is None:
            value.id = uuid4()

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def refresh(self, _value):
        pass


def test_old_runtime_discovery_route_reports_rebuild_required(monkeypatch):
    class HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=404, is_success=False)

    monkeypatch.setattr(agent_runtime.RuntimeClient, "_base_url", AsyncMock(return_value="http://127.0.0.1:3000"))
    monkeypatch.setattr(agent_runtime.httpx, "AsyncClient", lambda **_kwargs: HttpClient())
    with pytest.raises(LocalModelServiceError) as error:
        asyncio.run(agent_runtime.RuntimeClient().discover_local_models(str(uuid4()), "http://10.0.0.1:8000", ""))
    assert error.value.status_code == 503
    assert error.value.detail == "runtime_update_required"


def test_create_local_binding_discovers_models_without_requiring_key(monkeypatch):
    class Runtime:
        async def discover_local_models(self, _user, url, key):
            assert url == "http://10.1.2.3:8000"
            assert key == ""
            return {"base_url": "http://10.1.2.3:8000/v1", "models": [{"id": "m", "name": "M"}]}

    monkeypatch.setattr(providers, "RuntimeClient", Runtime)
    db = Database()
    result = asyncio.run(providers.create_binding(
        SimpleNamespace(id=uuid4()),
        providers.BindingInput(provider_id="vllm", display_name="local", base_url="http://10.1.2.3:8000"),
        SimpleNamespace(id=uuid4()), db,
    ))
    assert result["base_url"] == "http://10.1.2.3:8000/v1"
    assert db.committed
    assert len(db.added) == 2
    assert db.added[1].status == "pending"
    assert db.added[0].ciphertext and db.added[0].nonce


def test_failed_discovery_does_not_store_binding(monkeypatch):
    class Runtime:
        async def discover_local_models(self, *_args):
            raise LocalModelServiceError(503, "model_service_unavailable")

    monkeypatch.setattr(providers, "RuntimeClient", Runtime)
    db = Database()
    with pytest.raises(HTTPException) as error:
        asyncio.run(providers.create_binding(
            SimpleNamespace(id=uuid4()),
            providers.BindingInput(provider_id="sglang", display_name="local", base_url="http://10.0.0.2"),
            SimpleNamespace(id=uuid4()), db,
        ))
    assert error.value.detail == "model_service_unavailable"
    assert db.added == [] and not db.committed


def test_configure_and_refresh_local_model(monkeypatch):
    binding = SimpleNamespace(id=uuid4(), provider_id="sglang", base_url="http://10.0.0.2/v1", ciphertext=b"x", nonce=b"y")
    model = SimpleNamespace(binding_id=binding.id, model_id="old", name="Old", status="pending", context_window=None, max_tokens=None, reasoning=None)
    monkeypatch.setattr(providers, "owned_workspace", AsyncMock())
    user = SimpleNamespace(id=uuid4())
    db = Database(scalars=[binding, model], rows=[model])
    configured = asyncio.run(providers.configure_local_model(
        uuid4(), binding.id,
        providers.LocalModelConfigInput(model_id="old", context_window=8192, max_tokens=1024, reasoning=False),
        user, db,
    ))
    assert configured["status"] == "ready"
    assert configured["thinking_levels"] == []
    assert db.committed

    class Runtime:
        async def discover_local_models(self, *_args):
            return {"base_url": binding.base_url, "models": [{"id": "new", "name": "New"}]}

    monkeypatch.setattr(providers, "RuntimeClient", Runtime)
    monkeypatch.setattr(providers, "decrypt", lambda *_args: "")
    db = Database(scalars=[binding], rows=[model])
    refreshed = asyncio.run(providers.refresh_local_models(uuid4(), binding.id, user, db))
    assert model.status == "unavailable"
    assert model.context_window == 8192
    assert any(item.model_id == "new" and item.status == "pending" for item in db.added)
    assert refreshed["models"][0]["status"] == "unavailable"


def test_session_cannot_select_disappeared_model(monkeypatch):
    binding = SimpleNamespace(id=uuid4(), provider_id="vllm")
    gone = SimpleNamespace(status="unavailable")
    db = Database(scalars=[binding, gone], rows=[])
    with pytest.raises(HTTPException) as error:
        asyncio.run(sessions.validate_model_config(
            db, uuid4(), uuid4(),
            sessions.SessionModelConfigInput(provider_binding_id=binding.id, model_id="gone"),
        ))
    assert error.value.status_code == 409
    assert error.value.detail == "model_unavailable"


def test_refresh_failure_preserves_catalog(monkeypatch):
    binding = SimpleNamespace(id=uuid4(), provider_id="vllm", base_url="http://10.0.0.2/v1", ciphertext=b"x", nonce=b"y")
    model = SimpleNamespace(model_id="m", status="ready", context_window=8192)
    db = Database(scalars=[binding], rows=[model])
    monkeypatch.setattr(providers, "owned_workspace", AsyncMock())
    monkeypatch.setattr(providers, "decrypt", lambda *_args: "")

    class Runtime:
        async def discover_local_models(self, *_args):
            raise LocalModelServiceError(503, "model_service_unavailable")

    monkeypatch.setattr(providers, "RuntimeClient", Runtime)
    with pytest.raises(HTTPException) as error:
        asyncio.run(providers.refresh_local_models(uuid4(), binding.id, SimpleNamespace(id=uuid4()), db))
    assert error.value.detail == "model_service_unavailable"
    assert model.status == "ready"
    assert not db.committed
