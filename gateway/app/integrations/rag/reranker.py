from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import get_settings
from app.domain.rag.schemas import ModelConfigInput, RerankerModelConfigInput
from app.integrations.rag.model_clients import (
    _bypass_proxy_for_local_development,
    validate_base_url,
)


@dataclass(frozen=True)
class RerankResult:
    index: int
    score: float


class Reranker(Protocol):
    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[RerankResult]: ...


class RerankerError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _results(payload: object, *, document_count: int, top_n: int) -> list[RerankResult]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RerankerError("invalid_reranker_response")
    parsed: list[RerankResult] = []
    seen: set[int] = set()
    for item in payload["results"]:
        if not isinstance(item, dict):
            raise RerankerError("invalid_reranker_response")
        index = item.get("index")
        score = item.get("relevance_score")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= document_count
            or index in seen
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(float(score))
        ):
            raise RerankerError("invalid_reranker_response")
        seen.add(index)
        parsed.append(RerankResult(index=index, score=float(score)))
    if len(parsed) != top_n:
        raise RerankerError("invalid_reranker_response")
    return sorted(parsed, key=lambda item: (-item.score, item.index))


class VllmReranker:
    def __init__(self, config: ModelConfigInput | RerankerModelConfigInput):
        self.config = config

    @property
    def endpoint(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/rerank"

    async def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[RerankResult]:
        if not documents or top_n < 1 or top_n > len(documents):
            raise RerankerError("invalid_reranker_request")
        _bypass_proxy_for_local_development(self.config.base_url)
        settings = get_settings()
        retries = settings.rag_model_max_retries
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        payload = {
            "model": self.config.model_name,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=settings.rag_model_request_timeout_seconds
                ) as client:
                    response = await client.post(
                        self.endpoint, headers=headers, json=payload
                    )
            except httpx.RequestError:
                if attempt < retries:
                    continue
                raise RerankerError("reranker_unavailable") from None
            if response.status_code in {408, 429} or response.status_code >= 500:
                if attempt < retries:
                    continue
                raise RerankerError("reranker_unavailable")
            if response.is_error:
                raise RerankerError("reranker_unavailable")
            try:
                return _results(
                    response.json(), document_count=len(documents), top_n=top_n
                )
            except ValueError as exc:
                raise RerankerError("invalid_reranker_response") from exc
        raise RerankerError("reranker_unavailable")


def get_reranker(
    config: ModelConfigInput | RerankerModelConfigInput,
) -> Reranker:
    if config.protocol != "vllm":
        raise RerankerError("reranker_unavailable")
    return VllmReranker(config)


async def verify_reranker(body: RerankerModelConfigInput) -> None:
    await validate_base_url(body.base_url)
    try:
        await get_reranker(body).rerank(
            "pi-saas-rag-reranker-verification",
            ["verification relevant document", "unrelated document"],
            top_n=1,
        )
    except RerankerError as exc:
        if exc.code == "invalid_reranker_response":
            raise
        raise RerankerError("reranker_model_verification_failed") from exc
    except Exception as exc:
        raise RerankerError("reranker_model_verification_failed") from exc
