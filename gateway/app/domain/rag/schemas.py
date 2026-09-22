from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field


GraphPropertyValue = Annotated[str, Field(max_length=120)] | int | float | bool
GraphProperties = dict[str, GraphPropertyValue]


class ExtractedNode(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    entity_type: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=160)
    properties: GraphProperties = Field(default_factory=dict, max_length=4)


class ExtractedEdge(BaseModel):
    source: str = Field(min_length=1, max_length=512)
    relation: str = Field(min_length=1, max_length=256)
    target: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=160)
    properties: GraphProperties = Field(default_factory=dict, max_length=4)


class GraphExtraction(BaseModel):
    nodes: list[ExtractedNode] = Field(default_factory=list, max_length=8)
    edges: list[ExtractedEdge] = Field(default_factory=list, max_length=12)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    file_backend: Literal["postgresql"]
    block_backend: Literal["postgresql"]
    chunk_backend: Literal["postgresql"]
    vector_backend: Literal["postgresql", "milvus", "chroma", "qdrant"]
    graph_backend: Literal["postgresql"]


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    parsing_concurrency: int | None = Field(default=None, ge=1)
    chunking_concurrency: int | None = Field(default=None, ge=1)
    embedding_concurrency: int | None = Field(default=None, ge=1)
    graph_concurrency: int | None = Field(default=None, ge=1)


class KnowledgeBaseCopy(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class ModelConfigInput(BaseModel):
    protocol: Literal["openai", "anthropic"] = "openai"
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(min_length=1, max_length=4096)
    model_name: str = Field(min_length=1, max_length=256)
    thinking_effort: str | None = Field(default=None, max_length=64)


class RerankerModelConfigInput(BaseModel):
    """Configuration for Gateway's stable reranking interface.

    ``protocol`` names the concrete Gateway adapter, not an industry-standard
    reranking wire protocol.  Additional adapters can be added without
    changing retrieval callers.
    """

    protocol: Literal["vllm"] = "vllm"
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(min_length=1, max_length=4096)
    model_name: str = Field(min_length=1, max_length=256)


class JobSubmit(BaseModel):
    document_ids: list[UUID] = Field(min_length=1, max_length=100)


class ParsingJobItem(BaseModel):
    document_id: UUID
    processor_backend: str | None = Field(default=None, min_length=1, max_length=32)


class ParsingJobSubmit(BaseModel):
    items: list[ParsingJobItem] = Field(min_length=1, max_length=100)


class ChunkingConfigInput(BaseModel):
    strategy: Literal["fixed", "regex", "semantic"]
    config: dict[str, object] | None = None


class GraphMergeInput(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    graph_ids: list[UUID] = Field(min_length=2, max_length=100)


class RetrievalInput(BaseModel):
    query: str = Field(min_length=1, max_length=10000)
    mode: Literal["vector", "hybrid", "graph"] = "vector"
    document_ids: list[UUID] | None = Field(default=None, max_length=100)
    top_k: int = Field(default=5, ge=1, le=100)
    candidate_k: int = Field(default=20, ge=1, le=500)
    min_score: float | None = Field(default=None, ge=-1, le=1)
    vector_k: int = Field(default=20, ge=1, le=500)
    bm25_k: int = Field(default=20, ge=1, le=500)
    vector_weight: float = Field(default=1.0, ge=0, le=10)
    bm25_weight: float = Field(default=1.0, ge=0, le=10)
    rrf_k: int = Field(default=60, ge=1, le=1000)
    top_entities: int = Field(default=10, ge=1, le=100)
    max_hops: int = Field(default=1, ge=0, le=3)
