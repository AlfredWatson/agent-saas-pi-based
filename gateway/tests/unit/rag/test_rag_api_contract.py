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
    assert f"{prefix}/jobs/vectorization" in paths
    assert f"{prefix}/jobs/graph-extraction" in paths
    assert f"{prefix}/graphs:merge" in paths
    assert f"{prefix}/retrieve" in paths
    assert f"{prefix}/copy" in paths
    assert not any("answer" in path for path in paths)


def test_model_read_contract_never_contains_api_key():
    schema = app.openapi()
    operation = schema["paths"][
        "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/models"
    ]["get"]
    assert "api_key" not in str(operation)
