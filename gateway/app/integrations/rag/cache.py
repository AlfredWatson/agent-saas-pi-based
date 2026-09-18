from __future__ import annotations

import json
from uuid import UUID

from redis.asyncio import Redis

from app.core.config import get_settings


class RagCache:
    def __init__(self, client: Redis | None = None):
        settings = get_settings()
        self.client = client or Redis.from_url(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
            decode_responses=True,
        )
        self.ttl = settings.rag_cache_ttl_seconds

    @staticmethod
    def index_key(knowledge_base_id: UUID, document_id: UUID) -> str:
        return f"rag:{knowledge_base_id}:{document_id}:keys"

    @staticmethod
    def item_key(
        knowledge_base_id: UUID,
        document_id: UUID,
        kind: str,
        fingerprint: str,
        generation: int,
        content_hash: str,
    ) -> str:
        return f"rag:{knowledge_base_id}:{document_id}:{kind}:{fingerprint}:{generation}:{content_hash}"

    async def get(self, key: str):
        value = await self.client.get(key)
        if value is None:
            return None
        parts = key.split(":", 4)
        transaction = self.client.pipeline()
        transaction.expire(key, self.ttl)
        if len(parts) == 5 and parts[0] == "rag":
            index = self.index_key(UUID(parts[1]), UUID(parts[2]))
            transaction.sadd(index, key)
            transaction.expire(index, self.ttl)
        await transaction.execute()
        return json.loads(value)

    async def put(
        self, knowledge_base_id: UUID, document_id: UUID, key: str, value
    ) -> None:
        index = self.index_key(knowledge_base_id, document_id)
        transaction = self.client.pipeline()
        transaction.set(key, json.dumps(value, ensure_ascii=False), ex=self.ttl)
        transaction.sadd(index, key)
        transaction.expire(index, self.ttl)
        await transaction.execute()

    async def delete_document(self, knowledge_base_id: UUID, document_id: UUID) -> None:
        index = self.index_key(knowledge_base_id, document_id)
        keys = await self.client.smembers(index)
        if keys:
            await self.client.delete(*keys)
        await self.client.delete(index)

    async def delete_kind(
        self, knowledge_base_id: UUID, document_id: UUID, kind: str
    ) -> None:
        index = self.index_key(knowledge_base_id, document_id)
        marker = f"rag:{knowledge_base_id}:{document_id}:{kind}:"
        keys = {
            key for key in await self.client.smembers(index) if key.startswith(marker)
        }
        if not keys:
            return
        transaction = self.client.pipeline()
        transaction.delete(*keys)
        transaction.srem(index, *keys)
        transaction.expire(index, self.ttl)
        await transaction.execute()

    async def copy_document(
        self,
        source_kb: UUID,
        source_document: UUID,
        target_kb: UUID,
        target_document: UUID,
    ) -> None:
        source_index = self.index_key(source_kb, source_document)
        for source_key in await self.client.smembers(source_index):
            value = await self.client.get(source_key)
            ttl = await self.client.ttl(source_key)
            if value is None or ttl <= 0:
                continue
            target_key = source_key.replace(
                f"rag:{source_kb}:{source_document}:",
                f"rag:{target_kb}:{target_document}:",
                1,
            )
            target_index = self.index_key(target_kb, target_document)
            transaction = self.client.pipeline()
            transaction.set(target_key, value, ex=ttl)
            transaction.sadd(target_index, target_key)
            transaction.expire(target_index, max(ttl, 60))
            await transaction.execute()

    async def ping(self) -> bool:
        return bool(await self.client.ping())

    async def close(self) -> None:
        await self.client.aclose()
