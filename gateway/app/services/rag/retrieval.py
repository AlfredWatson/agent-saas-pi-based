from __future__ import annotations

import re
from uuid import UUID

import jieba
from langchain_core.documents import Document
from rank_bm25 import BM25Okapi
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.rag.models import (
    Chunk,
    GraphArtifact,
    GraphEdge,
    GraphEvidence,
    GraphNode,
    RagModelConfig,
)
from app.domain.rag.graph import reciprocal_rank_fusion
from app.domain.rag.schemas import RetrievalInput
from app.integrations.rag.model_clients import embedding_client, input_from_stored
from app.integrations.rag.vector_store import PostgresVectorStore


def tokenize(text: str) -> list[str]:
    return [
        token.casefold()
        for token in jieba.lcut(text)
        if re.search(r"\w", token, re.UNICODE)
    ]


async def _embedding_config(
    db: AsyncSession, knowledge_base_id: UUID
) -> RagModelConfig:
    config = await db.scalar(
        select(RagModelConfig).where(
            RagModelConfig.knowledge_base_id == knowledge_base_id,
            RagModelConfig.kind == "embedding",
        )
    )
    if config is None:
        raise ValueError("embedding_model_required")
    return config


def render_document(document: Document, score: float, source: str) -> dict:
    return {
        "chunk_id": document.metadata["chunk_id"],
        "document_id": document.metadata["document_id"],
        "text": document.page_content,
        "metadata": {
            key: value
            for key, value in document.metadata.items()
            if key not in {"chunk_id", "document_id", "knowledge_base_id"}
        },
        "score": score,
        "source": source,
    }


async def vector_retrieve(
    db: AsyncSession, knowledge_base_id: UUID, body: RetrievalInput
) -> list[dict]:
    config = await _embedding_config(db, knowledge_base_id)
    store = PostgresVectorStore(
        db,
        knowledge_base_id,
        embedding_client(input_from_stored(config)),
        body.document_ids,
    )
    rows = await store.asimilarity_search_with_relevance_scores(
        body.query,
        k=max(body.top_k, body.candidate_k),
        score_threshold=body.min_score,
    )
    return [
        render_document(document, score, "vector")
        for document, score in rows[: body.top_k]
    ]


async def hybrid_retrieve(
    db: AsyncSession, knowledge_base_id: UUID, body: RetrievalInput
) -> list[dict]:
    config = await _embedding_config(db, knowledge_base_id)
    store = PostgresVectorStore(
        db,
        knowledge_base_id,
        embedding_client(input_from_stored(config)),
        body.document_ids,
    )
    vector_rows = await store.asimilarity_search_with_relevance_scores(
        body.query, k=body.vector_k, score_threshold=body.min_score
    )
    statement = select(Chunk).where(Chunk.knowledge_base_id == knowledge_base_id)
    if body.document_ids:
        statement = statement.where(Chunk.document_id.in_(body.document_ids))
    chunks = list((await db.scalars(statement.order_by(Chunk.id))).all())
    query_tokens = tokenize(body.query)
    bm25_rows: list[tuple[Chunk, float]] = []
    tokenized_chunks = [
        (chunk, tokens) for chunk in chunks if (tokens := tokenize(chunk.text))
    ]
    if tokenized_chunks and query_tokens:
        bm25 = BM25Okapi([tokens for _, tokens in tokenized_chunks])
        scores = bm25.get_scores(query_tokens)
        bm25_rows = sorted(
            zip((chunk for chunk, _ in tokenized_chunks), scores, strict=True),
            key=lambda item: float(item[1]),
            reverse=True,
        )[: body.bm25_k]

    vector_ids = [document.metadata["chunk_id"] for document, _ in vector_rows]
    bm25_ids = [str(chunk.id) for chunk, _ in bm25_rows]
    fused = reciprocal_rank_fusion(
        [(vector_ids, body.vector_weight), (bm25_ids, body.bm25_weight)],
        rrf_k=body.rrf_k,
    )[: body.top_k]
    vector_map = {
        document.metadata["chunk_id"]: document for document, _ in vector_rows
    }
    chunk_map = {str(chunk.id): chunk for chunk in chunks}
    results: list[dict] = []
    for chunk_id, score in fused:
        if chunk_id in vector_map:
            document = vector_map[chunk_id]
        else:
            chunk = chunk_map[chunk_id]
            metadata = dict(chunk.metadata_)
            metadata.update(
                chunk_id=str(chunk.id),
                document_id=str(chunk.document_id),
                knowledge_base_id=str(knowledge_base_id),
            )
            document = Document(page_content=chunk.text, metadata=metadata)
        results.append(render_document(document, score, "hybrid"))
    return results


async def graph_retrieve(
    db: AsyncSession, knowledge_base_id: UUID, body: RetrievalInput
) -> dict:
    tokens = tokenize(body.query) or [body.query.casefold()]
    predicates = []
    for token in tokens[:10]:
        escaped = token.replace("%", "\\%").replace("_", "\\_")
        predicates.extend(
            (
                GraphNode.name.ilike(f"%{escaped}%"),
                GraphNode.description.ilike(f"%{escaped}%"),
            )
        )
    node_statement = (
        select(GraphNode)
        .join(GraphArtifact, GraphArtifact.id == GraphNode.artifact_id)
        .where(
            GraphArtifact.knowledge_base_id == knowledge_base_id,
            GraphArtifact.status == "ready",
            or_(*predicates),
        )
        .limit(body.top_entities)
    )
    nodes = list((await db.scalars(node_statement)).all())
    node_ids = {node.id for node in nodes}
    edges: list[GraphEdge] = []
    frontier = set(node_ids)
    for _ in range(body.max_hops):
        if not frontier:
            break
        edge_statement = (
            select(GraphEdge)
            .join(GraphArtifact, GraphArtifact.id == GraphEdge.artifact_id)
            .where(
                GraphArtifact.knowledge_base_id == knowledge_base_id,
                or_(
                    GraphEdge.source_node_id.in_(frontier),
                    GraphEdge.target_node_id.in_(frontier),
                ),
            )
            .limit(body.candidate_k)
        )
        layer = list((await db.scalars(edge_statement)).all())
        edges.extend(layer)
        discovered = {edge.source_node_id for edge in layer} | {
            edge.target_node_id for edge in layer
        }
        frontier = discovered - node_ids
        node_ids |= discovered
    if node_ids:
        nodes = list(
            (
                await db.scalars(select(GraphNode).where(GraphNode.id.in_(node_ids)))
            ).all()
        )

    evidence_statement = select(GraphEvidence).where(
        GraphEvidence.artifact_id.in_({node.artifact_id for node in nodes})
    )
    if body.document_ids:
        evidence_statement = evidence_statement.where(
            GraphEvidence.document_id.in_(body.document_ids)
        )
    evidence = (
        list((await db.scalars(evidence_statement.limit(body.top_k * 10))).all())
        if nodes
        else []
    )
    allowed_nodes = {item.node_id for item in evidence if item.node_id}
    allowed_edges = {item.edge_id for item in evidence if item.edge_id}
    if body.document_ids:
        nodes = [node for node in nodes if node.id in allowed_nodes]
        edges = [edge for edge in edges if edge.id in allowed_edges]
    return {
        "nodes": [
            {
                "id": str(node.id),
                "name": node.name,
                "entity_type": node.entity_type,
                "description": node.description,
                "properties": node.properties,
            }
            for node in nodes[: body.top_entities]
        ],
        "edges": [
            {
                "id": str(edge.id),
                "source_node_id": str(edge.source_node_id),
                "relation": edge.relation,
                "target_node_id": str(edge.target_node_id),
                "description": edge.description,
                "properties": edge.properties,
            }
            for edge in edges[: body.top_k]
        ],
        "evidence": [
            {
                "document_id": str(item.document_id),
                "chunk_id": str(item.chunk_id),
                "node_id": str(item.node_id) if item.node_id else None,
                "edge_id": str(item.edge_id) if item.edge_id else None,
            }
            for item in evidence[: body.top_k]
        ],
    }


async def retrieve(db: AsyncSession, knowledge_base_id: UUID, body: RetrievalInput):
    if body.mode == "vector":
        return {
            "mode": body.mode,
            "items": await vector_retrieve(db, knowledge_base_id, body),
        }
    if body.mode == "hybrid":
        return {
            "mode": body.mode,
            "items": await hybrid_retrieve(db, knowledge_base_id, body),
        }
    return {"mode": body.mode, **await graph_retrieve(db, knowledge_base_id, body)}
