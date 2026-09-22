import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.documents import Document

from app.domain.rag.schemas import RetrievalInput, RerankerModelConfigInput
from app.integrations.rag import reranker as reranker_module
from app.integrations.rag.reranker import (
    RerankResult,
    RerankerError,
    VllmReranker,
    _results,
)
from app.services.rag import retrieval


def test_vllm_reranker_posts_stable_request_and_sorts_results(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        is_error = False

        def json(self):
            return {
                "results": [
                    {"index": 1, "relevance_score": 0.4},
                    {"index": 0, "relevance_score": 0.9},
                ]
            }

    class Client:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(reranker_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        reranker_module,
        "get_settings",
        lambda: SimpleNamespace(
            rag_model_request_timeout_seconds=12, rag_model_max_retries=0
        ),
    )
    monkeypatch.setattr(
        reranker_module, "_bypass_proxy_for_local_development", lambda _: None
    )

    result = asyncio.run(
        VllmReranker(
            RerankerModelConfigInput(
                base_url="https://reranker.example/v1",
                api_key="secret",
                model_name="reranker",
            )
        ).rerank("question", ["first", "second"], 2)
    )

    assert result == [
        RerankResult(index=0, score=0.9),
        RerankResult(index=1, score=0.4),
    ]
    assert calls[1] == (
        "https://reranker.example/v1/rerank",
        {
            "headers": {"Authorization": "Bearer secret"},
            "json": {
                "model": "reranker",
                "query": "question",
                "documents": ["first", "second"],
                "top_n": 2,
            },
        },
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": [{"index": 2, "relevance_score": 0.1}]},
        {"results": [{"index": 0, "relevance_score": float("nan")}]},
        {
            "results": [
                {"index": 0, "relevance_score": 0.1},
                {"index": 0, "relevance_score": 0.2},
            ]
        },
    ],
)
def test_vllm_reranker_rejects_invalid_results(payload):
    with pytest.raises(RerankerError, match="invalid_reranker_response"):
        _results(payload, document_count=2, top_n=1)


def test_vllm_reranker_retries_transient_failure(monkeypatch):
    attempts = []

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        @property
        def is_error(self):
            return self.status_code >= 400

        def json(self):
            return {}

    class Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, *_args, **_kwargs):
            attempts.append(1)
            return Response(503 if len(attempts) == 1 else 200)

    monkeypatch.setattr(reranker_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        reranker_module,
        "get_settings",
        lambda: SimpleNamespace(
            rag_model_request_timeout_seconds=12, rag_model_max_retries=1
        ),
    )
    monkeypatch.setattr(
        reranker_module, "_bypass_proxy_for_local_development", lambda _: None
    )
    with pytest.raises(RerankerError, match="invalid_reranker_response"):
        asyncio.run(
            VllmReranker(
                RerankerModelConfigInput(
                    base_url="https://reranker.example",
                    api_key="secret",
                    model_name="reranker",
                )
            ).rerank("question", ["document"], 1)
        )
    assert len(attempts) == 2


def test_reranked_items_preserve_retrieval_scores_and_degrade():
    documents = [
        (
            Document(
                page_content="first", metadata={"chunk_id": "1", "document_id": "d"}
            ),
            0.2,
        ),
        (
            Document(
                page_content="second", metadata={"chunk_id": "2", "document_id": "d"}
            ),
            0.1,
        ),
    ]

    class Reranker:
        async def rerank(self, *_args, **_kwargs):
            return [RerankResult(index=1, score=0.9)]

    items, status = asyncio.run(
        retrieval._render_ranked_rows(
            documents,
            query="question",
            top_k=1,
            source="vector",
            reranker=Reranker(),
            reranker_config=SimpleNamespace(fingerprint="fingerprint"),
            knowledge_base_id=uuid4(),
        )
    )
    assert items[0]["chunk_id"] == "2"
    assert items[0]["score"] == 0.9
    assert items[0]["retrieval_score"] == 0.1
    assert status == {"configured": True, "applied": True, "error": None}

    class FailingReranker:
        async def rerank(self, *_args, **_kwargs):
            raise RerankerError("reranker_unavailable")

    fallback, fallback_status = asyncio.run(
        retrieval._render_ranked_rows(
            documents,
            query="question",
            top_k=1,
            source="vector",
            reranker=FailingReranker(),
            reranker_config=SimpleNamespace(fingerprint="fingerprint"),
            knowledge_base_id=uuid4(),
        )
    )
    assert fallback == [
        {
            "chunk_id": "1",
            "document_id": "d",
            "text": "first",
            "metadata": {},
            "score": 0.2,
            "source": "vector",
        }
    ]
    assert fallback_status == {
        "configured": True,
        "applied": False,
        "error": "reranker_unavailable",
    }


def test_vector_retrieve_passes_all_candidates_to_reranker(monkeypatch):
    rows = [
        (
            Document(
                page_content=f"document-{index}",
                metadata={"chunk_id": str(index), "document_id": "d"},
            ),
            float(index),
        )
        for index in range(5)
    ]
    captured = {}

    class Store:
        async def asimilarity_search_with_relevance_scores(self, _query, **kwargs):
            captured["k"] = kwargs["k"]
            return rows

    class Reranker:
        async def rerank(self, _query, documents, top_n):
            captured["documents"] = documents
            captured["top_n"] = top_n
            return [RerankResult(index=4, score=1.0), RerankResult(index=3, score=0.9)]

    async def embedding_config(*_):
        return SimpleNamespace()

    monkeypatch.setattr(retrieval, "_embedding_config", embedding_config)
    monkeypatch.setattr(retrieval, "embedding_client", lambda _: object())
    monkeypatch.setattr(retrieval, "input_from_stored", lambda _: object())
    monkeypatch.setattr(retrieval, "get_vector_store", lambda *_: Store())

    class Db:
        async def get(self, *_):
            return SimpleNamespace()

    items, status = asyncio.run(
        retrieval.vector_retrieve(
            Db(),
            uuid4(),
            RetrievalInput(query="question", top_k=2, candidate_k=5),
            reranker=Reranker(),
            reranker_config=SimpleNamespace(fingerprint="fingerprint"),
        )
    )
    assert captured == {
        "k": 5,
        "documents": [f"document-{index}" for index in range(5)],
        "top_n": 2,
    }
    assert [item["chunk_id"] for item in items] == ["4", "3"]
    assert status["applied"] is True


def test_hybrid_retrieve_keeps_candidate_k_only_when_reranker_is_enabled(monkeypatch):
    knowledge_base_id = uuid4()
    chunks = [
        SimpleNamespace(
            id=uuid4(),
            document_id=uuid4(),
            text=f"chunk-{index}",
            metadata_={},
        )
        for index in range(5)
    ]
    vector_rows = [
        (
            Document(
                page_content=chunk.text,
                metadata={
                    "chunk_id": str(chunk.id),
                    "document_id": str(chunk.document_id),
                },
            ),
            1.0,
        )
        for chunk in chunks[:2]
    ]
    captured = {}

    class Store:
        async def asimilarity_search_with_relevance_scores(self, *_args, **_kwargs):
            return vector_rows

    class Scalars:
        def all(self):
            return chunks

    class Db:
        async def get(self, *_):
            return SimpleNamespace()

        async def scalars(self, _statement):
            return Scalars()

    class Reranker:
        async def rerank(self, _query, documents, top_n):
            captured["documents"] = documents
            captured["top_n"] = top_n
            return [RerankResult(index=3, score=0.9), RerankResult(index=2, score=0.8)]

    async def embedding_config(*_):
        return SimpleNamespace()

    monkeypatch.setattr(retrieval, "_embedding_config", embedding_config)
    monkeypatch.setattr(retrieval, "embedding_client", lambda _: object())
    monkeypatch.setattr(retrieval, "input_from_stored", lambda _: object())
    monkeypatch.setattr(retrieval, "get_vector_store", lambda *_: Store())
    monkeypatch.setattr(retrieval, "tokenize", lambda text: [text])
    body = RetrievalInput(
        query="question", top_k=2, candidate_k=4, vector_k=2, bm25_k=5
    )

    no_rerank, status = asyncio.run(
        retrieval.hybrid_retrieve(
            Db(),
            knowledge_base_id,
            body,
            reranker=None,
            reranker_config=None,
        )
    )
    assert len(no_rerank) == 2
    assert status == {"configured": False, "applied": False, "error": None}

    _, reranked_status = asyncio.run(
        retrieval.hybrid_retrieve(
            Db(),
            knowledge_base_id,
            body,
            reranker=Reranker(),
            reranker_config=SimpleNamespace(fingerprint="fingerprint"),
        )
    )
    assert len(captured["documents"]) == 4
    assert captured["top_n"] == 2
    assert reranked_status["applied"] is True


def test_graph_retrieval_never_reads_reranker_configuration(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("reranker configuration must not be queried for graph")

    async def graph(*_args, **_kwargs):
        return {"nodes": [], "edges": [], "evidence": []}

    monkeypatch.setattr(retrieval, "_reranker_config", forbidden)
    monkeypatch.setattr(retrieval, "graph_retrieve", graph)
    result = asyncio.run(
        retrieval.retrieve(
            None, uuid4(), RetrievalInput(query="question", mode="graph")
        )
    )
    assert result == {"mode": "graph", "nodes": [], "edges": [], "evidence": []}
