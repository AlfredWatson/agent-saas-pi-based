from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Chunk, ChunkVector


class PostgresVectorStore(VectorStore):
    """LangChain VectorStore facade over tenant-owned RAG tables.

    The RAG worker writes precomputed vectors atomically, so the generic sync
    mutation methods are intentionally unavailable. Retrieval uses this
    store's async scored search.
    """

    def __init__(
        self,
        db: AsyncSession,
        knowledge_base_id: UUID,
        embedding,
        document_ids: list[UUID] | None = None,
    ):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        self._embedding = embedding
        self.document_ids = document_ids

    @property
    def embeddings(self):
        return self._embedding

    @classmethod
    def from_texts(cls, texts, embedding, metadatas=None, *, ids=None, **kwargs):
        raise RuntimeError(
            "construct PostgresVectorStore with an AsyncSession and knowledge base"
        )

    def add_texts(self, texts: Iterable[str], metadatas=None, *, ids=None, **kwargs):
        raise RuntimeError("use the RAG vectorization worker")

    def similarity_search(self, query: str, k: int = 4, **kwargs) -> list[Document]:
        raise RuntimeError("use asimilarity_search_with_relevance_scores")

    async def asimilarity_search_with_relevance_scores(
        self, query: str, *, k: int = 4, score_threshold: float | None = None, **kwargs
    ) -> list[tuple[Document, float]]:
        query_vector = await self._embedding.aembed_query(query)
        score = (1 - ChunkVector.embedding.cosine_distance(query_vector)).label("score")
        statement = (
            select(Chunk, score)
            .join(ChunkVector, ChunkVector.chunk_id == Chunk.id)
            .where(ChunkVector.knowledge_base_id == self.knowledge_base_id)
            .order_by(score.desc())
            .limit(k)
        )
        if self.document_ids:
            statement = statement.where(Chunk.document_id.in_(self.document_ids))
        rows = (await self.db.execute(statement)).all()
        results: list[tuple[Document, float]] = []
        for chunk, value in rows:
            relevance = float(value)
            if score_threshold is not None and relevance < score_threshold:
                continue
            metadata = dict(chunk.metadata_)
            metadata.update(
                chunk_id=str(chunk.id),
                document_id=str(chunk.document_id),
                knowledge_base_id=str(chunk.knowledge_base_id),
            )
            results.append(
                (Document(page_content=chunk.text, metadata=metadata), relevance)
            )
        return results

    async def replace_document_vectors(
        self,
        document_id: UUID,
        model_fingerprint: str,
        items: list[tuple[Chunk, list[float]]],
    ) -> None:
        await self.db.execute(
            delete(ChunkVector).where(ChunkVector.document_id == document_id)
        )
        for chunk, vector in items:
            self.db.add(
                ChunkVector(
                    knowledge_base_id=self.knowledge_base_id,
                    document_id=document_id,
                    chunk_id=chunk.id,
                    model_fingerprint=model_fingerprint,
                    dimension=len(vector),
                    embedding=vector,
                )
            )
        await self.db.flush()
