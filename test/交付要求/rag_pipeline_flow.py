#!/usr/bin/env python3
"""Black-box acceptance for the end-to-end RAG processing pipeline.

Covers requirement 1: 文档解析 → 分段 → 向量化 → 存储 → 召回 → 重排.

The script only calls the public Gateway API.  It logs into a dedicated test
account, creates an isolated Workspace and Knowledge Base, uploads a Markdown
document, then drives the four processing stages (parsing → chunking →
vectorization) before retrieving results and, when a reranker is configured,
verifying that rerank is applied.

Run after Gateway, the RAG worker, Redis, PostgreSQL and the embedding (and
optional reranker) services are ready::

    uv run python test/交付要求/rag_pipeline_flow.py --report /tmp/rag-pipeline-flow.json

Resources are cleaned up by default.  ``--keep-resources`` retains them.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

# The delivery scripts live one level below the shared acceptance helpers.
TEST_DIR = Path(__file__).resolve().parents[1]
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from flow_env import configured_value, parse_dotenv
from rag_user_flow import (
    Config as RagConfig,
    FlowError,
    MIME_TYPES,
    UserFlow,
    gateway_from_env_file,
    normalized_api_base,
    require,
)
from _delivery_log import (
    log,
    log_capabilities,
    log_document_uploaded,
    log_knowledge_base,
    log_model_config,
    log_retrieve,
    log_stage_completed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "test" / "files"
PIPELINE_FIXTURE = "markdown-test-1.md"
RETRIEVAL_QUERY = "Unity Codely Agent 前期推进计划"


def upload_markdown(flow: UserFlow, kb_id: str, path: Path) -> dict:
    with path.open("rb") as handle:
        payload = flow.json_request(
            "POST",
            f"{flow.kb_root(kb_id)}/documents",
            label="upload-pipeline-document",
            files={"files": (path.name, handle, MIME_TYPES[path.suffix.lower()])},
        )
    require(payload.get("uploaded") == 1 and payload.get("failed") == 0,
            f"upload result was unexpected: {payload!r}")
    documents = [
        item.get("document")
        for item in payload.get("items", [])
        if isinstance(item, dict) and item.get("status") == "uploaded"
    ]
    require(len(documents) == 1 and isinstance(documents[0], dict),
            "pipeline upload did not create exactly one document")
    return documents[0]


def configure_embedding(flow: UserFlow, kb_id: str) -> dict:
    return flow.json_request(
        "PUT",
        f"{flow.kb_root(kb_id)}/embedding-model",
        label="configure-embedding-model",
        json={
            "protocol": "openai",
            "base_url": normalized_api_base(flow.config.embedding_base_url),
            "api_key": flow.config.model_api_key,
            "model_name": flow.config.embedding_model,
        },
    )


def configure_reranker(flow: UserFlow, kb_id: str) -> dict:
    require(
        flow.config.reranker_model is not None
        and flow.config.reranker_api_key is not None,
        "reranker configuration is incomplete",
    )
    return flow.json_request(
        "PUT",
        f"{flow.kb_root(kb_id)}/reranker-model",
        label="configure-reranker-model",
        json={
            "protocol": "vllm",
            "base_url": flow.config.reranker_base_url,
            "api_key": flow.config.reranker_api_key,
            "model_name": flow.config.reranker_model,
        },
    )


def run(flow: UserFlow) -> None:
    flow.login_and_validate_capabilities()
    log(f"[登录] user_id={flow.user_id}")
    log_capabilities(flow.capabilities)

    flow.create_workspace_and_knowledge_bases()
    require(flow.primary_kb_id is not None and flow.workspace_id is not None,
            "RAG setup did not create a primary knowledge base")
    kb_id = flow.primary_kb_id
    log(f"[工作区] id={flow.workspace_id}")
    log_knowledge_base(flow.json_request("GET", flow.kb_root(kb_id), label="log-knowledge-base"))

    # 1) Upload: persists the file and document metadata, starts no jobs.
    document = upload_markdown(flow, kb_id, FIXTURES / PIPELINE_FIXTURE)
    log_document_uploaded(document)
    document_id = document["id"]
    require(isinstance(document_id, str), "pipeline document has no id")
    require(document.get("storage_backend") == "postgresql",
            "file storage backend is not postgresql")
    require(
        all(document.get("stages", {}).get(stage, {}).get("status") == "not_started"
            for stage in ("parsing", "chunking", "vectorization", "graph")),
        "upload unexpectedly started processing",
    )

    # 2) Parsing: extracts blocks into PostgreSQL.
    log(f"[解析 parsing] 提交 {PIPELINE_FIXTURE!r} 进入解析队列")
    parsing_jobs = flow.submit_parsing([document_id], label="submit-parsing")
    flow.wait_for_stage([document_id], "parsing", parsing_jobs, label="wait-parsing")
    log_stage_completed(flow, kb_id, document_id, "parsing", parsing_jobs, label="解析 parsing")

    # 3) Chunking: blocks -> chunks using the document-level (default fixed) strategy.
    embedding = configure_embedding(flow, kb_id)
    log_model_config(embedding, kind="embedding")
    log(f"[分段 chunking] 使用文档级策略 fixed 提交切分队列")
    chunking_jobs = flow.submit_stage("chunking", [document_id], label="submit-chunking")
    flow.wait_for_stage([document_id], "chunking", chunking_jobs, label="wait-chunking")
    log_stage_completed(flow, kb_id, document_id, "chunking", chunking_jobs, label="分段 chunking")

    # 4) Vectorization: chunks -> vectors in the selected vector backend.
    log(f"[向量化 vectorization] 提交向量化队列 (backend={flow.config.vector_backend})")
    vectorization_jobs = flow.submit_stage(
        "vectorization", [document_id], label="submit-vectorization"
    )
    flow.wait_for_stage(
        [document_id], "vectorization", vectorization_jobs, label="wait-vectorization"
    )
    log_stage_completed(flow, kb_id, document_id, "vectorization", vectorization_jobs, label="向量化 vectorization")

    # 5) Optional reranker, applied automatically by vector/hybrid retrieval.
    if flow.reranker_enabled:
        reranker = configure_reranker(flow, kb_id)
        log_model_config(reranker, kind="reranker")
    else:
        log("[重排 reranker] 未配置，retrieve 返回候选阶段原排序")

    # 6) Recall (vector + hybrid) with rerank status.
    for mode in ("vector", "hybrid"):
        result = flow.json_request(
            "POST",
            f"{flow.kb_root(kb_id)}/retrieve",
            label=f"retrieve-{mode}",
            json={"query": RETRIEVAL_QUERY, "mode": mode, "top_k": 5},
        )
        require(result.get("mode") == mode, f"{mode} retrieval reported wrong mode")
        items = result.get("items", [])
        require(isinstance(items, list) and items, f"{mode} retrieval returned no items")
        for item in items:
            require(item.get("document_id") == document_id,
                    f"{mode} retrieval returned an unexpected document")
            score = item.get("score")
            require(isinstance(score, (int, float)) and math.isfinite(score),
                    f"{mode} retrieval has invalid score: {item!r}")
            if flow.reranker_enabled:
                require(isinstance(item.get("retrieval_score"), (int, float)),
                        f"{mode} reranked item lost its retrieval_score")
        expected_rerank = (
            {"configured": True, "applied": True, "error": None}
            if flow.reranker_enabled
            else {"configured": False, "applied": False, "error": None}
        )
        require(result.get("rerank") == expected_rerank,
                f"{mode} rerank state was unexpected: {result!r}")
        log_retrieve(result, mode=mode)

    # 7) Final stage state: parsing/chunking/vectorization succeeded, graph untouched.
    detail = flow.document_detail(document_id, label="verify-final-stages")
    for stage in ("parsing", "chunking", "vectorization"):
        require(detail["stages"][stage]["status"] == "succeeded",
                f"{stage} did not succeed: {detail!r}")
    require(detail["stages"]["graph"]["status"] == "not_started",
            "graph stage should remain untouched for the RAG pipeline flow")


def parse_args() -> RagConfig:
    dotenv = parse_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway",
        default=configured_value("RAG_TEST_GATEWAY", dotenv) or gateway_from_env_file(),
    )
    parser.add_argument("--email", default=configured_value("RAG_TEST_EMAIL", dotenv))
    parser.add_argument("--password", default=configured_value("RAG_TEST_PASSWORD", dotenv))
    parser.add_argument(
        "--embedding-base-url",
        default=configured_value("RAG_TEST_EMBEDDING_BASE_URL", dotenv)
        or "http://127.0.0.1:31995/v1",
    )
    parser.add_argument(
        "--embedding-model",
        default=configured_value("RAG_TEST_EMBEDDING_MODEL", dotenv)
        or "Qwen3-Embedding-8B",
    )
    parser.add_argument(
        "--model-api-key",
        default=configured_value("RAG_TEST_MODEL_API_KEY", dotenv) or "EMPTY",
    )
    parser.add_argument(
        "--reranker-base-url", default=configured_value("RAG_TEST_RERANKER_BASE_URL", dotenv)
    )
    parser.add_argument(
        "--reranker-model", default=configured_value("RAG_TEST_RERANKER_MODEL", dotenv)
    )
    parser.add_argument(
        "--reranker-api-key", default=configured_value("RAG_TEST_RERANKER_API_KEY", dotenv)
    )
    parser.add_argument(
        "--vector-backend",
        choices=("postgresql", "milvus", "chroma", "qdrant"),
        default=configured_value("RAG_TEST_VECTOR_BACKEND", dotenv) or "postgresql",
    )
    parser.add_argument("--request-timeout-seconds", type=float, default=120)
    parser.add_argument("--job-timeout-seconds", type=float, default=1_800)
    parser.add_argument("--poll-interval-seconds", type=float, default=2)
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    require(bool(args.email), "required test configuration is missing: RAG_TEST_EMAIL")
    require(bool(args.password), "required test configuration is missing: RAG_TEST_PASSWORD")
    reranker_values = (args.reranker_base_url, args.reranker_model, args.reranker_api_key)
    require(all(reranker_values) or not any(reranker_values),
            "reranker base URL, model, and API key must be configured together")
    for value in (args.request_timeout_seconds, args.job_timeout_seconds, args.poll_interval_seconds):
        require(value > 0, "timeouts and poll interval must be positive")

    return RagConfig(
        gateway=normalized_api_base(args.gateway),
        email=args.email,
        password=args.password,
        files_dir=FIXTURES,
        embedding_base_url=normalized_api_base(args.embedding_base_url),
        embedding_model=args.embedding_model,
        llm_base_url=normalized_api_base(
            configured_value("RAG_TEST_LLM_BASE_URL", dotenv) or "http://127.0.0.1:30018/v1"
        ),
        llm_model=configured_value("RAG_TEST_LLM_MODEL", dotenv) or "Qwen3.8-27B-FP8",
        model_api_key=args.model_api_key,
        vector_backend=args.vector_backend,
        request_timeout_seconds=args.request_timeout_seconds,
        job_timeout_seconds=args.job_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        keep_resources=args.keep_resources,
        report=args.report.resolve() if args.report else None,
        reranker_base_url=normalized_api_base(args.reranker_base_url) if args.reranker_base_url else None,
        reranker_model=args.reranker_model,
        reranker_api_key=args.reranker_api_key,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    try:
        config = parse_args()
    except FlowError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    flow = UserFlow(config)
    failure: Exception | None = None
    cleanup_errors: list[str] = []
    try:
        run(flow)
    except Exception as exc:
        failure = exc
    finally:
        try:
            cleanup_errors = flow.cleanup()
        finally:
            status = "passed" if failure is None and not cleanup_errors else "failed"
            report = flow.report(status, failure, cleanup_errors)
            if config.report:
                config.report.parent.mkdir(parents=True, exist_ok=True)
                config.report.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                print(f"Report: {config.report}")
            flow.close()
    if failure is not None:
        print(f"FAIL: {failure}", file=sys.stderr)
        if flow.resources:
            print(f"Created resources: {json.dumps(flow.resources, ensure_ascii=False)}",
                  file=sys.stderr)
        return 1
    if cleanup_errors:
        print("FAIL: cleanup did not complete:", file=sys.stderr)
        for error in cleanup_errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("PASS: RAG pipeline (parsing/chunking/vectorization/recall/rerank) completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
