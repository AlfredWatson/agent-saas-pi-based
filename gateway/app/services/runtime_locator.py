import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import docker
import httpx
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from sqlalchemy import select

from ..core.config import Settings, get_settings
from ..db.models import AgentRun, RuntimeInstance
from ..db.session import SessionLocal


class RuntimeUnavailableError(RuntimeError):
    pass


class RuntimeBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeEndpoint:
    base_url: str
    state: str


@dataclass(frozen=True)
class RuntimeStatus:
    state: str
    image: str | None
    container_id: str | None
    last_error: str | None
    last_seen_at: datetime | None


class RuntimeLocator:
    async def preflight(self) -> None:
        raise NotImplementedError

    async def ensure(self, user_id: UUID) -> RuntimeEndpoint:
        raise NotImplementedError

    async def status(self, user_id: UUID) -> RuntimeStatus:
        raise NotImplementedError

    async def stop(self, user_id: UUID) -> RuntimeStatus:
        raise NotImplementedError

    async def recreate(self, user_id: UUID) -> RuntimeEndpoint:
        raise NotImplementedError


class DockerRuntimeLocator(RuntimeLocator):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = docker.from_env()
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def preflight(self) -> None:
        if not self.settings.resolved_runtime_source_dir.is_dir():
            raise RuntimeUnavailableError("runtime_source_dir_missing")
        await asyncio.to_thread(self.settings.resolved_runtime_data_host_root.mkdir, parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(self.client.ping)
            await asyncio.to_thread(self.client.images.get, self.settings.runtime_docker_image)
            await asyncio.to_thread(self._ensure_network)
        except (DockerException, ImageNotFound, OSError) as exc:
            raise RuntimeUnavailableError("docker_runtime_preflight_failed") from exc

    async def ensure(self, user_id: UUID) -> RuntimeEndpoint:
        async with self._lock(user_id):
            return await self._ensure_locked(user_id)

    async def status(self, user_id: UUID) -> RuntimeStatus:
        async with SessionLocal() as db:
            instance = await db.scalar(select(RuntimeInstance).where(RuntimeInstance.user_id == user_id))
            if instance is None:
                return RuntimeStatus("absent", self.settings.runtime_docker_image, None, None, None)
            await self._refresh_instance(db, instance)
            await db.commit()
            return self._status(instance)

    async def stop(self, user_id: UUID) -> RuntimeStatus:
        async with self._lock(user_id):
            await self._assert_not_busy(user_id)
            async with SessionLocal() as db:
                instance = await db.scalar(select(RuntimeInstance).where(RuntimeInstance.user_id == user_id))
                if instance is None or not instance.container_id:
                    return RuntimeStatus("absent", self.settings.runtime_docker_image, None, None, None)
                container = await self._container(instance.container_id)
                if container is not None:
                    await asyncio.to_thread(container.stop, timeout=self.settings.runtime_stop_timeout_seconds)
                instance.state, instance.host_port, instance.last_seen_at = "stopped", None, datetime.now(UTC)
                await db.commit()
                return self._status(instance)

    async def recreate(self, user_id: UUID) -> RuntimeEndpoint:
        async with self._lock(user_id):
            await self._assert_not_busy(user_id)
            async with SessionLocal() as db:
                instance = await db.scalar(select(RuntimeInstance).where(RuntimeInstance.user_id == user_id))
                if instance and instance.container_id:
                    container = await self._container(instance.container_id)
                    if container is not None:
                        await asyncio.to_thread(container.remove, force=True)
                    instance.container_id = None
                    instance.host_port = None
                    instance.state = "stopped"
                    await db.commit()
            return await self._ensure_locked(user_id)

    async def _ensure_locked(self, user_id: UUID) -> RuntimeEndpoint:
        async with SessionLocal() as db:
            instance = await db.scalar(select(RuntimeInstance).where(RuntimeInstance.user_id == user_id))
            if instance is None:
                instance = RuntimeInstance(user_id=user_id, container_name=self._container_name(user_id), image=self.settings.runtime_docker_image)
                db.add(instance)
                await db.flush()
            try:
                container = await self._container(instance.container_id) if instance.container_id else None
                if container is None:
                    container = await asyncio.to_thread(self._create_container, user_id)
                    instance.container_id = container.id
                    instance.container_name = container.name
                await asyncio.to_thread(container.reload)
                if container.status != "running":
                    await asyncio.to_thread(container.start)
                endpoint = await self._wait_ready(user_id, container)
                instance.state = "running"
                instance.host_port = int(endpoint.base_url.rsplit(":", 1)[1])
                instance.image = self.settings.runtime_docker_image
                instance.last_error = None
                instance.last_seen_at = datetime.now(UTC)
                await db.commit()
                return endpoint
            except (DockerException, OSError, RuntimeUnavailableError) as exc:
                instance.state = "error"
                instance.last_error = str(exc)
                instance.last_seen_at = datetime.now(UTC)
                await db.commit()
                raise RuntimeUnavailableError("docker_runtime_unavailable") from exc

    def _create_container(self, user_id: UUID):
        tenant_dir = self.settings.resolved_runtime_data_host_root / "tenants" / str(user_id)
        tenant_dir.mkdir(parents=True, exist_ok=True)
        container_path = f"/runtime-data/tenants/{user_id}"
        return self.client.containers.run(
            self.settings.runtime_docker_image,
            name=self._container_name(user_id),
            detach=True,
            network=self.settings.runtime_docker_network,
            ports={"3000/tcp": ("127.0.0.1", None)},
            environment={
                "TENANT_ID": str(user_id),
                "RUNTIME_SHARED_SECRET": self.settings.runtime_shared_secret,
                "PI_CODING_AGENT_DIR": f"{container_path}/agent",
                "HOME": f"{container_path}/home",
            },
            volumes={
                str(self.settings.resolved_runtime_source_dir): {"bind": "/opt/pi-runtime/src", "mode": "ro"},
                str(tenant_dir): {"bind": container_path, "mode": "rw"},
            },
            user=f"{os.getuid()}:{os.getgid()}",
            read_only=True,
            tmpfs={"/tmp": "rw,nosuid,nodev,size=256m"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit=self.settings.runtime_memory_limit,
            nano_cpus=self.settings.runtime_nano_cpus,
            pids_limit=self.settings.runtime_pids_limit,
            labels={"com.pi-saas.managed": "true", "com.pi-saas.tenant-id": str(user_id)},
        )

    async def _wait_ready(self, user_id: UUID, container) -> RuntimeEndpoint:
        deadline = asyncio.get_running_loop().time() + self.settings.runtime_start_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.to_thread(container.reload)
            endpoint = self._endpoint(container)
            if container.status == "running" and endpoint is not None and await self._health(endpoint.base_url, user_id):
                return endpoint
            await asyncio.sleep(0.2)
        raise RuntimeUnavailableError("docker_runtime_start_timeout")

    async def _health(self, base_url: str, user_id: UUID) -> bool:
        headers = {"Authorization": f"Bearer {self.settings.runtime_shared_secret}", "X-Tenant-ID": str(user_id)}
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                return (await client.get(f"{base_url}/internal/v1/health", headers=headers)).is_success
        except httpx.HTTPError:
            return False

    async def _refresh_instance(self, db, instance: RuntimeInstance) -> None:
        container = await self._container(instance.container_id) if instance.container_id else None
        if container is None:
            instance.state, instance.host_port = "absent", None
        else:
            await asyncio.to_thread(container.reload)
            endpoint = self._endpoint(container)
            instance.state = container.status
            instance.host_port = int(endpoint.base_url.rsplit(":", 1)[1]) if endpoint else None
        instance.last_seen_at = datetime.now(UTC)

    async def _container(self, container_id: str | None):
        if not container_id:
            return None
        try:
            return await asyncio.to_thread(self.client.containers.get, container_id)
        except NotFound:
            return None

    def _endpoint(self, container) -> RuntimeEndpoint | None:
        bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {}).get("3000/tcp") or []
        if not bindings:
            return None
        port = bindings[0].get("HostPort")
        return RuntimeEndpoint(f"http://127.0.0.1:{port}", "running") if port else None

    def _ensure_network(self) -> None:
        try:
            self.client.networks.get(self.settings.runtime_docker_network)
        except NotFound:
            self.client.networks.create(self.settings.runtime_docker_network, driver="bridge", options={"com.docker.network.bridge.enable_icc": "false"})

    async def _assert_not_busy(self, user_id: UUID) -> None:
        async with SessionLocal() as db:
            running = await db.scalar(select(AgentRun.id).where(AgentRun.user_id == user_id, AgentRun.status == "running").limit(1))
            if running is not None:
                raise RuntimeBusyError("runtime_busy")

    def _container_name(self, user_id: UUID) -> str:
        return f"pi-saas-runtime-{str(user_id).replace('-', '')}"

    def _lock(self, user_id: UUID) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    @staticmethod
    def _status(instance: RuntimeInstance) -> RuntimeStatus:
        return RuntimeStatus(instance.state, instance.image, instance.container_id, instance.last_error, instance.last_seen_at)


_locator: RuntimeLocator | None = None


def get_runtime_locator() -> RuntimeLocator:
    global _locator
    if _locator is None:
        _locator = DockerRuntimeLocator(get_settings())
    return _locator
