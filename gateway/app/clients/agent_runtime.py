from collections.abc import AsyncIterator

import httpx

from ..core.config import get_settings


class RuntimeClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.runtime_url.rstrip("/")
        self.secret = settings.runtime_shared_secret

    def _headers(self, user_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}", "X-Tenant-ID": user_id}

    async def health(self) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{self.base_url}/internal/v1/health", headers=self._headers("00000000-0000-0000-0000-000000000000"))
            response.raise_for_status()

    async def providers(self, user_id: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.base_url}/internal/v1/providers", headers=self._headers(user_id))
            response.raise_for_status()
            return response.json()["providers"]

    async def accept_provider_binding(self, user_id: str, provider_id: str, api_key: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self.base_url}/internal/v1/providers/validate", headers=self._headers(user_id), json={"provider_id": provider_id, "api_key": api_key})
            response.raise_for_status()
            return None

    async def models(self, user_id: str, provider_id: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{self.base_url}/internal/v1/models", headers=self._headers(user_id), params={"provider_id": provider_id})
            response.raise_for_status()
            return response.json()["models"]

    async def ensure_session(self, user_id: str, session_id: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.put(f"{self.base_url}/internal/v1/sessions/{session_id}", headers=self._headers(user_id), json=payload)
            response.raise_for_status()
            return response.json()

    async def stream_chat(self, user_id: str, session_id: str, content: str) -> AsyncIterator[dict]:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", f"{self.base_url}/internal/v1/sessions/{session_id}/chat", headers=self._headers(user_id), json={"content": content}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line:
                        yield __import__("json").loads(line)

    async def control(self, user_id: str, session_id: str, action: str, content: str | None = None) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self.base_url}/internal/v1/sessions/{session_id}/{action}", headers=self._headers(user_id), json={"content": content} if content else {})
            response.raise_for_status()
