from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from .graph import canonical_key, merge_property_maps
from .models import (
    Chunk,
    GraphArtifact,
    GraphEdge,
    GraphEvidence,
    GraphNode,
    RagDocument,
    RagModelConfig,
)
from .schemas import ExtractedNode, GraphExtraction


class GraphStorageBackend(Protocol):
    async def replace_document_graph(
        self,
        document: RagDocument,
        model: RagModelConfig,
        extracted: list[tuple[Chunk, GraphExtraction]],
    ) -> GraphArtifact: ...


class PostgresGraphStore:
    def __init__(self, db: AsyncSession, knowledge_base_id: UUID):
        self.db = db
        self.knowledge_base_id = knowledge_base_id

    async def replace_document_graph(
        self,
        document: RagDocument,
        model: RagModelConfig,
        extracted: list[tuple[Chunk, GraphExtraction]],
    ) -> GraphArtifact:
        await self.db.execute(
            delete(GraphArtifact).where(
                GraphArtifact.knowledge_base_id == self.knowledge_base_id,
                GraphArtifact.source_document_id == document.id,
            )
        )
        artifact = GraphArtifact(
            knowledge_base_id=self.knowledge_base_id,
            source_document_id=document.id,
            kind="document",
            name=document.filename,
            model_fingerprint=model.fingerprint,
        )
        self.db.add(artifact)
        await self.db.flush()
        node_map: dict[str, GraphNode] = {}
        node_evidence: dict[str, set[UUID]] = {}
        for chunk, graph in extracted:
            endpoint_names = {edge.source for edge in graph.edges} | {
                edge.target for edge in graph.edges
            }
            nodes = list(graph.nodes)
            known_names = {node.name for node in nodes}
            nodes.extend(
                ExtractedNode(name=name, entity_type="unknown")
                for name in endpoint_names - known_names
            )
            for node in nodes:
                key = canonical_key(node.entity_type, node.name)
                node_evidence.setdefault(key, set()).add(chunk.id)
                if key not in node_map:
                    stored = GraphNode(
                        artifact_id=artifact.id,
                        canonical_key=key,
                        name=node.name,
                        entity_type=node.entity_type,
                        description=node.description,
                        properties=node.properties,
                    )
                    self.db.add(stored)
                    node_map[key] = stored
                else:
                    stored = node_map[key]
                    stored.properties = merge_property_maps(
                        stored.properties, node.properties
                    )
                    descriptions = [
                        value
                        for value in (stored.description, node.description)
                        if value
                    ]
                    stored.description = "\n".join(dict.fromkeys(descriptions))
        await self.db.flush()
        by_name = {}
        for key, node in node_map.items():
            by_name.setdefault(key.split(":", 1)[1], node)
        edge_map: dict[tuple[UUID, str, UUID], GraphEdge] = {}
        edge_evidence: dict[tuple[UUID, str, UUID], set[UUID]] = {}
        for chunk, graph in extracted:
            for edge in graph.edges:
                source = by_name.get(
                    canonical_key("unknown", edge.source).split(":", 1)[1]
                )
                target = by_name.get(
                    canonical_key("unknown", edge.target).split(":", 1)[1]
                )
                if source is None or target is None:
                    continue
                key = (source.id, edge.relation.casefold().strip(), target.id)
                edge_evidence.setdefault(key, set()).add(chunk.id)
                if key not in edge_map:
                    stored = GraphEdge(
                        artifact_id=artifact.id,
                        source_node_id=source.id,
                        target_node_id=target.id,
                        relation=edge.relation,
                        description=edge.description,
                        properties=edge.properties,
                    )
                    self.db.add(stored)
                    edge_map[key] = stored
                else:
                    edge_map[key].properties = merge_property_maps(
                        edge_map[key].properties, edge.properties
                    )
        await self.db.flush()
        for key, chunk_ids in node_evidence.items():
            for chunk_id in chunk_ids:
                self.db.add(
                    GraphEvidence(
                        artifact_id=artifact.id,
                        node_id=node_map[key].id,
                        document_id=document.id,
                        chunk_id=chunk_id,
                        model_fingerprint=model.fingerprint,
                    )
                )
        for key, chunk_ids in edge_evidence.items():
            for chunk_id in chunk_ids:
                self.db.add(
                    GraphEvidence(
                        artifact_id=artifact.id,
                        edge_id=edge_map[key].id,
                        document_id=document.id,
                        chunk_id=chunk_id,
                        model_fingerprint=model.fingerprint,
                    )
                )
        await self.db.flush()
        return artifact


def get_graph_store(
    backend: str, db: AsyncSession, knowledge_base_id: UUID
) -> GraphStorageBackend:
    if backend != "postgresql":
        raise ValueError(f"unsupported_graph_backend:{backend}")
    return PostgresGraphStore(db, knowledge_base_id)
