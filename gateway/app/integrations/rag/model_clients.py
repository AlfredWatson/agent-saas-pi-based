from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import os
import socket
from urllib.parse import urlparse

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.core.config import get_settings
from app.core.encryption import decrypt
from app.db.rag.models import RagModelConfig
from app.domain.rag.schemas import (
    GraphExtraction,
    ModelConfigInput,
    RerankerModelConfigInput,
)


def _bypass_proxy_for_local_development(base_url: str) -> None:
    settings = get_settings()
    if not settings.rag_model_base_url_allow_private:
        return
    hostname = urlparse(base_url).hostname
    if hostname is None:
        return
    try:
        is_local = not ipaddress.ip_address(hostname).is_global
    except ValueError:
        is_local = hostname.casefold() == "localhost"
    if not is_local:
        return
    for variable in ("NO_PROXY", "no_proxy"):
        existing = [item for item in os.environ.get(variable, "").split(",") if item]
        if hostname not in existing:
            os.environ[variable] = ",".join([*existing, hostname])


def model_fingerprint(
    kind: str, body: ModelConfigInput | RerankerModelConfigInput
) -> str:
    material = "\0".join(
        (
            kind,
            body.protocol,
            body.base_url.rstrip("/"),
            body.model_name,
            getattr(body, "thinking_effort", None) or "",
            body.api_key,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


async def validate_base_url(value: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("invalid_model_base_url")
    settings = get_settings()
    if parsed.scheme != "https" and not settings.rag_model_base_url_allow_private:
        raise ValueError("model_base_url_https_required")
    if settings.rag_model_base_url_allow_private:
        return
    try:
        addresses = [ipaddress.ip_address(parsed.hostname)]
    except ValueError:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo, parsed.hostname, parsed.port or 443
            )
        except socket.gaierror as exc:
            raise ValueError("model_base_url_unresolvable") from exc
        addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    for address in addresses:
        if not address.is_global:
            raise ValueError("private_model_base_url_forbidden")


def embedding_client(body: ModelConfigInput) -> OpenAIEmbeddings:
    if body.protocol != "openai":
        raise ValueError("embedding_protocol_must_be_openai")
    _bypass_proxy_for_local_development(body.base_url)
    settings = get_settings()
    return OpenAIEmbeddings(
        model=body.model_name,
        base_url=body.base_url.rstrip("/"),
        api_key=body.api_key,
        check_embedding_ctx_length=False,
        timeout=settings.rag_model_request_timeout_seconds,
        max_retries=settings.rag_model_max_retries,
    )


def chat_client(body: ModelConfigInput, *, max_tokens: int | None = None):
    _bypass_proxy_for_local_development(body.base_url)
    settings = get_settings()
    if body.protocol == "openai":
        options = {}
        if body.thinking_effort:
            options["reasoning_effort"] = body.thinking_effort
        elif settings.rag_openai_chat_template_disable_thinking:
            options["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        if max_tokens is not None:
            options["max_tokens"] = max_tokens
        return ChatOpenAI(
            model=body.model_name,
            base_url=body.base_url.rstrip("/"),
            api_key=body.api_key,
            temperature=0,
            timeout=settings.rag_model_request_timeout_seconds,
            max_retries=settings.rag_model_max_retries,
            **options,
        )
    if body.protocol == "anthropic":
        options = {}
        if body.thinking_effort:
            efforts = {"low", "medium", "high", "xhigh", "max"}
            if body.thinking_effort not in efforts:
                raise ValueError("invalid_anthropic_thinking_effort")
            options["effort"] = body.thinking_effort
        if max_tokens is not None:
            options["max_tokens"] = max_tokens
        return ChatAnthropic(
            model=body.model_name,
            base_url=body.base_url.rstrip("/"),
            api_key=body.api_key,
            temperature=0,
            timeout=settings.rag_model_request_timeout_seconds,
            max_retries=settings.rag_model_max_retries,
            **options,
        )
    raise ValueError("invalid_llm_protocol")


async def verify_embedding(body: ModelConfigInput) -> int:
    await validate_base_url(body.base_url)
    try:
        vector = await embedding_client(body).aembed_query(
            "pi-saas-rag-embedding-verification"
        )
    except Exception as exc:
        raise ValueError("embedding_model_verification_failed") from exc
    if not vector or any(not math.isfinite(value) for value in vector):
        raise ValueError("invalid_embedding_response")
    return len(vector)


async def verify_llm(body: ModelConfigInput) -> None:
    await validate_base_url(body.base_url)
    try:
        structured = chat_client(body).with_structured_output(GraphExtraction)
        result = await structured.ainvoke(
            "Return a graph with one node named verification, entity_type test, and no edges."
        )
    except Exception as exc:
        raise ValueError("llm_model_verification_failed") from exc
    if not isinstance(result, GraphExtraction):
        result = GraphExtraction.model_validate(result)
    if not result.nodes:
        raise ValueError("llm_structured_output_verification_failed")


def input_from_stored(
    config: RagModelConfig,
) -> ModelConfigInput | RerankerModelConfigInput:
    aad = f"rag:{config.knowledge_base_id}:{config.kind}:{config.id}:{config.protocol}".encode()
    values = {
        "protocol": config.protocol,
        "base_url": config.base_url,
        "api_key": decrypt(config.ciphertext, config.nonce, aad),
        "model_name": config.model_name,
    }
    if config.kind == "reranker":
        return RerankerModelConfigInput(**values)
    return ModelConfigInput(**values, thinking_effort=config.thinking_effort)
