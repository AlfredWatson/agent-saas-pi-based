#!/usr/bin/env python3
"""Prepare deterministic, one-corpus-item-per-chunk MTEB retrieval samples."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import tiktoken

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gateway"))

DATASET_NAMES = (
    "CmedqaRetrieval",
    "DuRetrieval",
    "EcomRetrieval",
    "T2Retrieval",
)
CL100K = tiktoken.get_encoding("cl100k_base")


def read_parquet(directory: Path, columns: list[str]) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    paths = sorted(directory.glob("*.parquet"))
    if not paths:
        raise ValueError(f"no parquet files in {directory}")
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(pq.read_table(path, columns=columns).to_pylist())
    return rows


def encode_id(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def decode_id(value: str) -> str:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")


def render_body(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "").strip()
    body = str(row.get("text") or "").strip()
    combined = "\n".join(part for part in (title, body) if part)
    combined = "\n".join(combined.splitlines())
    # The Markdown processor interprets headings as block boundaries. Escape
    # source headings so only the generated corpus-ID heading starts a block.
    return re.sub(r"(?m)^(#{1,6})(\s+)", r"\\\1\2", combined)


def load_tokenizer(model: str, tokenizer_json: Path | None):
    from tokenizers import Tokenizer

    if tokenizer_json is None:
        from huggingface_hub import hf_hub_download

        tokenizer_json = Path(hf_hub_download(repo_id=model, filename="tokenizer.json"))
    return Tokenizer.from_file(str(tokenizer_json))


def fits(text: str, tokenizer, max_tokens: int) -> bool:
    return (
        bool(text)
        and len(tokenizer.encode(text).ids) <= max_tokens
        and len(CL100K.encode(text)) <= max_tokens
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def prepare_dataset(
    source: Path,
    output: Path,
    *,
    tokenizer,
    max_tokens: int,
    query_count: int = 200,
    corpus_count: int = 2000,
    seed: int = 42,
    items_per_file: int = 100,
    tokenizer_name: str | None = None,
) -> dict[str, Any]:
    if query_count <= 0 or corpus_count < query_count or items_per_file <= 0:
        raise ValueError("invalid sample sizes")
    name = source.name
    if name not in DATASET_NAMES:
        raise ValueError(f"unsupported dataset: {name}")
    queries = {
        str(row["_id"]): str(row["text"] or "").strip()
        for row in read_parquet(source / "queries", ["_id", "text"])
    }
    corpus = {
        str(row["_id"]): row
        for row in read_parquet(source / "corpus", ["_id", "text", "title"])
    }
    qrels: dict[str, set[str]] = defaultdict(set)
    for row in read_parquet(source / "data", ["query-id", "corpus-id", "score"]):
        if int(row["score"]) > 0:
            qrels[str(row["query-id"])].add(str(row["corpus-id"]))
    if not queries or not corpus or not qrels:
        raise ValueError(f"incomplete MTEB dataset: {name}")

    rng = random.Random(seed)
    query_ids = sorted(qrels)
    rng.shuffle(query_ids)
    selected_queries: list[str] = []
    positives: set[str] = set()
    excluded_queries = {
        "missing_query": 0,
        "oversize_query": 0,
        "missing_or_oversize_positive": 0,
        "positive_pool_full": 0,
    }
    body_cache: dict[str, str] = {}

    def body_for(corpus_id: str) -> str:
        if corpus_id not in body_cache:
            body_cache[corpus_id] = render_body(corpus[corpus_id])
        return body_cache[corpus_id]

    for query_id in query_ids:
        if len(selected_queries) == query_count:
            break
        query = queries.get(query_id)
        if not query:
            excluded_queries["missing_query"] += 1
            continue
        if not fits(query, tokenizer, max_tokens):
            excluded_queries["oversize_query"] += 1
            continue
        relevant = qrels[query_id]
        if any(
            corpus_id not in corpus
            or not fits(body_for(corpus_id), tokenizer, max_tokens)
            for corpus_id in relevant
        ):
            excluded_queries["missing_or_oversize_positive"] += 1
            continue
        if len(positives | relevant) > corpus_count:
            excluded_queries["positive_pool_full"] += 1
            continue
        selected_queries.append(query_id)
        positives.update(relevant)
    if len(selected_queries) != query_count:
        raise ValueError(
            f"{name}: only {len(selected_queries)}/{query_count} eligible queries; "
            f"excluded={excluded_queries}"
        )

    selected_corpus = set(positives)
    negative_ids = sorted(set(corpus) - positives)
    rng.shuffle(negative_ids)
    skipped_negatives = 0
    for corpus_id in negative_ids:
        if len(selected_corpus) >= corpus_count:
            break
        if fits(body_for(corpus_id), tokenizer, max_tokens):
            selected_corpus.add(corpus_id)
        else:
            skipped_negatives += 1
    if len(selected_corpus) != corpus_count:
        raise ValueError(
            f"{name}: only {len(selected_corpus)}/{corpus_count} eligible corpus items"
        )

    output.mkdir(parents=True, exist_ok=True)
    ordered_corpus = sorted(selected_corpus)
    manifest: list[dict[str, str | int]] = []
    files: list[str] = []
    for batch_number, start in enumerate(range(0, len(ordered_corpus), items_per_file)):
        part = ordered_corpus[start : start + items_per_file]
        filename = f"corpus-{batch_number:04d}.md"
        lines: list[str] = []
        for corpus_id in part:
            text = body_for(corpus_id)
            lines.extend((f"# corpus:{encode_id(corpus_id)}", text, ""))
            manifest.append(
                {
                    "corpus_id": corpus_id,
                    "heading": f"corpus:{encode_id(corpus_id)}",
                    "file": filename,
                    "embedding_tokens": len(tokenizer.encode(text).ids),
                    "chunk_tokens": len(CL100K.encode(text)),
                }
            )
        (output / filename).write_text("\n".join(lines), encoding="utf-8")
        files.append(filename)

    # These are the exact parser/splitter classes used by the worker. A failure
    # here means the benchmark cannot honestly claim one item per chunk.
    from app.domain.rag.chunking import split_documents
    from app.integrations.rag.processors import DefaultDocumentProcessor

    processor = DefaultDocumentProcessor()
    fixed = {
        "max_token_size": max_tokens,
        "overlap_token_size": 0,
        "split_by_character": "\n\n",
    }
    expected = {
        str(row["heading"]): body_for(str(row["corpus_id"])) for row in manifest
    }
    seen: set[str] = set()
    for filename in files:
        parsed = processor.parse(filename, (output / filename).read_bytes())
        chunks = split_documents("fixed", fixed, parsed)
        if len(chunks) != len(parsed):
            raise ValueError(f"{name}/{filename}: fixed split changed block count")
        for chunk in chunks:
            heading = chunk.metadata.get("heading")
            if (
                heading not in expected
                or heading in seen
                or chunk.page_content != expected[heading]
            ):
                raise ValueError(
                    f"{name}/{filename}: corpus item was split, merged, or altered: {heading}"
                )
            seen.add(heading)
    if len(seen) != len(manifest):
        raise ValueError(f"{name}: parsed chunk count mismatch")

    with (output / "corpus_manifest.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(manifest[0]))
        writer.writeheader()
        writer.writerows(manifest)
    write_json(
        output / "queries.json",
        [
            {
                "query_id": query_id,
                "text": queries[query_id],
                "relevant_corpus_ids": sorted(qrels[query_id]),
            }
            for query_id in selected_queries
        ],
    )
    details = {
        "dataset": name,
        "seed": seed,
        "query_count": len(selected_queries),
        "corpus_count": len(selected_corpus),
        "positive_corpus_count": len(positives),
        "max_input_tokens_after_reserve": max_tokens,
        "tokenizer_model": tokenizer_name,
        "excluded_queries": excluded_queries,
        "skipped_oversize_or_empty_negatives": skipped_negatives,
        "source_query_count": len(queries),
        "source_corpus_count": len(corpus),
        "source_positive_query_count": len(qrels),
        "files": files,
        "chunking_config": fixed,
    }
    write_json(output / "preparation.json", details)
    return details


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--datasets-dir", type=Path, default=ROOT / "test/datasets/MTEB"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="Qwen/Qwen3-Embedding-8B")
    parser.add_argument("--tokenizer-json", type=Path)
    parser.add_argument(
        "--max-input-tokens",
        type=int,
        required=True,
        help="Effective model/server limit before the 32-token reserve",
    )
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--corpus", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.max_input_tokens <= 64:
        parser.error("--max-input-tokens must exceed 64")
    tokenizer = load_tokenizer(args.model, args.tokenizer_json)
    for name in DATASET_NAMES:
        details = prepare_dataset(
            args.datasets_dir / name,
            args.output_dir / name,
            tokenizer=tokenizer,
            max_tokens=args.max_input_tokens - 32,
            query_count=args.queries,
            corpus_count=args.corpus,
            seed=args.seed,
            tokenizer_name=args.model,
        )
        print(
            f"{name}: {details['query_count']} queries, {details['corpus_count']} corpus items"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
