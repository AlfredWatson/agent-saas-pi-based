#!/usr/bin/env python3
"""Black-box acceptance for knowledge-graph construction from user documents.

Covers requirement 2: 基于用户已有文档构建领域知识图谱.

The script logs into a dedicated test account, creates an isolated Workspace and
Knowledge Base, uploads two Markdown documents, runs parsing → chunking, configures
a verified LLM, submits graph-extraction, and then validates the produced document
graphs, merges them, and performs a graph-mode retrieval.

Run after Gateway, the RAG worker, Redis, PostgreSQL and the LLM vLLM server are
ready::

    uv run python test/交付要求/knowledge_graph_flow.py --report /tmp/knowledge-graph-flow.json

Resources are cleaned up by default.  ``--keep-resources`` retains them.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

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
    log_graph_detail,
    log_graph_retrieve,
    log_graphs,
    log_knowledge_base,
    log_model_config,
    log_stage_completed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "test" / "files"
GRAPH_FIXTURES = ("markdown-test-1.md", "markdown-test-2.md")
GRAPH_QUERY = "Unity"


def upload_markdown(flow: UserFlow, kb_id: str, path: Path, label: str) -> dict:
    with path.open("rb") as handle:
        payload = flow.json_request(
            "POST",
            f"{flow.kb_root(kb_id)}/documents",
            label=label,
            files={"files": (path.name, handle, MIME_TYPES[path.suffix.lower()])},
        )
    documents = [
        item.get("document")
        for item in payload.get("items", [])
        if isinstance(item, dict) and item.get("status") == "uploaded"
    ]
    require(len(documents) == 1 and isinstance(documents[0], dict),
            f"{label} did not create exactly one document")
    return documents[0]


def configure_llm(flow: UserFlow, kb_id: str) -> dict:
    return flow.json_request(
        "PUT",
        f"{flow.kb_root(kb_id)}/llm-model",
        label="configure-llm-model",
        json={
            "protocol": "openai",
            "base_url": normalized_api_base(flow.config.llm_base_url),
            "api_key": flow.config.model_api_key,
            "model_name": flow.config.llm_model,
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

    # Upload two documents so we can extract two document graphs and merge them.
    documents: dict[str, str] = {}
    for name in GRAPH_FIXTURES:
        document = upload_markdown(flow, kb_id, FIXTURES / name, label=f"upload-{name}")
        log_document_uploaded(document)
        documents[name] = document["id"]

    document_ids = list(documents.values())

    # parsing -> chunking (fixed default, no embedding required).
    log(f"[解析 parsing] 提交 {len(document_ids)} 个文档进入解析队列")
    parsing_jobs = flow.submit_parsing(document_ids, label="submit-parsing")
    flow.wait_for_stage(document_ids, "parsing", parsing_jobs, label="wait-parsing")
    for document_id, job_id in zip(document_ids, parsing_jobs, strict=True):
        log_stage_completed(flow, kb_id, document_id, "parsing", [job_id], label="解析 parsing")
    log(f"[分段 chunking] 提交 {len(document_ids)} 个文档进入切分队列")
    chunking_jobs = flow.submit_stage("chunking", document_ids, label="submit-chunking")
    flow.wait_for_stage(document_ids, "chunking", chunking_jobs, label="wait-chunking")
    for document_id, job_id in zip(document_ids, chunking_jobs, strict=True):
        log_stage_completed(flow, kb_id, document_id, "chunking", [job_id], label="分段 chunking")

    # Graph extraction requires a verified LLM (structured graph output is probed).
    llm = configure_llm(flow, kb_id)
    require(llm.get("kind") == "llm", f"invalid LLM configuration response: {llm!r}")
    log_model_config(llm, kind="llm")

    log(f"[图谱提取 graph-extraction] 提交 {len(document_ids)} 个文档进入抽取队列")
    graph_jobs = flow.submit_stage(
        "graph-extraction", document_ids, label="submit-graph-extraction"
    )
    flow.wait_for_stage(document_ids, "graph", graph_jobs, label="wait-graph-extraction")
    for document_id, job_id in zip(document_ids, graph_jobs, strict=True):
        log_stage_completed(flow, kb_id, document_id, "graph", [job_id], label="图谱提取 graph")

    # Document graphs must be listed and ready.
    graphs = flow.list_graphs(kb_id, label="list-document-graphs")
    log(f"[图谱列表] 共 {len(graphs)} 个图谱 artifact")
    log_graphs(graphs)
    document_graphs = [
        graph
        for graph in graphs
        if graph.get("kind") == "document"
        and graph.get("source_document_id") in set(document_ids)
        and graph.get("status") == "ready"
    ]
    require(len(document_graphs) == 2,
            f"expected two ready document graphs, got {document_graphs!r}")

    # Graph detail must contain entities and relations.
    for graph in document_graphs:
        detail = flow.json_request(
            "GET", f"{flow.kb_root(kb_id)}/graphs/{graph['id']}", label="get-graph-detail"
        )
        require(isinstance(detail.get("nodes"), list) and detail.get("nodes"),
                f"graph detail lacks nodes: {detail!r}")
        require(isinstance(detail.get("edges"), list),
                f"graph detail lacks edges: {detail!r}")
        log(f"[图谱详情] graph_id={graph['id']} name={graph.get('name')!r}")
        log_graph_detail(detail)

    # Merge the two document graphs into one domain graph.
    merged = flow.json_request(
        "POST",
        f"{flow.kb_root(kb_id)}/graphs:merge",
        expected=201,
        label="merge-document-graphs",
        json={
            "name": f"delivery-merge-{uuid4().hex[:8]}",
            "graph_ids": [graph["id"] for graph in document_graphs],
        },
    )
    require(isinstance(merged.get("id"), str) and merged.get("kind") == "merged",
            f"graph merge failed: {merged!r}")
    log(f"[图谱合并] merged_id={merged.get('id')} name={merged.get('name')!r} kind={merged.get('kind')} source_graph_ids={merged.get('source_graph_ids')}")

    # Graph-mode retrieval must return nodes plus sourced evidence.
    result = flow.json_request(
        "POST",
        f"{flow.kb_root(kb_id)}/retrieve",
        label="retrieve-graph",
        json={"query": GRAPH_QUERY, "mode": "graph", "top_k": 5},
    )
    require(result.get("nodes") and result.get("evidence"),
            f"graph retrieval returned no nodes or evidence: {result!r}")
    require(
        all(item.get("document_id") in set(document_ids) for item in result["evidence"]),
        "graph retrieval evidence escaped the selected documents",
    )
    require("rerank" not in result,
            f"graph retrieval unexpectedly exposed rerank state: {result!r}")
    log_graph_retrieve(result)


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
        "--llm-base-url",
        default=configured_value("RAG_TEST_LLM_BASE_URL", dotenv)
        or "http://127.0.0.1:30018/v1",
    )
    parser.add_argument(
        "--llm-model",
        default=configured_value("RAG_TEST_LLM_MODEL", dotenv) or "Qwen3.8-27B-FP8",
    )
    parser.add_argument(
        "--model-api-key",
        default=configured_value("RAG_TEST_MODEL_API_KEY", dotenv) or "EMPTY",
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
    for value in (args.request_timeout_seconds, args.job_timeout_seconds, args.poll_interval_seconds):
        require(value > 0, "timeouts and poll interval must be positive")

    return RagConfig(
        gateway=normalized_api_base(args.gateway),
        email=args.email,
        password=args.password,
        files_dir=FIXTURES,
        embedding_base_url=normalized_api_base(
            configured_value("RAG_TEST_EMBEDDING_BASE_URL", dotenv)
            or "http://127.0.0.1:31995/v1"
        ),
        embedding_model=configured_value("RAG_TEST_EMBEDDING_MODEL", dotenv)
        or "Qwen3-Embedding-8B",
        llm_base_url=normalized_api_base(args.llm_base_url),
        llm_model=args.llm_model,
        model_api_key=args.model_api_key,
        vector_backend=args.vector_backend,
        request_timeout_seconds=args.request_timeout_seconds,
        job_timeout_seconds=args.job_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        keep_resources=args.keep_resources,
        report=args.report.resolve() if args.report else None,
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
    print("PASS: knowledge graph construction (extraction, merge, graph retrieval) completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
