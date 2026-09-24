#!/usr/bin/env python3
# ruff: noqa: E402
"""Public-HTTP MTEB vector retrieval benchmark with Milvus knowledge bases.

Run Gateway, RAG worker, PostgreSQL, Redis, Milvus and the embedding server::

    uv run python test/performanc-requirements/mteb_retrieval_eval.py

Results and prepared samples remain under ./tmp/rag-retrieval-eval/. Created
Gateway resources are deleted by default; --keep-resources retains them.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "test"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flow_env import configured_value, parse_dotenv
from prepare_mteb import (
    DATASET_NAMES,
    decode_id,
    load_tokenizer,
    prepare_dataset,
    write_json,
)
from rag_user_flow import (
    Config,
    FlowError,
    STATIC_BACKENDS,
    UserFlow,
    gateway_from_env_file,
    require,
)

K_VALUES = (5, 10, 15, 20)
LOG = logging.getLogger("mteb-retrieval-eval")


def score_ranking(
    ranked_ids: list[str], relevant_ids: set[str]
) -> dict[int, dict[str, float]]:
    """Macro-friendly per-query metrics; unjudged results count as non-relevant."""
    if not relevant_ids:
        raise ValueError("query has no positive labels")
    if len(ranked_ids) != len(set(ranked_ids)):
        raise ValueError("retrieval returned duplicate corpus IDs")
    result: dict[int, dict[str, float]] = {}
    for k in K_VALUES:
        top = ranked_ids[:k]
        hits = sum(corpus_id in relevant_ids for corpus_id in top)
        first = next(
            (
                rank
                for rank, corpus_id in enumerate(top, 1)
                if corpus_id in relevant_ids
            ),
            None,
        )
        result[k] = {
            "recall": hits / len(relevant_ids),
            "precision": hits / k,
            "hit_rate": float(hits > 0),
            "mrr": 1.0 / first if first else 0.0,
        }
    return result


def aggregate_scores(
    per_query: list[dict[int, dict[str, float]]],
) -> dict[int, dict[str, float]]:
    if not per_query:
        raise ValueError("cannot aggregate zero queries")
    return {
        k: {
            metric: mean(item[k][metric] for item in per_query)
            for metric in ("recall", "precision", "hit_rate", "mrr")
        }
        for k in K_VALUES
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError("cannot write empty CSV")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class Evaluation(UserFlow):
    def __init__(self, config: Config, output: Path):
        super().__init__(config)
        self.output = output
        self.knowledge_bases: dict[str, str] = {}

    def effective_embedding_limit(self, requested: int | None) -> int:
        endpoint = f"{self.config.embedding_base_url}/models"
        try:
            response = self.client.get(endpoint)
            response.raise_for_status()
            models = response.json().get("data", [])
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise FlowError(
                f"embedding model preflight failed at {endpoint}: {exc}"
            ) from exc
        selected = next(
            (
                item
                for item in models
                if isinstance(item, dict)
                and item.get("id") == self.config.embedding_model
            ),
            None,
        )
        require(
            isinstance(selected, dict),
            f"model {self.config.embedding_model!r} absent from {endpoint}",
        )
        served = selected.get("max_model_len")
        require(
            (isinstance(served, int) and served > 64) or requested is not None,
            "model server did not report max_model_len; pass --max-input-tokens",
        )
        if served is not None:
            require(
                isinstance(served, int) and served > 64,
                f"invalid max_model_len: {served!r}",
            )
        if requested is not None:
            require(requested > 64, "--max-input-tokens must exceed 64")
        limits = [value for value in (served, requested) if value is not None]
        if self.config.embedding_model == "Qwen3-Embedding-8B":
            limits.append(32768)
        return min(limits)

    def create_workspace(self) -> None:
        workspace = self.json_request(
            "POST",
            "/workspaces",
            expected=201,
            label="create-evaluation-workspace",
            json={"name": f"mteb-eval-{uuid4().hex[:10]}"},
        )
        self.workspace_id = workspace["id"]
        self.resources["workspace_id"] = self.workspace_id
        switched = self.json_request(
            "POST",
            f"/workspaces/{self.workspace_id}:switch",
            label="switch-evaluation-workspace",
        )
        require(switched.get("is_current") is True, "workspace switch failed")

    def create_knowledge_base(self, dataset: str) -> str:
        require(self.workspace_id is not None, "workspace is missing")
        kb = self.json_request(
            "POST",
            f"/workspaces/{self.workspace_id}/knowledge-bases",
            expected=201,
            label=f"create-{dataset}-knowledge-base",
            json={
                "name": f"mteb-{dataset}-{uuid4().hex[:8]}",
                **STATIC_BACKENDS,
                "vector_backend": "milvus",
            },
        )
        require(
            kb.get("vector_backend") == "milvus",
            f"{dataset}: knowledge base is not Milvus",
        )
        kb_id = kb["id"]
        self.knowledge_bases[dataset] = kb_id
        self.resources[f"{dataset}_knowledge_base_id"] = kb_id
        self.primary_kb_id = kb_id
        return kb_id

    def upload_dataset(self, prepared: Path, details: dict[str, Any]) -> list[str]:
        from contextlib import ExitStack

        require(self.primary_kb_id is not None, "knowledge base is missing")
        files = [prepared / name for name in details["files"]]
        document_ids: list[str] = []
        for start in range(0, len(files), 20):
            with ExitStack() as stack:
                multipart = [
                    (
                        "files",
                        (
                            path.name,
                            stack.enter_context(path.open("rb")),
                            "text/markdown",
                        ),
                    )
                    for path in files[start : start + 20]
                ]
                uploaded = self.json_request(
                    "POST",
                    f"{self.kb_root(self.primary_kb_id)}/documents",
                    label="upload-prepared-corpus",
                    files=multipart,
                )
            require(
                uploaded.get("uploaded") == len(multipart)
                and uploaded.get("failed") == 0,
                f"corpus upload failed: {uploaded!r}",
            )
            documents = [item["document"] for item in uploaded["items"]]
            document_ids.extend(document["id"] for document in documents)
        require(len(document_ids) == len(files), "uploaded document count mismatch")
        return document_ids

    def submit_and_wait(self, stage: str, ids: list[str]) -> list[str]:
        job_ids: list[str] = []
        for start in range(0, len(ids), 100):
            part = ids[start : start + 100]
            if stage == "parsing":
                jobs = self.submit_parsing(part, label="submit-parsing")
            else:
                jobs = self.submit_stage(stage, part, label=f"submit-{stage}")
            self.wait_for_stage(part, stage, jobs, label=f"wait-{stage}")
            job_ids.extend(jobs)
        return job_ids

    def verify_job_counts(self, job_ids: list[str], details: dict[str, Any]) -> None:
        require(self.primary_kb_id is not None, "knowledge base is missing")
        manifest = list(
            csv.DictReader(
                (
                    self.output
                    / "prepared"
                    / details["dataset"]
                    / "corpus_manifest.csv"
                ).open(encoding="utf-8")
            )
        )
        expected: dict[str, int] = defaultdict(int)
        for row in manifest:
            expected[row["file"]] += 1
        documents = self.list_documents(
            self.primary_kb_id, label="verify-chunk-documents"
        )
        by_id = {doc["id"]: doc["original_filename"] for doc in documents}
        jobs = {
            job["id"]: job
            for job in self.list_jobs(self.primary_kb_id, label="verify-chunk-jobs")
        }
        for job_id in job_ids:
            job = jobs[job_id]
            filename = by_id[job["document_id"]]
            require(
                job["progress"]["total"] == expected[filename],
                f"{filename}: expected {expected[filename]} chunks, got {job['progress']!r}",
            )

    def evaluate_dataset(
        self, dataset: str, prepared: Path, details: dict[str, Any]
    ) -> list[dict[str, Any]]:
        kb_id = self.create_knowledge_base(dataset)
        self.json_request(
            "PUT",
            f"{self.kb_root(kb_id)}/embedding-model",
            label="configure-embedding",
            json={
                "protocol": "openai",
                "base_url": self.config.embedding_base_url,
                "api_key": self.config.model_api_key,
                "model_name": self.config.embedding_model,
            },
        )
        ids = self.upload_dataset(prepared, details)
        self.submit_and_wait("parsing", ids)
        for document_id in ids:
            configured = self.json_request(
                "PUT",
                f"{self.kb_root(kb_id)}/documents/{document_id}/chunking-config",
                label="configure-fixed-chunking",
                json={"strategy": "fixed", "config": details["chunking_config"]},
            )
            require(
                configured.get("chunking_strategy") == "fixed",
                "fixed chunking was not stored",
            )
        chunk_jobs = self.submit_and_wait("chunking", ids)
        self.verify_job_counts(chunk_jobs, details)
        vector_jobs = self.submit_and_wait("vectorization", ids)
        self.verify_job_counts(vector_jobs, details)

        queries = json.loads((prepared / "queries.json").read_text(encoding="utf-8"))
        expected_corpus = {
            row["corpus_id"]
            for row in csv.DictReader(
                (prepared / "corpus_manifest.csv").open(encoding="utf-8")
            )
        }
        per_query: list[dict[int, dict[str, float]]] = []
        query_rows: list[dict[str, Any]] = []
        for query in queries:
            retrieved = self.json_request(
                "POST",
                f"{self.kb_root(kb_id)}/retrieve",
                label="retrieve-top-20",
                json={
                    "query": query["text"],
                    "mode": "vector",
                    "top_k": 20,
                    "candidate_k": 20,
                },
            )
            require(retrieved.get("mode") == "vector", "retrieval mode mismatch")
            ranked: list[str] = []
            for item in retrieved.get("items", []):
                heading = item.get("metadata", {}).get("heading", "")
                require(
                    isinstance(heading, str) and heading.startswith("corpus:"),
                    f"result missing corpus heading: {item!r}",
                )
                corpus_id = decode_id(heading.removeprefix("corpus:"))
                require(
                    corpus_id in expected_corpus,
                    f"result outside sampled corpus: {corpus_id}",
                )
                ranked.append(corpus_id)
            require(
                len(ranked) == len(set(ranked)),
                "retrieval returned duplicate corpus items",
            )
            relevant = set(query["relevant_corpus_ids"])
            require(
                relevant <= expected_corpus,
                "positive labels absent from sampled corpus",
            )
            scores = score_ranking(ranked, relevant)
            per_query.append(scores)
            query_rows.append(
                {
                    "query_id": query["query_id"],
                    "relevant_ids": sorted(relevant),
                    "ranked_ids": ranked,
                    "scores": scores,
                }
            )

        dataset_dir = self.output / dataset
        dataset_dir.mkdir(parents=True, exist_ok=True)
        with (dataset_dir / "queries.jsonl").open("w", encoding="utf-8") as handle:
            for row in query_rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        totals = aggregate_scores(per_query)
        rows = [
            {
                "dataset": dataset,
                "k": k,
                **totals[k],
                "queries": len(queries),
                "corpus": len(expected_corpus),
                "seed": details["seed"],
            }
            for k in K_VALUES
        ]
        write_csv(dataset_dir / "metrics.csv", rows)
        write_json(
            dataset_dir / "run.json",
            {
                "dataset": dataset,
                "knowledge_base_id": kb_id,
                "vector_backend": "milvus",
                "embedding_model": self.config.embedding_model,
                "embedding_base_url": self.config.embedding_base_url,
                "preparation": details,
            },
        )
        LOG.warning(
            "%s complete: %d queries, %d corpus items",
            dataset,
            len(queries),
            len(expected_corpus),
        )
        return rows

    def cleanup(self) -> list[str]:
        if self.config.keep_resources:
            return []
        errors: list[str] = []
        for dataset, kb_id in self.knowledge_bases.items():
            try:
                operation = self.json_request(
                    "DELETE",
                    self.kb_root(kb_id),
                    expected=202,
                    label=f"cleanup-{dataset}-knowledge-base",
                )
                self.wait_operation(
                    operation["operation_id"], label=f"cleanup-{dataset}"
                )
            except Exception as exc:
                errors.append(f"{dataset}: {exc}")
        if self.workspace_id:
            try:
                self.request(
                    "DELETE",
                    f"/workspaces/{self.workspace_id}",
                    expected=204,
                    label="cleanup-workspace",
                )
            except Exception as exc:
                errors.append(f"workspace: {exc}")
        return errors


def parse_args() -> tuple[Config, argparse.Namespace]:
    env = parse_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway",
        default=configured_value("RAG_TEST_GATEWAY", env) or gateway_from_env_file(),
    )
    parser.add_argument("--email", default=configured_value("RAG_TEST_EMAIL", env))
    parser.add_argument(
        "--password", default=configured_value("RAG_TEST_PASSWORD", env)
    )
    parser.add_argument(
        "--datasets-dir", type=Path, default=ROOT / "test/datasets/MTEB"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT
        / "tmp/rag-retrieval-eval"
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
    )
    parser.add_argument(
        "--embedding-base-url",
        default=configured_value("RAG_TEST_EMBEDDING_BASE_URL", env)
        or "http://127.0.0.1:31995/v1",
    )
    parser.add_argument(
        "--embedding-model",
        default=configured_value("RAG_TEST_EMBEDDING_MODEL", env)
        or "Qwen3-Embedding-8B",
    )
    parser.add_argument("--tokenizer-model", default="Qwen/Qwen3-Embedding-8B")
    parser.add_argument("--tokenizer-json", type=Path)
    parser.add_argument("--max-input-tokens", type=int)
    parser.add_argument(
        "--model-api-key",
        default=configured_value("RAG_TEST_MODEL_API_KEY", env) or "EMPTY",
    )
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--corpus", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--request-timeout-seconds", type=float, default=120)
    parser.add_argument("--job-timeout-seconds", type=float, default=7200)
    parser.add_argument("--poll-interval-seconds", type=float, default=2)
    parser.add_argument("--keep-resources", action="store_true")
    args = parser.parse_args()
    require(
        bool(args.email and args.password),
        "RAG_TEST_EMAIL and RAG_TEST_PASSWORD are required",
    )
    require(
        all(
            x > 0
            for x in (
                args.request_timeout_seconds,
                args.job_timeout_seconds,
                args.poll_interval_seconds,
                args.queries,
                args.corpus,
            )
        ),
        "timeouts and sample sizes must be positive",
    )
    config = Config(
        gateway=args.gateway.rstrip("/"),
        email=args.email,
        password=args.password,
        files_dir=args.datasets_dir.resolve(),
        embedding_base_url=args.embedding_base_url.rstrip("/"),
        embedding_model=args.embedding_model,
        llm_base_url="",
        llm_model="",
        model_api_key=args.model_api_key,
        vector_backend="milvus",
        request_timeout_seconds=args.request_timeout_seconds,
        job_timeout_seconds=args.job_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        keep_resources=args.keep_resources,
        report=None,
    )
    return config, args


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        config, args = parse_args()
        flow = Evaluation(config, args.output_dir.resolve())
        flow.output.mkdir(parents=True, exist_ok=True)
        error: Exception | None = None
        rows: list[dict[str, Any]] = []
        try:
            flow.login_and_validate_capabilities()
            limit = flow.effective_embedding_limit(args.max_input_tokens)
            tokenizer = load_tokenizer(args.tokenizer_model, args.tokenizer_json)
            flow.create_workspace()
            for dataset in DATASET_NAMES:
                prepared = flow.output / "prepared" / dataset
                details = prepare_dataset(
                    args.datasets_dir / dataset,
                    prepared,
                    tokenizer=tokenizer,
                    max_tokens=limit - 32,
                    query_count=args.queries,
                    corpus_count=args.corpus,
                    seed=args.seed,
                    tokenizer_name=args.tokenizer_model,
                )
                rows.extend(flow.evaluate_dataset(dataset, prepared, details))
        except Exception as exc:
            error = exc
        finally:
            cleanup_errors = flow.cleanup()
            flow.close()
        if error or cleanup_errors:
            write_json(
                flow.output / "failure.json",
                {
                    "error": str(error) if error else None,
                    "cleanup_errors": cleanup_errors,
                    "completed_datasets": sorted({row["dataset"] for row in rows}),
                },
            )
            raise FlowError(
                f"evaluation failed: {error}; cleanup errors: {cleanup_errors}"
            )
        macro = [
            {
                "dataset": "MACRO_AVERAGE",
                "k": k,
                **{
                    metric: mean(float(row[metric]) for row in rows if row["k"] == k)
                    for metric in ("recall", "precision", "hit_rate", "mrr")
                },
                "queries": sum(int(row["queries"]) for row in rows if row["k"] == k),
                "corpus": sum(int(row["corpus"]) for row in rows if row["k"] == k),
                "seed": args.seed,
            }
            for k in K_VALUES
        ]
        write_csv(flow.output / "summary.csv", rows + macro)
        print(f"PASS: {flow.output / 'summary.csv'}")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
