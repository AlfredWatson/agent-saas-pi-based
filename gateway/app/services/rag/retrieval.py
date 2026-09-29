from __future__ import annotations

import logging
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
    KnowledgeBase,
    RagModelConfig,
)
from app.domain.rag.graph import reciprocal_rank_fusion
from app.domain.rag.schemas import RetrievalInput
from app.integrations.rag.model_clients import embedding_client, input_from_stored
from app.integrations.rag.reranker import Reranker, RerankerError, get_reranker
from app.integrations.rag.vector_store import get_vector_store


logger = logging.getLogger(__name__)


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


async def _reranker_config(
    db: AsyncSession, knowledge_base_id: UUID
) -> RagModelConfig | None:
    return await db.scalar(
        select(RagModelConfig).where(
            RagModelConfig.knowledge_base_id == knowledge_base_id,
            RagModelConfig.kind == "reranker",
        )
    )


def render_document(
    document: Document,
    score: float,
    source: str,
    *,
    retrieval_score: float | None = None,
) -> dict:
    result = {
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
    if retrieval_score is not None:
        result["retrieval_score"] = retrieval_score
    return result


def _rerank_status(configured: bool, applied: bool, error: str | None = None) -> dict:
    return {"configured": configured, "applied": applied, "error": error}


async def _render_ranked_rows(
    rows: list[tuple[Document, float]],
    *,
    query: str,
    top_k: int,
    source: str,
    reranker: Reranker | None,
    reranker_config: RagModelConfig | None,
    knowledge_base_id: UUID,
) -> tuple[list[dict], dict]:
    if reranker is None:
        return (
            [
                render_document(document, score, source)
                for document, score in rows[:top_k]
            ],
            _rerank_status(reranker_config is not None, False),
        )
    if not rows:
        return [], _rerank_status(True, False)
    try:
        ranked = await reranker.rerank(
            query,
            [document.page_content for document, _ in rows],
            top_n=min(top_k, len(rows)),
        )
    except RerankerError as exc:
        logger.warning(
            "RAG reranker degraded knowledge_base_id=%s fingerprint=%s error=%s",
            knowledge_base_id,
            reranker_config.fingerprint if reranker_config else "unknown",
            exc.code,
        )
        return (
            [
                render_document(document, score, source)
                for document, score in rows[:top_k]
            ],
            _rerank_status(True, False, exc.code),
        )
    except Exception:
        logger.warning(
            "RAG reranker degraded knowledge_base_id=%s fingerprint=%s error=%s",
            knowledge_base_id,
            reranker_config.fingerprint if reranker_config else "unknown",
            "reranker_unavailable",
        )
        return (
            [
                render_document(document, score, source)
                for document, score in rows[:top_k]
            ],
            _rerank_status(True, False, "reranker_unavailable"),
        )
    return (
        [
            render_document(
                rows[item.index][0],
                item.score,
                source,
                retrieval_score=rows[item.index][1],
            )
            for item in ranked
        ],
        _rerank_status(True, True),
    )


async def vector_retrieve(
    db: AsyncSession,
    knowledge_base_id: UUID,
    body: RetrievalInput,
    *,
    reranker: Reranker | None,
    reranker_config: RagModelConfig | None,
) -> tuple[list[dict], dict]:
    config = await _embedding_config(db, knowledge_base_id)
    kb = await db.get(KnowledgeBase, knowledge_base_id)
    if kb is None:
        raise ValueError("knowledge_base_not_found")
    store = get_vector_store(
        kb, db, embedding_client(input_from_stored(config)), body.document_ids
    )
    rows = await store.asimilarity_search_with_relevance_scores(
        body.query,
        k=max(body.top_k, body.candidate_k),
        score_threshold=body.min_score,
    )
    return await _render_ranked_rows(
        rows,
        query=body.query,
        top_k=body.top_k,
        source="vector",
        reranker=reranker,
        reranker_config=reranker_config,
        knowledge_base_id=knowledge_base_id,
    )


async def hybrid_retrieve(
    db: AsyncSession,
    knowledge_base_id: UUID,
    body: RetrievalInput,
    *,
    reranker: Reranker | None,
    reranker_config: RagModelConfig | None,
) -> tuple[list[dict], dict]:
    config = await _embedding_config(db, knowledge_base_id)
    kb = await db.get(KnowledgeBase, knowledge_base_id)
    if kb is None:
        raise ValueError("knowledge_base_not_found")
    store = get_vector_store(
        kb, db, embedding_client(input_from_stored(config)), body.document_ids
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
    candidate_limit = max(body.top_k, body.candidate_k) if reranker else body.top_k
    fused = reciprocal_rank_fusion(
        [(vector_ids, body.vector_weight), (bm25_ids, body.bm25_weight)],
        rrf_k=body.rrf_k,
    )[:candidate_limit]
    vector_map = {
        document.metadata["chunk_id"]: document for document, _ in vector_rows
    }
    chunk_map = {str(chunk.id): chunk for chunk in chunks}
    rows: list[tuple[Document, float]] = []
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
        rows.append((document, score))
    return await _render_ranked_rows(
        rows,
        query=body.query,
        top_k=body.top_k,
        source="hybrid",
        reranker=reranker,
        reranker_config=reranker_config,
        knowledge_base_id=knowledge_base_id,
    )


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
    # Graph retrieval intentionally bypasses all reranker configuration and I/O.
    if body.mode == "graph":
        return {"mode": body.mode, **await graph_retrieve(db, knowledge_base_id, body)}

    reranker_config = await _reranker_config(db, knowledge_base_id)
    reranker = (
        get_reranker(input_from_stored(reranker_config))
        if body.rerank and reranker_config is not None
        else None
    )
    if body.mode == "vector":
        items, status = await vector_retrieve(
            db,
            knowledge_base_id,
            body,
            reranker=reranker,
            reranker_config=reranker_config,
        )
    else:
        items, status = await hybrid_retrieve(
            db,
            knowledge_base_id,
            body,
            reranker=reranker,
            reranker_config=reranker_config,
        )
    return {"mode": body.mode, "items": items, "rerank": status}
