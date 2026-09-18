from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable

import tiktoken
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

_ENCODING = tiktoken.get_encoding("cl100k_base")


def token_count(text: str) -> int:
    return len(_ENCODING.encode(text))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _bounded(
    splitter, documents: Iterable[Document], max_tokens: int
) -> list[Document]:
    result: list[Document] = []
    fallback = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name="cl100k_base", chunk_size=max_tokens, chunk_overlap=0
    )
    for item in splitter.split_documents(list(documents)):
        if token_count(item.page_content) <= max_tokens:
            result.append(item)
        else:
            result.extend(fallback.split_documents([item]))
    return result


def _cosine_distance(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 1.0
    return 1 - numerator / (left_norm * right_norm)


def _percentile(values: list[float], amount: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 1.0
    index = (len(ordered) - 1) * amount / 100
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def _semantic_split(
    documents: list[Document], embeddings, threshold_amount: float
) -> list[Document]:
    results: list[Document] = []
    for document in documents:
        sentences = [
            item.strip()
            for item in re.split(r"(?<=[。！？.!?])\s*", document.page_content)
            if item.strip()
        ]
        if len(sentences) < 2:
            results.append(document)
            continue
        vectors = embeddings.embed_documents(sentences)
        distances = [
            _cosine_distance(vectors[index], vectors[index + 1])
            for index in range(len(vectors) - 1)
        ]
        threshold = _percentile(distances, threshold_amount)
        group: list[str] = []
        for index, sentence in enumerate(sentences):
            group.append(sentence)
            if index == len(sentences) - 1 or distances[index] >= threshold:
                results.append(
                    Document(
                        page_content=" ".join(group), metadata=dict(document.metadata)
                    )
                )
                group = []
    return results


def split_documents(
    strategy: str, config: dict, documents: list[Document], embeddings=None
) -> list[Document]:
    maximum = int(config["max_token_size"])
    if strategy == "fixed":
        splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base",
            chunk_size=maximum,
            chunk_overlap=int(config["overlap_token_size"]),
            separators=[str(config["split_by_character"]), "\n", " ", ""],
        )
        return splitter.split_documents(documents)
    if strategy == "regex":
        splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base",
            chunk_size=maximum,
            chunk_overlap=0,
            separators=[str(config["re_expression"]), ""],
            is_separator_regex=True,
        )
        return splitter.split_documents(documents)
    if strategy == "semantic":
        if embeddings is None:
            raise ValueError("embedding_model_required_for_semantic_chunking")
        semantic_documents = _semantic_split(
            documents, embeddings, float(config["breakpoint_threshold"])
        )
        fallback = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base", chunk_size=maximum, chunk_overlap=0
        )
        return _bounded(fallback, semantic_documents, maximum)
    raise ValueError("invalid_chunking_strategy")
