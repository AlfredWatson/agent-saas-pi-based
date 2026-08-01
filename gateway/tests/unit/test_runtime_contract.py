from datetime import UTC, datetime

from app.api.v1.runtime import render
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
