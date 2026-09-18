# ruff: noqa: F403, F405
from .common import *  # noqa: F403


@router.post("/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/retrieve")
async def retrieve_from_knowledge_base(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: RetrievalInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id)
    if body.document_ids:
        count = await db.scalar(
            select(func.count())
            .select_from(RagDocument)
            .where(
                RagDocument.knowledge_base_id == kb.id,
                RagDocument.id.in_(set(body.document_ids)),
            )
        )
        if count != len(set(body.document_ids)):
            raise HTTPException(422, "document_filter_outside_knowledge_base")
    try:
        return await retrieve(db, kb.id, body)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/graphs:merge",
    status_code=201,
)
async def merge_graphs(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    body: GraphMergeInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    source_ids = set(body.graph_ids)
    sources = list(
        (
            await db.scalars(
                select(GraphArtifact).where(
                    GraphArtifact.knowledge_base_id == kb.id,
                    GraphArtifact.id.in_(source_ids),
                    GraphArtifact.status == "ready",
                )
            )
        ).all()
    )
    if len(sources) != len(source_ids):
        raise HTTPException(422, "graph_outside_knowledge_base")
    artifact = GraphArtifact(
        knowledge_base_id=kb.id,
        kind="merged",
        name=body.name,
        source_graph_ids=[str(value) for value in sorted(source_ids, key=str)],
    )
    db.add(artifact)
    await db.flush()
    node_map: dict[str, GraphNode] = {}
    source_node_to_key: dict[UUID, str] = {}
    for node in (
        await db.scalars(select(GraphNode).where(GraphNode.artifact_id.in_(source_ids)))
    ).all():
        key = canonical_key(node.entity_type, node.name)
        source_node_to_key[node.id] = key
        if key not in node_map:
            merged = GraphNode(
                artifact_id=artifact.id,
                canonical_key=key,
                name=node.name,
                entity_type=node.entity_type,
                description=node.description,
                properties=node.properties,
            )
            db.add(merged)
            node_map[key] = merged
        else:
            node_map[key].properties = merge_property_maps(
                node_map[key].properties, node.properties
            )
            descriptions = [
                value
                for value in (node_map[key].description, node.description)
                if value
            ]
            node_map[key].description = "\n".join(dict.fromkeys(descriptions))
    await db.flush()
    edge_map: dict[tuple[str, str, str], GraphEdge] = {}
    source_edge_to_merged: dict[UUID, GraphEdge] = {}
    for edge in (
        await db.scalars(select(GraphEdge).where(GraphEdge.artifact_id.in_(source_ids)))
    ).all():
        source_key = source_node_to_key[edge.source_node_id]
        target_key = source_node_to_key[edge.target_node_id]
        key = (source_key, edge.relation.casefold().strip(), target_key)
        if key not in edge_map:
            merged_edge = GraphEdge(
                artifact_id=artifact.id,
                source_node_id=node_map[source_key].id,
                target_node_id=node_map[target_key].id,
                relation=edge.relation,
                description=edge.description,
                properties=edge.properties,
            )
            db.add(merged_edge)
            edge_map[key] = merged_edge
        else:
            merged_edge = edge_map[key]
            merged_edge.properties = merge_property_maps(
                merged_edge.properties, edge.properties
            )
        source_edge_to_merged[edge.id] = edge_map[key]
    await db.flush()
    evidence_rows = (
        await db.scalars(
            select(GraphEvidence).where(GraphEvidence.artifact_id.in_(source_ids))
        )
    ).all()
    seen = set()
    for evidence in evidence_rows:
        node_id = (
            node_map[source_node_to_key[evidence.node_id]].id
            if evidence.node_id
            else None
        )
        edge_id = (
            source_edge_to_merged[evidence.edge_id].id if evidence.edge_id else None
        )
        key = (node_id, edge_id, evidence.document_id, evidence.chunk_id)
        if key in seen:
            continue
        seen.add(key)
        db.add(
            GraphEvidence(
                artifact_id=artifact.id,
                node_id=node_id,
                edge_id=edge_id,
                document_id=evidence.document_id,
                chunk_id=evidence.chunk_id,
                model_fingerprint=evidence.model_fingerprint,
            )
        )
    await db.commit()
    return {
        "id": str(artifact.id),
        "name": artifact.name,
        "kind": artifact.kind,
        "source_graph_ids": artifact.source_graph_ids,
    }


@router.get("/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/graphs")
async def list_graphs(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id)
    rows = (
        await db.scalars(
            select(GraphArtifact)
            .where(GraphArtifact.knowledge_base_id == kb.id)
            .order_by(GraphArtifact.created_at)
        )
    ).all()
    return {
        "items": [
            {
                "id": str(item.id),
                "name": item.name,
                "kind": item.kind,
                "source_document_id": str(item.source_document_id)
                if item.source_document_id
                else None,
                "source_graph_ids": item.source_graph_ids,
                "status": item.status,
            }
            for item in rows
        ]
    }


@router.get(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/graphs/{graph_id}"
)
async def get_graph(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    graph_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id)
    artifact = await db.scalar(
        select(GraphArtifact).where(
            GraphArtifact.id == graph_id, GraphArtifact.knowledge_base_id == kb.id
        )
    )
    if artifact is None:
        raise HTTPException(404, "graph_not_found")
    nodes = (
        await db.scalars(select(GraphNode).where(GraphNode.artifact_id == artifact.id))
    ).all()
    edges = (
        await db.scalars(select(GraphEdge).where(GraphEdge.artifact_id == artifact.id))
    ).all()
    return {
        "id": str(artifact.id),
        "name": artifact.name,
        "kind": artifact.kind,
        "nodes": [
            {
                "id": str(node.id),
                "name": node.name,
                "entity_type": node.entity_type,
                "description": node.description,
                "properties": node.properties,
            }
            for node in nodes
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
            for edge in edges
        ],
    }


@router.delete(
    "/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/graphs/{graph_id}",
    status_code=204,
)
async def delete_graph(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    graph_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await owned_kb(db, workspace_id, knowledge_base_id, user.id, lock=True)
    if kb.status != "active":
        raise HTTPException(409, "knowledge_base_unavailable")
    artifact = await db.scalar(
        select(GraphArtifact)
        .where(GraphArtifact.id == graph_id, GraphArtifact.knowledge_base_id == kb.id)
        .with_for_update()
    )
    if artifact is None:
        raise HTTPException(404, "graph_not_found")
    if artifact.source_document_id:
        document = await db.get(RagDocument, artifact.source_document_id)
        if document:
            document.graph_status = "not_started"
            document.graph_progress = 0
            document.graph_message = None
            document.graph_error = None
            document.graph_updated_at = func.now()
    merged = (
        await db.scalars(
            select(GraphArtifact).where(
                GraphArtifact.knowledge_base_id == kb.id,
                GraphArtifact.kind == "merged",
            )
        )
    ).all()
    dependent_ids = {str(artifact.id)}
    dependent = []
    changed = True
    while changed:
        changed = False
        for item in merged:
            if item in dependent:
                continue
            if dependent_ids.intersection(item.source_graph_ids):
                dependent.append(item)
                dependent_ids.add(str(item.id))
                changed = True
    for item in dependent:
        await db.delete(item)
    await db.delete(artifact)
    await db.commit()
