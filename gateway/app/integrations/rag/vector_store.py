from __future__ import annotations

import ipaddress
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse
from uuid import UUID

import chromadb
from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from pymilvus import AsyncMilvusClient, DataType
from qdrant_client import AsyncQdrantClient, models as qmodels
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.rag.models import Chunk, ChunkVector, KnowledgeBase, RagDocument

logger = logging.getLogger("uvicorn.error")
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class VectorHit:
    chunk_id: UUID
    score: float


class RagVectorStore(Protocol):
    async def asimilarity_search_with_relevance_scores(self, query: str, *, k: int = 4, score_threshold: float | None = None) -> list[tuple[Document, float]]: ...
    async def replace_document_vectors(self, document_id: UUID, model_fingerprint: str, items: list[tuple[Chunk, list[float]]]) -> None: ...
    async def delete_document_vectors(self, document_id: UUID) -> None: ...
    async def has_vectors(self) -> bool: ...
    async def copy_to(self, target: KnowledgeBase, chunk_map: dict[UUID, Chunk]) -> None: ...
    async def delete_knowledge_base(self) -> None: ...


def collection_name(settings: Settings, knowledge_base_id: UUID) -> str:
    return f"{settings.rag_vector_collection_prefix}_{knowledge_base_id.hex}"


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _bypass_proxy_for_local_endpoint(host: str) -> None:
    """Keep local vector-store traffic out of inherited HTTP/gRPC proxies."""
    if not _is_loopback_host(host):
        return
    entries = [item.strip() for item in os.environ.get("NO_PROXY", "").split(",") if item]
    if host not in entries:
        entries.append(host)
        os.environ["NO_PROXY"] = ",".join(entries)
        os.environ["no_proxy"] = os.environ["NO_PROXY"]


def _display_endpoint(value: str) -> str:
    parsed = urlparse(value)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else value


def _document_expression(document_ids: list[UUID] | None) -> str:
    if not document_ids:
        return ""
    return "document_id in [" + ", ".join(f'"{value}"' for value in document_ids) + "]"


def _qdrant_filter(document_ids: list[UUID] | None) -> qmodels.Filter | None:
    if not document_ids:
        return None
    return qmodels.Filter(must=[qmodels.FieldCondition(key="document_id", match=qmodels.MatchAny(any=[str(value) for value in document_ids]))])


async def _hydrate_hits(db: AsyncSession, knowledge_base_id: UUID, hits: list[VectorHit], document_ids: list[UUID] | None) -> list[tuple[Document, float]]:
    if not hits:
        return []
    statement = (
        select(Chunk, RagDocument)
        .join(RagDocument, RagDocument.id == Chunk.document_id)
        .where(
            Chunk.knowledge_base_id == knowledge_base_id,
            Chunk.id.in_([hit.chunk_id for hit in hits]),
            RagDocument.vectorization_status == "succeeded",
            Chunk.generation == RagDocument.processing_generation,
        )
    )
    if document_ids:
        statement = statement.where(Chunk.document_id.in_(document_ids))
    chunks = {chunk.id: chunk for chunk, _ in (await db.execute(statement)).all()}
    result: list[tuple[Document, float]] = []
    for hit in hits:
        chunk = chunks.get(hit.chunk_id)
        if chunk is None:
            continue
        metadata = dict(chunk.metadata_)
        metadata.update(chunk_id=str(chunk.id), document_id=str(chunk.document_id), knowledge_base_id=str(chunk.knowledge_base_id))
        result.append((Document(page_content=chunk.text, metadata=metadata), hit.score))
    return result


class PostgresVectorStore(VectorStore):
    """LangChain facade over tenant-owned PostgreSQL vector tables."""

    def __init__(self, db: AsyncSession, knowledge_base_id: UUID, embedding, document_ids: list[UUID] | None = None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        self._embedding = embedding
        self.document_ids = document_ids

    @property
    def embeddings(self):
        return self._embedding

    @classmethod
    def from_texts(cls, texts, embedding, metadatas=None, *, ids=None, **kwargs):
        raise RuntimeError("construct PostgresVectorStore with an AsyncSession and knowledge base")

    def add_texts(self, texts: Iterable[str], metadatas=None, *, ids=None, **kwargs):
        raise RuntimeError("use the RAG vectorization worker")

    def similarity_search(self, query: str, k: int = 4, **kwargs) -> list[Document]:
        raise RuntimeError("use asimilarity_search_with_relevance_scores")

    async def asimilarity_search_with_relevance_scores(self, query: str, *, k: int = 4, score_threshold: float | None = None, **kwargs) -> list[tuple[Document, float]]:
        query_vector = await self._embedding.aembed_query(query)
        score = (1 - ChunkVector.embedding.cosine_distance(query_vector)).label("score")
        statement = (
            select(Chunk, score)
            .join(ChunkVector, ChunkVector.chunk_id == Chunk.id)
            .join(RagDocument, RagDocument.id == Chunk.document_id)
            .where(ChunkVector.knowledge_base_id == self.knowledge_base_id, RagDocument.vectorization_status == "succeeded", Chunk.generation == RagDocument.processing_generation)
            .order_by(score.desc()).limit(k)
        )
        if self.document_ids:
            statement = statement.where(Chunk.document_id.in_(self.document_ids))
        result: list[tuple[Document, float]] = []
        for chunk, value in (await self.db.execute(statement)).all():
            relevance = max(-1.0, min(1.0, float(value)))
            if score_threshold is not None and relevance < score_threshold:
                continue
            metadata = dict(chunk.metadata_)
            metadata.update(chunk_id=str(chunk.id), document_id=str(chunk.document_id), knowledge_base_id=str(chunk.knowledge_base_id))
            result.append((Document(page_content=chunk.text, metadata=metadata), relevance))
        return result

    async def replace_document_vectors(self, document_id: UUID, model_fingerprint: str, items: list[tuple[Chunk, list[float]]]) -> None:
        await self.delete_document_vectors(document_id)
        for chunk, vector in items:
            self.db.add(ChunkVector(knowledge_base_id=self.knowledge_base_id, document_id=document_id, chunk_id=chunk.id, model_fingerprint=model_fingerprint, dimension=len(vector), embedding=vector))
        await self.db.flush()

    async def delete_document_vectors(self, document_id: UUID) -> None:
        await self.db.execute(delete(ChunkVector).where(ChunkVector.document_id == document_id))

    async def has_vectors(self) -> bool:
        return (await self.db.scalar(select(ChunkVector.id).where(ChunkVector.knowledge_base_id == self.knowledge_base_id).limit(1))) is not None

    async def copy_to(self, target: KnowledgeBase, chunk_map: dict[UUID, Chunk]) -> None:
        vectors = (await self.db.scalars(select(ChunkVector).where(ChunkVector.knowledge_base_id == self.knowledge_base_id))).all()
        for vector in vectors:
            chunk = chunk_map[vector.chunk_id]
            self.db.add(ChunkVector(knowledge_base_id=target.id, document_id=chunk.document_id, chunk_id=chunk.id, model_fingerprint=vector.model_fingerprint, dimension=vector.dimension, embedding=vector.embedding))
        await self.db.flush()

    async def delete_knowledge_base(self) -> None:
        return None


class _ExternalVectorStore:
    backend: str

    def __init__(self, settings: Settings, db: AsyncSession, knowledge_base: KnowledgeBase, embedding, document_ids: list[UUID] | None = None):
        self.settings = settings
        self.db = db
        self.knowledge_base = knowledge_base
        self._embedding = embedding
        self.document_ids = document_ids

    @property
    def name(self) -> str:
        return collection_name(self.settings, self.knowledge_base.id)

    async def asimilarity_search_with_relevance_scores(self, query: str, *, k: int = 4, score_threshold: float | None = None) -> list[tuple[Document, float]]:
        if self._embedding is None:
            raise RuntimeError("embedding_model_required")
        hits = await self._search(await self._embedding.aembed_query(query), max(k * 4, k), score_threshold)
        return await _hydrate_hits(self.db, self.knowledge_base.id, hits, self.document_ids)

    async def _search(self, vector: list[float], limit: int, score_threshold: float | None) -> list[VectorHit]:
        raise NotImplementedError


class MilvusVectorStore(_ExternalVectorStore):
    backend = "milvus"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client: AsyncMilvusClient | None = None

    async def _get_client(self) -> AsyncMilvusClient:
        if self._client is None:
            host = urlparse(self.settings.milvus_uri).hostname
            if host:
                _bypass_proxy_for_local_endpoint(host)
            kwargs: dict[str, object] = {"uri": self.settings.milvus_uri, "db_name": self.settings.milvus_database, "timeout": self.settings.rag_vector_store_timeout_seconds}
            if self.settings.milvus_token:
                kwargs["token"] = self.settings.milvus_token
            self._client = AsyncMilvusClient(**kwargs)
        return self._client

    async def _ensure_collection(self, dimension: int) -> None:
        client = await self._get_client()
        if await client.has_collection(self.name):
            description = await client.describe_collection(self.name)
            for field in description.get("fields", []):
                if field.get("name") == "embedding":
                    existing = int(field.get("params", {}).get("dim", 0))
                    if not existing or existing == dimension:
                        return
                    if await self.has_vectors():
                        raise RuntimeError("vector_collection_schema_mismatch")
                    await client.drop_collection(self.name)
                    break
            else:
                raise RuntimeError("vector_collection_schema_mismatch")
        schema = AsyncMilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dimension)
        schema.add_field("document_id", DataType.VARCHAR, max_length=64)
        schema.add_field("generation", DataType.INT64)
        schema.add_field("model_fingerprint", DataType.VARCHAR, max_length=64)
        indexes = AsyncMilvusClient.prepare_index_params()
        indexes.add_index("embedding", index_type="AUTOINDEX", metric_type="COSINE")
        await client.create_collection(self.name, schema=schema, index_params=indexes)

    async def replace_document_vectors(self, document_id, model_fingerprint, items):
        if not items:
            return
        await self._ensure_collection(len(items[0][1]))
        await self.delete_document_vectors(document_id)
        client = await self._get_client()
        for start in range(0, len(items), self.settings.rag_vector_store_batch_size):
            batch = items[start:start + self.settings.rag_vector_store_batch_size]
            await client.upsert(self.name, [{"chunk_id": str(chunk.id), "embedding": vector, "document_id": str(document_id), "generation": chunk.generation, "model_fingerprint": model_fingerprint} for chunk, vector in batch])
        await client.flush(self.name)

    async def _search(self, vector, limit, score_threshold):
        client = await self._get_client()
        if not await client.has_collection(self.name):
            return []
        rows = await client.search(self.name, data=[vector], filter=_document_expression(self.document_ids), limit=limit, output_fields=["chunk_id"], consistency_level="Strong")
        hits: list[VectorHit] = []
        for row in rows[0] if rows else []:
            value = row.get("entity", {}).get("chunk_id", row.get("id"))
            score = max(-1.0, min(1.0, float(row["distance"])))
            if value and (score_threshold is None or score >= score_threshold):
                hits.append(VectorHit(UUID(str(value)), score))
        return hits

    async def delete_document_vectors(self, document_id):
        client = await self._get_client()
        if await client.has_collection(self.name):
            await client.delete(self.name, filter=f'document_id == "{document_id}"')
            await client.flush(self.name)

    async def has_vectors(self):
        client = await self._get_client()
        return bool(await client.has_collection(self.name) and await client.query(self.name, filter="", output_fields=["chunk_id"], limit=1, consistency_level="Strong"))

    async def copy_to(self, target, chunk_map):
        client = await self._get_client()
        if not await client.has_collection(self.name):
            return
        rows = await client.query(self.name, filter="", output_fields=["chunk_id", "embedding", "model_fingerprint"], limit=16_384)
        target_store = MilvusVectorStore(self.settings, self.db, target, None)
        grouped: dict[UUID, list[tuple[Chunk, list[float]]]] = {}
        fingerprints: dict[UUID, str] = {}
        for row in rows:
            chunk = chunk_map.get(UUID(str(row["chunk_id"])))
            if chunk:
                grouped.setdefault(chunk.document_id, []).append((chunk, list(row["embedding"])))
                fingerprints[chunk.document_id] = row["model_fingerprint"]
        for document_id, items in grouped.items():
            await target_store.replace_document_vectors(document_id, fingerprints[document_id], items)

    async def delete_knowledge_base(self):
        client = await self._get_client()
        if await client.has_collection(self.name):
            await client.drop_collection(self.name)


class ChromaVectorStore(_ExternalVectorStore):
    backend = "chroma"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None

    async def _get_client(self):
        if self._client is None:
            _bypass_proxy_for_local_endpoint(self.settings.chroma_host)
            self._client = await chromadb.AsyncHttpClient(host=self.settings.chroma_host, port=self.settings.chroma_port, ssl=self.settings.chroma_ssl, tenant=self.settings.chroma_tenant, database=self.settings.chroma_database)
        return self._client

    async def _collection(self, dimension: int | None = None):
        client = await self._get_client()
        try:
            collection = await client.get_collection(self.name)
        except Exception:
            if dimension is None:
                return None
            collection = await client.get_or_create_collection(self.name, metadata={"hnsw:space": "cosine", "rag_dimension": dimension, "rag_schema_version": _SCHEMA_VERSION})
        if dimension is not None and collection.metadata and collection.metadata.get("rag_dimension") not in {None, dimension}:
            if await collection.count():
                raise RuntimeError("vector_collection_schema_mismatch")
            await client.delete_collection(self.name)
            collection = await client.get_or_create_collection(self.name, metadata={"hnsw:space": "cosine", "rag_dimension": dimension, "rag_schema_version": _SCHEMA_VERSION})
        return collection

    async def replace_document_vectors(self, document_id, model_fingerprint, items):
        if not items:
            return
        collection = await self._collection(len(items[0][1]))
        await self.delete_document_vectors(document_id)
        for start in range(0, len(items), self.settings.rag_vector_store_batch_size):
            batch = items[start:start + self.settings.rag_vector_store_batch_size]
            await collection.upsert(ids=[str(chunk.id) for chunk, _ in batch], embeddings=[vector for _, vector in batch], metadatas=[{"document_id": str(document_id), "generation": chunk.generation, "model_fingerprint": model_fingerprint} for chunk, _ in batch])

    async def _search(self, vector, limit, score_threshold):
        collection = await self._collection()
        if collection is None:
            return []
        where = {"document_id": {"$in": [str(value) for value in self.document_ids]}} if self.document_ids else None
        result = await collection.query(query_embeddings=[vector], n_results=limit, where=where, include=["distances"])
        hits: list[VectorHit] = []
        for value, distance in zip(result.get("ids", [[]])[0], result.get("distances", [[]])[0], strict=True):
            score = max(-1.0, min(1.0, 1.0 - float(distance)))
            if score_threshold is None or score >= score_threshold:
                hits.append(VectorHit(UUID(str(value)), score))
        return hits

    async def delete_document_vectors(self, document_id):
        collection = await self._collection()
        if collection is not None:
            await collection.delete(where={"document_id": str(document_id)})

    async def has_vectors(self):
        collection = await self._collection()
        return bool(collection and await collection.count())

    async def copy_to(self, target, chunk_map):
        collection = await self._collection()
        if collection is None:
            return
        target_store = ChromaVectorStore(self.settings, self.db, target, None)
        offset = 0
        while True:
            page = await collection.get(limit=self.settings.rag_vector_store_batch_size, offset=offset, include=["embeddings", "metadatas"])
            ids = page.get("ids", [])
            if not ids:
                return
            grouped: dict[UUID, list[tuple[Chunk, list[float]]]] = {}
            fingerprints: dict[UUID, str] = {}
            for source_id, vector, metadata in zip(ids, page["embeddings"], page["metadatas"], strict=True):
                chunk = chunk_map.get(UUID(str(source_id)))
                if chunk:
                    grouped.setdefault(chunk.document_id, []).append((chunk, list(vector)))
                    fingerprints[chunk.document_id] = str(metadata["model_fingerprint"])
            for document_id, items in grouped.items():
                await target_store.replace_document_vectors(document_id, fingerprints[document_id], items)
            offset += len(ids)

    async def delete_knowledge_base(self):
        try:
            await (await self._get_client()).delete_collection(self.name)
        except Exception:
            return None


class QdrantVectorStore(_ExternalVectorStore):
    backend = "qdrant"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client: AsyncQdrantClient | None = None

    async def _get_client(self) -> AsyncQdrantClient:
        if self._client is None:
            host = urlparse(self.settings.qdrant_url).hostname
            if host:
                _bypass_proxy_for_local_endpoint(host)
            self._client = AsyncQdrantClient(url=self.settings.qdrant_url, api_key=self.settings.qdrant_api_key or None, grpc_port=self.settings.qdrant_grpc_port, prefer_grpc=self.settings.qdrant_prefer_grpc, timeout=self.settings.rag_vector_store_timeout_seconds)
        return self._client

    async def _ensure_collection(self, dimension: int) -> None:
        client = await self._get_client()
        if await client.collection_exists(self.name):
            vectors = (await client.get_collection(self.name)).config.params.vectors
            existing = vectors.size if isinstance(vectors, qmodels.VectorParams) else None
            if not existing or existing == dimension:
                return
            if await self.has_vectors():
                raise RuntimeError("vector_collection_schema_mismatch")
            await client.delete_collection(self.name)
        await client.create_collection(self.name, vectors_config=qmodels.VectorParams(size=dimension, distance=qmodels.Distance.COSINE), metadata={"rag_schema_version": str(_SCHEMA_VERSION)})

    async def replace_document_vectors(self, document_id, model_fingerprint, items):
        if not items:
            return
        await self._ensure_collection(len(items[0][1]))
        await self.delete_document_vectors(document_id)
        client = await self._get_client()
        for start in range(0, len(items), self.settings.rag_vector_store_batch_size):
            batch = items[start:start + self.settings.rag_vector_store_batch_size]
            await client.upsert(self.name, points=[qmodels.PointStruct(id=str(chunk.id), vector=vector, payload={"document_id": str(document_id), "generation": chunk.generation, "model_fingerprint": model_fingerprint}) for chunk, vector in batch], wait=True)

    async def _search(self, vector, limit, score_threshold):
        client = await self._get_client()
        if not await client.collection_exists(self.name):
            return []
        points = (await client.query_points(self.name, query=vector, query_filter=_qdrant_filter(self.document_ids), limit=limit, with_payload=False, score_threshold=score_threshold)).points
        return [VectorHit(UUID(str(point.id)), max(-1.0, min(1.0, float(point.score)))) for point in points]

    async def delete_document_vectors(self, document_id):
        client = await self._get_client()
        if await client.collection_exists(self.name):
            await client.delete(self.name, points_selector=_qdrant_filter([document_id]), wait=True)

    async def has_vectors(self):
        client = await self._get_client()
        if not await client.collection_exists(self.name):
            return False
        points, _ = await client.scroll(self.name, limit=1, with_payload=False)
        return bool(points)

    async def copy_to(self, target, chunk_map):
        client = await self._get_client()
        if not await client.collection_exists(self.name):
            return
        target_store = QdrantVectorStore(self.settings, self.db, target, None)
        offset = None
        while True:
            records, offset = await client.scroll(self.name, offset=offset, limit=self.settings.rag_vector_store_batch_size, with_payload=True, with_vectors=True)
            grouped: dict[UUID, list[tuple[Chunk, list[float]]]] = {}
            fingerprints: dict[UUID, str] = {}
            for record in records:
                chunk = chunk_map.get(UUID(str(record.id)))
                if chunk:
                    grouped.setdefault(chunk.document_id, []).append((chunk, list(record.vector)))
                    fingerprints[chunk.document_id] = str(record.payload["model_fingerprint"])
            for document_id, items in grouped.items():
                await target_store.replace_document_vectors(document_id, fingerprints[document_id], items)
            if offset is None:
                return

    async def delete_knowledge_base(self):
        client = await self._get_client()
        if await client.collection_exists(self.name):
            await client.delete_collection(self.name)


def get_vector_store(knowledge_base: KnowledgeBase, db: AsyncSession, embedding=None, document_ids: list[UUID] | None = None, settings: Settings | None = None) -> RagVectorStore:
    settings = settings or get_settings()
    if knowledge_base.vector_backend == "postgresql":
        return PostgresVectorStore(db, knowledge_base.id, embedding, document_ids)
    if knowledge_base.vector_backend == "milvus":
        return MilvusVectorStore(settings, db, knowledge_base, embedding, document_ids)
    if knowledge_base.vector_backend == "chroma":
        return ChromaVectorStore(settings, db, knowledge_base, embedding, document_ids)
    if knowledge_base.vector_backend == "qdrant":
        return QdrantVectorStore(settings, db, knowledge_base, embedding, document_ids)
    raise RuntimeError("unsupported_vector_backend")


async def verify_vector_backends(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    logger.info("RAG vector backends enabled: %s", ", ".join(settings.enabled_vector_backends))
    if "milvus" in settings.enabled_vector_backends:
        try:
            host = urlparse(settings.milvus_uri).hostname
            if host:
                _bypass_proxy_for_local_endpoint(host)
            kwargs: dict[str, object] = {"uri": settings.milvus_uri, "db_name": settings.milvus_database, "timeout": settings.rag_vector_store_timeout_seconds}
            if settings.milvus_token:
                kwargs["token"] = settings.milvus_token
            client = AsyncMilvusClient(**kwargs)
            await client.list_collections()
            await client.close()
            logger.info("RAG vector backend ready: backend=milvus endpoint=%s database=%s", _display_endpoint(settings.milvus_uri), settings.milvus_database)
        except Exception as exc:
            raise RuntimeError("rag_vector_backend_unavailable:milvus") from exc
    if "chroma" in settings.enabled_vector_backends:
        try:
            _bypass_proxy_for_local_endpoint(settings.chroma_host)
            client = await chromadb.AsyncHttpClient(host=settings.chroma_host, port=settings.chroma_port, ssl=settings.chroma_ssl, tenant=settings.chroma_tenant, database=settings.chroma_database)
            await client.heartbeat()
            logger.info("RAG vector backend ready: backend=chroma endpoint=%s://%s:%d tenant=%s database=%s", "https" if settings.chroma_ssl else "http", settings.chroma_host, settings.chroma_port, settings.chroma_tenant, settings.chroma_database)
        except Exception as exc:
            raise RuntimeError("rag_vector_backend_unavailable:chroma") from exc
    if "qdrant" in settings.enabled_vector_backends:
        try:
            host = urlparse(settings.qdrant_url).hostname
            if host:
                _bypass_proxy_for_local_endpoint(host)
            client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None, grpc_port=settings.qdrant_grpc_port, prefer_grpc=settings.qdrant_prefer_grpc, timeout=settings.rag_vector_store_timeout_seconds)
            await client.get_collections()
            await client.close()
            logger.info("RAG vector backend ready: backend=qdrant endpoint=%s", _display_endpoint(settings.qdrant_url))
        except Exception as exc:
            raise RuntimeError("rag_vector_backend_unavailable:qdrant") from exc
