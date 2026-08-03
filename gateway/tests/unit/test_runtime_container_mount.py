from types import SimpleNamespace
from uuid import uuid4

from app.services.runtime_locator import DockerRuntimeLocator


class FakeContainers:
    def __init__(self):
        self.args = None
        self.kwargs = None

    def run(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return SimpleNamespace(id="container-id", name="runtime")


def test_tenant_directory_is_mounted_at_the_runtime_data_root(tmp_path):
    user_id = uuid4()
    containers = FakeContainers()
    locator = object.__new__(DockerRuntimeLocator)
    locator.settings = SimpleNamespace(
        resolved_runtime_data_host_root=tmp_path / ".runtime-data",
        resolved_runtime_source_dir=tmp_path / "agent-runtime-src",
        runtime_docker_image="runtime:test",
        runtime_docker_network="runtime-network",
        runtime_shared_secret="runtime-secret",
        runtime_memory_limit="1g",
        runtime_nano_cpus=1_000_000_000,
        runtime_pids_limit=256,
    )
    locator.client = SimpleNamespace(containers=containers)
    locator._container_name = lambda _: "runtime"

    locator._create_container(user_id)

    assert containers.kwargs["environment"] == {
        "TENANT_ID": str(user_id),
        "RUNTIME_SHARED_SECRET": "runtime-secret",
    }
    assert containers.kwargs["volumes"][str(tmp_path / ".runtime-data" / "tenants" / str(user_id))] == {
        "bind": "/runtime-data",
        "mode": "rw",
    }
    assert (tmp_path / ".runtime-data" / "tenants" / str(user_id)).is_dir()
