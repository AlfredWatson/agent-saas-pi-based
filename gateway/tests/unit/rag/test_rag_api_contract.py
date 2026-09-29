from app.main import app


def test_rag_routes_are_workspace_scoped_and_expose_no_answer_generation():
    paths = app.openapi()["paths"]
    prefix = "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}"
    assert "/api/v1/rag/capabilities" in paths
    assert f"{prefix}/documents" in paths
    assert f"{prefix}/jobs/parsing" in paths
    assert f"{prefix}/jobs/chunking" in paths
    assert f"{prefix}/documents/{{document_id}}/chunking-config" in paths
    assert f"{prefix}/documents/{{document_id}}/blocks" in paths
    assert {"get", "delete"}.issubset(
        paths[f"{prefix}/documents/{{document_id}}/blocks"]
    )
    assert {"get", "delete"}.issubset(
        paths[f"{prefix}/documents/{{document_id}}/chunks"]
    )
    assert "patch" in paths[f"{prefix}/documents/{{document_id}}/blocks/{{block_id}}"]
    assert "patch" in paths[f"{prefix}/documents/{{document_id}}/chunks/{{chunk_id}}"]
    assert f"{prefix}/jobs/vectorization" in paths
    assert f"{prefix}/jobs/graph-extraction" in paths
    assert f"{prefix}/graphs:merge" in paths
    assert f"{prefix}/retrieve" in paths
    assert f"{prefix}/reranker-model" in paths
    assert f"{prefix}/copy" in paths
    assert not any("answer" in path for path in paths)


def test_model_read_contract_never_contains_api_key():
    schema = app.openapi()
    operation = schema["paths"][
        "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/models"
    ]["get"]
    assert "api_key" not in str(operation)


def test_reranker_model_contract_only_allows_vllm():
    components = app.openapi()["components"]["schemas"]
    schema = components["RerankerModelConfigInput"]
    assert schema["properties"]["protocol"]["const"] == "vllm"
    assert (
        "vllm" not in components["ModelConfigInput"]["properties"]["protocol"]["enum"]
    )
    path = app.openapi()["paths"][
        "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/reranker-model"
    ]
    assert {"put", "delete"}.issubset(path)


def test_public_retrieval_defaults_to_reranking_and_accepts_opt_out():
    from app.domain.rag.schemas import RetrievalInput

    assert RetrievalInput(query="question").rerank is True
    assert RetrievalInput(query="question", rerank=False).rerank is False
    schema = app.openapi()["components"]["schemas"]["RetrievalInput"]
    assert schema["properties"]["rerank"]["type"] == "boolean"


def test_knowledge_base_schema_exposes_all_supported_vector_backends():
    schema = app.openapi()["components"]["schemas"]["KnowledgeBaseCreate"]
    assert schema["properties"]["vector_backend"]["enum"] == [
        "postgresql",
        "milvus",
        "chroma",
        "qdrant",
    ]
