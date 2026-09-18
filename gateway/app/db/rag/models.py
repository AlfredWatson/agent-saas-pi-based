import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base

RAG_SCHEMA = "rag"


class RagTimestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KnowledgeBase(RagTimestamped, Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "name", name="uq_knowledge_bases_workspace_name"
        ),
        {"schema": RAG_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform.users.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform.workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    file_backend: Mapped[str] = mapped_column(String(32), default="postgresql")
    block_backend: Mapped[str] = mapped_column(String(32), default="postgresql")
    chunk_backend: Mapped[str] = mapped_column(String(32), default="postgresql")
    vector_backend: Mapped[str] = mapped_column(String(32), default="postgresql")
    graph_backend: Mapped[str] = mapped_column(String(32), default="postgresql")
    parsing_concurrency: Mapped[int] = mapped_column(Integer, default=2)
    chunking_concurrency: Mapped[int] = mapped_column(Integer, default=2)
    embedding_concurrency: Mapped[int] = mapped_column(Integer, default=2)
    graph_concurrency: Mapped[int] = mapped_column(Integer, default=1)


class RagModelConfig(RagTimestamped, Base):
    __tablename__ = "model_configs"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_id", "kind", name="uq_rag_model_configs_kb_kind"
        ),
        {"schema": RAG_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    protocol: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str] = mapped_column(String(2048))
    model_name: Mapped[str] = mapped_column(String(256))
    thinking_effort: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12))
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RagDocument(RagTimestamped, Base):
    __tablename__ = "documents"
    __table_args__ = ({"schema": RAG_SCHEMA},)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    original_filename: Mapped[str] = mapped_column(String(512))
    stored_filename: Mapped[str] = mapped_column(String(768))
    content_type: Mapped[str] = mapped_column(String(256))
    extension: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_backend: Mapped[str] = mapped_column(String(32), default="postgresql")
    storage_key: Mapped[str] = mapped_column(String(128), unique=True)
    # The PostgreSQL file-storage adapter owns this bytea payload.  Future
    # backends (for example MinIO) can leave it null and resolve storage_key.
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    processing_generation: Mapped[int] = mapped_column(Integer, default=1)
    parsing_backend: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parsing_status: Mapped[str] = mapped_column(String(32), default="not_started")
    parsing_progress: Mapped[int] = mapped_column(Integer, default=0)
    parsing_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsing_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    chunking_strategy: Mapped[str] = mapped_column(String(32), default="fixed")
    chunking_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    chunking_status: Mapped[str] = mapped_column(String(32), default="not_started")
    chunking_progress: Mapped[int] = mapped_column(Integer, default=0)
    chunking_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunking_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunking_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    vectorization_status: Mapped[str] = mapped_column(String(32), default="not_started")
    vectorization_progress: Mapped[int] = mapped_column(Integer, default=0)
    vectorization_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    vectorization_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    vectorization_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    graph_status: Mapped[str] = mapped_column(String(32), default="not_started")
    graph_progress: Mapped[int] = mapped_column(Integer, default=0)
    graph_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    graph_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    graph_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentBlock(Base):
    __tablename__ = "document_blocks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "ordinal", name="uq_document_blocks_document_ordinal"
        ),
        {"schema": RAG_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "generation",
            "ordinal",
            name="uq_chunks_document_generation_ordinal",
        ),
        {"schema": RAG_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.documents.id", ondelete="CASCADE"), index=True
    )
    block_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.document_blocks.id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    strategy_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChunkVector(Base):
    __tablename__ = "chunk_vectors"
    __table_args__ = (
        UniqueConstraint("chunk_id", name="uq_chunk_vectors_chunk"),
        {"schema": RAG_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.documents.id", ondelete="CASCADE"), index=True
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.chunks.id", ondelete="CASCADE"), index=True
    )
    model_fingerprint: Mapped[str] = mapped_column(String(64))
    dimension: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GraphArtifact(RagTimestamped, Base):
    __tablename__ = "graph_artifacts"
    __table_args__ = ({"schema": RAG_SCHEMA},)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rag.documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(256))
    model_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_graph_ids: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(32), default="ready")


class GraphNode(Base):
    __tablename__ = "graph_nodes"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id", "canonical_key", name="uq_graph_nodes_artifact_key"
        ),
        {"schema": RAG_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.graph_artifacts.id", ondelete="CASCADE"), index=True
    )
    canonical_key: Mapped[str] = mapped_column(String(768))
    name: Mapped[str] = mapped_column(String(512))
    entity_type: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    properties: Mapped[dict] = mapped_column(JSONB, default=dict)


class GraphEdge(Base):
    __tablename__ = "graph_edges"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "source_node_id",
            "relation",
            "target_node_id",
            name="uq_graph_edges_key",
        ),
        {"schema": RAG_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.graph_artifacts.id", ondelete="CASCADE"), index=True
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.graph_nodes.id", ondelete="CASCADE"), index=True
    )
    target_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.graph_nodes.id", ondelete="CASCADE"), index=True
    )
    relation: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    properties: Mapped[dict] = mapped_column(JSONB, default=dict)


class GraphEvidence(Base):
    __tablename__ = "graph_evidence"
    __table_args__ = ({"schema": RAG_SCHEMA},)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.graph_artifacts.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rag.graph_nodes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    edge_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rag.graph_edges.id", ondelete="CASCADE"), nullable=True, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.documents.id", ondelete="CASCADE"), index=True
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.chunks.id", ondelete="CASCADE"), index=True
    )
    model_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = ({"schema": RAG_SCHEMA},)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.documents.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    leased_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


Index(
    "uq_processing_jobs_active_stage",
    ProcessingJob.document_id,
    ProcessingJob.kind,
    unique=True,
    postgresql_where=text("status IN ('queued', 'running')"),
)


class RagOperation(Base):
    __tablename__ = "operations"
    __table_args__ = ({"schema": RAG_SCHEMA},)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform.users.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform.workspaces.id", ondelete="CASCADE"), index=True
    )
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rag.knowledge_bases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    leased_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
