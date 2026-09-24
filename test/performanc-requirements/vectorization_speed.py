#!/usr/bin/env python3
# ruff: noqa: E402
"""Measure one-document-at-a-time RAG vectorization through public HTTP APIs.

Run Gateway, RAG worker, PostgreSQL, Redis, and the embedding server first::

    uv run python test/performanc-requirements/vectorization_speed.py

The CSV is written to /tmp by default. Created resources are deleted unless
--keep-resources is given. Timing starts immediately before submitting each
vectorization job and ends when both the job and document report success.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from uuid import uuid4

import tiktoken

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "test"))
sys.path.insert(0, str(ROOT / "gateway"))

from app.integrations.rag.processors import (
    DefaultDocumentProcessor,
    SUPPORTED_EXTENSIONS,
)
from flow_env import configured_value, parse_dotenv
from rag_user_flow import (
    Config,
    FlowError,
    MIME_TYPES,
    STATIC_BACKENDS,
    UserFlow,
    gateway_from_env_file,
    require,
)

LOG = logging.getLogger("vectorization-speed")
ENCODING = tiktoken.get_encoding("cl100k_base")


class Benchmark(UserFlow):
    def __init__(self, config: Config):
        super().__init__(config)
        self.token_counts: dict[str, int] = {}
        self.skipped: list[tuple[str, str]] = []
        self.rows: list[dict[str, object]] = []
        self.embedding_dimension: int | None = None

    def fixtures(self) -> list[Path]:
        require(
            self.config.files_dir.is_dir(),
            f"files directory is missing: {self.config.files_dir}",
        )
        files = sorted(
            path
            for path in self.config.files_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        require(files, f"no supported documents in {self.config.files_dir}")
        names = [path.name for path in files]
        require(
            len(names) == len(set(names)),
            "fixture basenames must be unique for upload mapping",
        )
        return files

    def count_tokens(self, files: list[Path]) -> list[Path]:
        """Count the same parsed blocks that the default worker will create."""
        parser = DefaultDocumentProcessor()
        accepted: list[Path] = []
        for path in files:
            try:
                blocks = parser.parse(path.name, path.read_bytes())
                count = sum(
                    len(ENCODING.encode(block.page_content)) for block in blocks
                )
            except Exception as exc:
                raise FlowError(
                    f"cannot count parsed tokens for {path}: {exc}"
                ) from exc
            if count == 0:
                self.skipped.append((path.name, "no extractable text"))
            else:
                self.token_counts[path.name] = count
                accepted.append(path)
        require(accepted, "all uploaded documents have zero extractable tokens")
        return accepted

    def create_workspace_and_knowledge_base(self) -> None:
        suffix = uuid4().hex[:10]
        workspace = self.json_request(
            "POST",
            "/workspaces",
            expected=201,
            label="create-workspace",
            json={"name": f"vector-perf-{suffix}"},
        )
        self.workspace_id = workspace["id"]
        self.resources["workspace_id"] = self.workspace_id
        switched = self.json_request(
            "POST", f"/workspaces/{self.workspace_id}:switch", label="switch-workspace"
        )
        require(switched.get("is_current") is True, "workspace switch failed")
        kb = self.json_request(
            "POST",
            f"/workspaces/{self.workspace_id}/knowledge-bases",
            expected=201,
            label="create-knowledge-base",
            json={
                "name": f"vector-perf-{suffix}",
                **STATIC_BACKENDS,
                "vector_backend": self.config.vector_backend,
            },
        )
        self.primary_kb_id = kb["id"]
        self.resources["knowledge_base_id"] = self.primary_kb_id

    def upload(self, paths: list[Path]) -> None:
        from contextlib import ExitStack

        require(self.primary_kb_id is not None, "knowledge base is missing")
        with ExitStack() as stack:
            files = [
                (
                    "files",
                    (
                        path.name,
                        stack.enter_context(path.open("rb")),
                        MIME_TYPES.get(path.suffix.lower(), "text/markdown"),
                    ),
                )
                for path in paths
            ]
            result = self.json_request(
                "POST",
                f"{self.kb_root(self.primary_kb_id)}/documents",
                label="upload-documents",
                files=files,
            )
        require(
            result.get("uploaded") == len(paths) and result.get("failed") == 0,
            f"upload failed: {result!r}",
        )
        documents = [item.get("document") for item in result.get("items", [])]
        require(
            len(documents) == len(paths)
            and all(isinstance(doc, dict) for doc in documents),
            "upload returned incomplete documents",
        )
        self.primary_documents = {doc["original_filename"]: doc for doc in documents}
        require(
            set(self.primary_documents) == {path.name for path in paths},
            "uploaded document names differ from selected files",
        )

    def run_benchmark(self) -> None:
        all_paths = self.fixtures()
        self.login_and_validate_capabilities()
        self.preflight_model(
            "embedding", self.config.embedding_base_url, self.config.embedding_model
        )
        self.create_workspace_and_knowledge_base()
        self.upload(all_paths)
        paths = self.count_tokens(all_paths)
        document_ids = [self.primary_documents[path.name]["id"] for path in paths]
        parse_jobs = self.submit_parsing(document_ids, label="submit-parsing")
        self.wait_for_stage(document_ids, "parsing", parse_jobs, label="wait-parsing")

        require(self.primary_kb_id is not None, "knowledge base is missing")
        embedding = self.json_request(
            "PUT",
            f"{self.kb_root(self.primary_kb_id)}/embedding-model",
            label="configure-embedding",
            json={
                "protocol": "openai",
                "base_url": self.config.embedding_base_url,
                "api_key": self.config.model_api_key,
                "model_name": self.config.embedding_model,
            },
        )
        dimension = embedding.get("embedding_dimension")
        require(
            isinstance(dimension, int) and dimension > 0,
            f"invalid embedding dimension: {dimension!r}",
        )
        self.embedding_dimension = dimension
        fixed = self.capabilities["chunking_strategies"]["fixed"]
        for document_id in document_ids:
            self.json_request(
                "PUT",
                f"{self.kb_root(self.primary_kb_id)}/documents/{document_id}/chunking-config",
                label="configure-fixed-chunking",
                json={"strategy": "fixed", "config": fixed},
            )
        chunk_jobs = self.submit_stage(
            "chunking", document_ids, label="submit-chunking"
        )
        self.wait_for_stage(document_ids, "chunking", chunk_jobs, label="wait-chunking")

        for path in paths:
            document_id = self.primary_documents[path.name]["id"]
            started = time.perf_counter()
            jobs = self.submit_stage(
                "vectorization",
                [document_id],
                label=f"submit-vectorization-{path.name}",
            )
            self.wait_for_stage(
                [document_id],
                "vectorization",
                jobs,
                label=f"wait-vectorization-{path.name}",
            )
            seconds = time.perf_counter() - started
            row: dict[str, object] = {
                "document": path.name,
                "embedding_protocol": "openai",
                "embedding_model": self.config.embedding_model,
                "embedding_base_url": self.config.embedding_base_url,
                "embedding_dimension": dimension,
                "vector_backend": self.config.vector_backend,
                "tokenizer": "cl100k_base",
                "tokens": self.token_counts[path.name],
                "seconds": round(seconds, 3),
                "tokens_per_second": round(self.token_counts[path.name] / seconds, 2),
            }
            self.rows.append(row)
            LOG.info("%s: %s tokens, %.3f s", path.name, row["tokens"], seconds)


def write_table(path: Path, flow: Benchmark) -> None:
    require(flow.rows, "no completed vectorization results")
    fields = list(flow.rows[0])
    seconds = [float(row["seconds"]) for row in flow.rows]
    total_tokens = sum(int(row["tokens"]) for row in flow.rows)
    summary = {
        **flow.rows[0],
        "document": "AVERAGE",
        "tokens": round(total_tokens / len(flow.rows), 2),
        "seconds": round(mean(seconds), 3),
        "tokens_per_second": round(total_tokens / sum(seconds), 2),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(flow.rows)
        writer.writerow(summary)


def parse_args() -> tuple[Config, Path]:
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
    parser.add_argument("--files-dir", type=Path, default=ROOT / "test" / "files")
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
    parser.add_argument(
        "--model-api-key",
        default=configured_value("RAG_TEST_MODEL_API_KEY", env) or "EMPTY",
    )
    parser.add_argument(
        "--vector-backend",
        choices=("postgresql", "milvus", "chroma", "qdrant"),
        default=configured_value("RAG_TEST_VECTOR_BACKEND", env) or "postgresql",
    )
    parser.add_argument("--request-timeout-seconds", type=float, default=120)
    parser.add_argument("--job-timeout-seconds", type=float, default=1800)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.5)
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp")
        / f"vectorization-speed-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.csv",
    )
    args = parser.parse_args()
    require(
        bool(args.email and args.password),
        "RAG_TEST_EMAIL and RAG_TEST_PASSWORD are required",
    )
    require(
        all(
            value > 0
            for value in (
                args.request_timeout_seconds,
                args.job_timeout_seconds,
                args.poll_interval_seconds,
            )
        ),
        "timeouts and poll interval must be positive",
    )
    output = args.output.resolve()
    require(output.is_relative_to(Path("/tmp")), "output must be under /tmp")
    config = Config(
        gateway=args.gateway.rstrip("/"),
        email=args.email,
        password=args.password,
        files_dir=args.files_dir.resolve(),
        embedding_base_url=args.embedding_base_url.rstrip("/"),
        embedding_model=args.embedding_model,
        llm_base_url="",
        llm_model="",
        model_api_key=args.model_api_key,
        vector_backend=args.vector_backend,
        request_timeout_seconds=args.request_timeout_seconds,
        job_timeout_seconds=args.job_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        keep_resources=args.keep_resources,
        report=None,
    )
    return config, output


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        config, output = parse_args()
        flow = Benchmark(config)
        failure: Exception | None = None
        cleanup_errors: list[str] = []
        try:
            flow.run_benchmark()
        except Exception as exc:
            failure = exc
        finally:
            try:
                cleanup_errors = flow.cleanup()
            finally:
                flow.close()
        if failure:
            raise failure
        require(not cleanup_errors, f"cleanup failed: {cleanup_errors}")
        write_table(output, flow)
        print(f"CSV: {output}")
        for row in flow.rows:
            print(f"{row['document']}: {row['tokens']} tokens, {row['seconds']} s")
        print(
            f"AVERAGE: {mean(float(row['seconds']) for row in flow.rows):.3f} s/document"
        )
        for name, reason in flow.skipped:
            print(f"SKIPPED: {name} ({reason})")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
