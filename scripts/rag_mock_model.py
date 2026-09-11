"""Local OpenAI-compatible model stub used by the RAG HTTP smoke test."""

from __future__ import annotations

import hashlib
import json
import math

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="RAG smoke model")


class FailureControl(BaseModel):
    embedding_fail_after: int | None = None
    chat_fail_after: int | None = None


state = {
    "embedding_calls": 0,
    "chat_calls": 0,
    "embedding_successes": 0,
    "chat_successes": 0,
    "embedding_fail_after": None,
    "chat_fail_after": None,
}


def embedding(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    values = [(digest[index] - 127.5) / 127.5 for index in range(8)]
    norm = math.sqrt(sum(value * value for value in values)) or 1
    return [value / norm for value in values]


@app.post("/control/reset")
async def reset(control: FailureControl):
    state.update(
        embedding_calls=0,
        chat_calls=0,
        embedding_successes=0,
        chat_successes=0,
        embedding_fail_after=control.embedding_fail_after,
        chat_fail_after=control.chat_fail_after,
    )
    return state


@app.get("/control/state")
async def read_state():
    return state


@app.post("/v1/embeddings")
async def embeddings(body: dict):
    state["embedding_calls"] += 1
    limit = state["embedding_fail_after"]
    if limit is not None and state["embedding_successes"] >= limit:
        raise HTTPException(503, "injected_embedding_failure")
    inputs = body.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    state["embedding_successes"] += len(inputs)
    return {
        "object": "list",
        "model": body.get("model", "smoke-embedding"),
        "data": [
            {"object": "embedding", "index": index, "embedding": embedding(text)}
            for index, text in enumerate(inputs)
        ],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


@app.post("/v1/chat/completions")
async def chat_completions(body: dict):
    state["chat_calls"] += 1
    limit = state["chat_fail_after"]
    if limit is not None and state["chat_successes"] >= limit:
        raise HTTPException(503, "injected_chat_failure")
    state["chat_successes"] += 1
    graph = {
        "nodes": [
            {
                "name": "Acme",
                "entity_type": "Company",
                "description": "A company in the smoke corpus",
                "properties": {"source": "smoke"},
            },
            {
                "name": "RAG",
                "entity_type": "Technology",
                "description": "Retrieval augmented generation",
                "properties": {},
            },
        ],
        "edges": [
            {
                "source": "Acme",
                "relation": "uses",
                "target": "RAG",
                "description": "Acme uses RAG",
                "properties": {},
            }
        ],
    }
    message: dict = {"role": "assistant", "content": json.dumps(graph)}
    tools = body.get("tools") or []
    if tools:
        function = tools[0].get("function", tools[0])
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_rag_smoke",
                    "type": "function",
                    "function": {
                        "name": function["name"],
                        "arguments": json.dumps(graph),
                    },
                }
            ],
        }
    return {
        "id": "chatcmpl-rag-smoke",
        "object": "chat.completion",
        "created": 0,
        "model": body.get("model", "smoke-llm"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tools else "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=22001)
