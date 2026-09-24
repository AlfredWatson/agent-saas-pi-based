from collections.abc import AsyncIterator
from uuid import UUID

import httpx

from ..core.config import get_settings
from ..services.runtime_locator import RuntimeUnavailableError, get_runtime_locator


class LocalModelServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class RuntimeClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.secret = settings.runtime_shared_secret

    def _headers(self, user_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}", "X-Tenant-ID": user_id}

    async def health(self) -> None:
        await get_runtime_locator().preflight()

    async def _base_url(self, user_id: str) -> str:
        try:
            return (await get_runtime_locator().ensure(UUID(user_id))).base_url
        except ValueError as exc:
            raise RuntimeUnavailableError("invalid_runtime_tenant") from exc

    async def providers(self, user_id: str) -> list[dict]:
        base_url = await self._base_url(user_id)
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(
                f"{base_url}/internal/v1/providers", headers=self._headers(user_id)
            )
            response.raise_for_status()
            return response.json()["providers"]

    async def accept_provider_binding(
        self, user_id: str, provider_id: str, api_key: str
    ) -> None:
        base_url = await self._base_url(user_id)
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(
                f"{base_url}/internal/v1/providers/validate",
                headers=self._headers(user_id),
                json={"provider_id": provider_id, "api_key": api_key},
            )
            response.raise_for_status()
            return None

    async def discover_local_models(
        self, user_id: str, base_url: str, api_key: str
    ) -> dict:
        runtime_url = await self._base_url(user_id)
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.post(
                    f"{runtime_url}/internal/v1/local-models/discover",
                    headers=self._headers(user_id),
                    json={"base_url": base_url, "api_key": api_key},
                )
        except httpx.RequestError as exc:
            raise LocalModelServiceError(503, "model_service_unavailable") from exc
        if not response.is_success:
            if response.status_code == 404:
                raise LocalModelServiceError(503, "runtime_update_required")
            try:
                error = response.json().get("error")
            except ValueError:
                error = None
            if error not in {
                "invalid_model_base_url", "model_service_unavailable",
                "model_service_auth_failed", "invalid_model_catalog",
            }:
                error = "model_service_unavailable"
            raise LocalModelServiceError(503 if error == "model_service_unavailable" else 422, error)
        return response.json()

    async def models(self, user_id: str, provider_id: str) -> list[dict]:
        base_url = await self._base_url(user_id)
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.get(
                f"{base_url}/internal/v1/models",
                headers=self._headers(user_id),
                params={"provider_id": provider_id},
            )
            response.raise_for_status()
            return response.json()["models"]

    async def ensure_session(
        self, user_id: str, session_id: str, payload: dict
    ) -> dict:
        base_url = await self._base_url(user_id)
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.put(
                f"{base_url}/internal/v1/sessions/{session_id}",
                headers=self._headers(user_id),
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def stream_chat(
        self, user_id: str, session_id: str, content: str
    ) -> AsyncIterator[dict]:
        base_url = await self._base_url(user_id)
        async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
            async with client.stream(
                "POST",
                f"{base_url}/internal/v1/sessions/{session_id}/chat",
                headers=self._headers(user_id),
                json={"content": content},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line:
                        yield __import__("json").loads(line)

    async def control(
        self, user_id: str, session_id: str, action: str, content: str | None = None
    ) -> None:
        base_url = await self._base_url(user_id)
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(
                f"{base_url}/internal/v1/sessions/{session_id}/{action}",
                headers=self._headers(user_id),
                json={"content": content} if content else {},
            )
            response.raise_for_status()

    async def delete_session(
        self, user_id: str, session_id: str, session_file_key: str
    ) -> None:
        """Remove one idle Runtime session trajectory.

        The public Gateway route owns the database transaction.  This method is
        deliberately idempotent at the Runtime boundary so a database failure
        after a successful file deletion can be retried safely.
        """
        base_url = await self._base_url(user_id)
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.request(
                    "DELETE",
                    f"{base_url}/internal/v1/sessions/{session_id}",
                    headers=self._headers(user_id),
                    json={"session_file_key": session_file_key},
                )
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError("runtime_session_delete_failed") from exc
        if response.status_code == 409:
            raise RuntimeSessionBusyError("session_busy")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError("runtime_session_delete_failed") from exc

    async def delete_workspace(
        self, user_id: str, workspace_key: str, sessions: list[dict]
    ) -> None:
        base_url = await self._base_url(user_id)
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.request(
                    "DELETE",
                    f"{base_url}/internal/v1/workspaces/{workspace_key}",
                    headers=self._headers(user_id),
                    json={"sessions": sessions},
                )
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError("runtime_workspace_delete_failed") from exc
        if response.status_code == 409:
            raise RuntimeWorkspaceBusyError("workspace_busy")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError("runtime_workspace_delete_failed") from exc

    async def list_workspace_files(
        self, user_id: str, workspace_key: str
    ) -> list[dict]:
        base_url = await self._base_url(user_id)
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.get(
                    f"{base_url}/internal/v1/workspaces/{workspace_key}/files",
                    headers=self._headers(user_id),
                )
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError("runtime_workspace_file_list_failed") from exc
        self._raise_file_operation_error(response)
        return response.json()["items"]

    async def download_workspace_file(
        self, user_id: str, workspace_key: str, path: str
    ) -> tuple[int | None, AsyncIterator[bytes]]:
        """Return an open Runtime stream whose iterator closes its HTTP client."""
        base_url = await self._base_url(user_id)
        client = httpx.AsyncClient(timeout=None, trust_env=False)
        try:
            response = await client.send(
                client.build_request(
                    "GET",
                    f"{base_url}/internal/v1/workspaces/{workspace_key}/files/content",
                    headers=self._headers(user_id),
                    params={"path": path},
                ),
                stream=True,
            )
        except httpx.HTTPError as exc:
            await client.aclose()
            raise RuntimeUnavailableError(
                "runtime_workspace_file_download_failed"
            ) from exc
        try:
            self._raise_file_operation_error(response)
        except Exception:
            await response.aclose()
            await client.aclose()
            raise

        try:
            size = int(response.headers["content-length"])
        except (KeyError, ValueError):
            size = None

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return size, chunks()

    async def upload_workspace_file(
        self,
        user_id: str,
        workspace_key: str,
        path: str,
        overwrite: bool,
        content: AsyncIterator[bytes],
    ) -> dict:
        base_url = await self._base_url(user_id)
        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                response = await client.put(
                    f"{base_url}/internal/v1/workspaces/{workspace_key}/files",
                    headers={
                        **self._headers(user_id),
                        "Content-Type": "application/octet-stream",
                    },
                    params={"path": path, "overwrite": str(overwrite).lower()},
                    content=content,
                )
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError(
                "runtime_workspace_file_upload_failed"
            ) from exc
        self._raise_file_operation_error(response)
        return response.json()

    async def delete_workspace_file(
        self, user_id: str, workspace_key: str, path: str
    ) -> None:
        base_url = await self._base_url(user_id)
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.delete(
                    f"{base_url}/internal/v1/workspaces/{workspace_key}/files",
                    headers=self._headers(user_id),
                    params={"path": path},
                )
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError(
                "runtime_workspace_file_delete_failed"
            ) from exc
        self._raise_file_operation_error(response)

    @staticmethod
    def _raise_file_operation_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code in {404, 409, 413, 422}:
            try:
                error = response.json().get("error")
            except ValueError:
                error = None
            if response.status_code == 413 and not isinstance(error, str):
                error = "file_too_large"
            if isinstance(error, str):
                raise RuntimeWorkspaceFileError(response.status_code, error)
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError(
                "runtime_workspace_file_operation_failed"
            ) from exc


class RuntimeWorkspaceBusyError(RuntimeError):
    pass


class RuntimeSessionBusyError(RuntimeError):
    pass


class RuntimeWorkspaceFileError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
