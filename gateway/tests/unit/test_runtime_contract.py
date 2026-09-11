from datetime import UTC, datetime

import httpx
import pytest

from app.api.v1.runtime import render
from app.clients.agent_runtime import RuntimeClient, RuntimeWorkspaceFileError
from app.db.models import RuntimeInstance
from app.services.runtime_locator import RuntimeStatus


def test_runtime_status_is_docker_orchestration_only():
    status = RuntimeStatus("running", "pi-saas-agent-runtime:test", "container-id", None, datetime.now(UTC))

    assert render(status) == {
        "state": "running",
        "image": "pi-saas-agent-runtime:test",
        "container_id": "container-id",
        "last_error": None,
        "last_seen_at": status.last_seen_at,
    }
    assert "backend" not in RuntimeInstance.__table__.columns


def test_runtime_file_errors_preserve_the_public_status_and_detail():
    response = httpx.Response(409, json={"error": "file_exists"})

    with pytest.raises(RuntimeWorkspaceFileError) as error:
        RuntimeClient._raise_file_operation_error(response)

    assert (error.value.status_code, error.value.detail) == (409, "file_exists")
