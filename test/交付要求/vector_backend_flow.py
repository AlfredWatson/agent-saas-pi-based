#!/usr/bin/env python3
"""Black-box acceptance for unified vector-database management.

Covers requirement 5: 向量数据库统一纳管，兼容 Milvus / Chroma / Qdrant.

The script logs into a dedicated test account, discovers which vector backends are
enabled for the deployment via ``GET /rag/capabilities``, then for every enabled
backend creates a knowledge base bound to that backend and runs the minimal
pipeline (upload → parsing → chunking → embedding → vectorization → retrieval).
This proves the same public API drives PostgreSQL, Milvus, Chroma and Qdrant
without client-side differences.

Run after Gateway, the RAG worker, Redis, PostgreSQL, the embedding server, and
every external vector service you want to exercise are ready::

    uv run python test/交付要求/vector_backend_flow.py --report /tmp/vector-backend-flow.json

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
    log_knowledge_base,
    log_model_config,
    log_retrieve,
    log_stage_completed,
    log_workspace,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "test" / "files"
BACKEND_FIXTURE = "markdown-test-1.md"
RETRIEVAL_QUERY = "Unity Codely Agent 前期推进计划"
STATIC_BACKENDS = {
    "file_backend": "postgresql",
    "block_backend": "postgresql",
    "chunk_backend": "postgresql",
    "graph_backend": "postgresql",
}


def create_workspace(flow: UserFlow) -> str:
    suffix = uuid4().hex[:10]
    workspace = flow.json_request(
        "POST", "/workspaces", expected=201, label="create-workspace",
        json={"name": f"vec-e2e-{suffix}"},
    )
    workspace_id = workspace.get("id")
    require(isinstance(workspace_id, str), "workspace creation did not return id")
    flow.workspace_id = workspace_id
    flow.resources["workspace_id"] = workspace_id
    log_workspace(workspace, label="工作区")
    switched = flow.json_request(
        "POST", f"/workspaces/{workspace_id}:switch", label="switch-workspace"
    )
    require(switched.get("is_current") is True, "workspace switch failed")
    return workspace_id


def create_knowledge_base(flow: UserFlow, workspace_id: str, backend: str) -> str:
    knowledge_base = flow.json_request(
        "POST",
        f"/workspaces/{workspace_id}/knowledge-bases",
        expected=201,
        label=f"create-knowledge-base-{backend}",
        json={"name": f"vec-{backend}-{uuid4().hex[:10]}", "vector_backend": backend,
              **STATIC_BACKENDS},
    )
    kb_id = knowledge_base.get("id")
    require(isinstance(kb_id, str), f"{backend} knowledge base did not return id")
    require(knowledge_base.get("vector_backend") == backend,
            f"{backend} knowledge base did not retain its vector backend")
    log_knowledge_base(knowledge_base)
    return kb_id


def process_backend(flow: UserFlow, workspace_id: str, backend: str) -> str:
    kb_id = create_knowledge_base(flow, workspace_id, backend)
    flow.primary_kb_id = kb_id  # stage helpers operate on the primary knowledge base.
    flow.resources[f"knowledge_base_{backend}"] = kb_id

    with (FIXTURES / BACKEND_FIXTURE).open("rb") as handle:
        payload = flow.json_request(
            "POST",
            f"{flow.kb_root(kb_id)}/documents",
            label=f"upload-{backend}",
            files={"files": (BACKEND_FIXTURE, handle, MIME_TYPES[".md"])},
        )
    documents = [
        item.get("document")
        for item in payload.get("items", [])
        if isinstance(item, dict) and item.get("status") == "uploaded"
    ]
    require(len(documents) == 1 and isinstance(documents[0], dict),
            f"{backend} upload did not create one document")
    document_id = documents[0]["id"]
    log_document_uploaded(documents[0])

    log(f"[{backend}] 解析 parsing 提交")
    parsing = flow.submit_parsing([document_id], label=f"submit-parsing-{backend}")
    flow.wait_for_stage([document_id], "parsing", parsing, label=f"wait-parsing-{backend}")
    log_stage_completed(flow, kb_id, document_id, "parsing", parsing, label=f"[{backend}] 解析 parsing")

    embedding = flow.json_request(
        "PUT",
        f"{flow.kb_root(kb_id)}/embedding-model",
        label=f"configure-embedding-{backend}",
        json={
            "protocol": "openai",
            "base_url": normalized_api_base(flow.config.embedding_base_url),
            "api_key": flow.config.model_api_key,
            "model_name": flow.config.embedding_model,
        },
    )
    log_model_config(embedding, kind="embedding")
    log(f"[{backend}] 分段 chunking 提交")
    chunking = flow.submit_stage("chunking", [document_id], label=f"submit-chunking-{backend}")
    flow.wait_for_stage([document_id], "chunking", chunking, label=f"wait-chunking-{backend}")
    log_stage_completed(flow, kb_id, document_id, "chunking", chunking, label=f"[{backend}] 分段 chunking")
    log(f"[{backend}] 向量化 vectorization 提交")
    vectorization = flow.submit_stage(
        "vectorization", [document_id], label=f"submit-vectorization-{backend}"
    )
    flow.wait_for_stage(
        [document_id], "vectorization", vectorization, label=f"wait-vectorization-{backend}"
    )
    log_stage_completed(flow, kb_id, document_id, "vectorization", vectorization, label=f"[{backend}] 向量化 vectorization")

    result = flow.json_request(
        "POST",
        f"{flow.kb_root(kb_id)}/retrieve",
        label=f"retrieve-{backend}",
        json={"query": RETRIEVAL_QUERY, "mode": "vector", "top_k": 5},
    )
    items = result.get("items", [])
    require(isinstance(items, list) and items,
            f"{backend} retrieval returned no items")
    require(all(item.get("document_id") == document_id for item in items),
            f"{backend} retrieval returned an unexpected document")
    log_retrieve(result, mode=f"vector/{backend}")
    return kb_id


def run(flow: UserFlow) -> None:
    flow.login_and_validate_capabilities()
    log(f"[登录] user_id={flow.user_id}")
    log_capabilities(flow.capabilities)
    workspace_id = create_workspace(flow)
    enabled = flow.capabilities.get("vector_backends", [])
    require(isinstance(enabled, list) and "postgresql" in enabled,
            f"capabilities must enable at least postgresql: {enabled!r}")
    log(f"[向量后端] 当前部署启用的后端：{enabled}")

    tested: list[str] = []
    for backend in enabled:
        log(f"[向量后端] 开始测试 {backend!r}")
        process_backend(flow, workspace_id, backend)
        tested.append(backend)
        log(f"[向量后端] 完成测试 {backend!r}")

    flow.resources["vector_backends_tested"] = tested
    external = [backend for backend in tested if backend != "postgresql"]
    if external:
        print(f"PASS: vector backends exercised: {', '.join(tested)}")
    else:
        print("PASS: only postgresql vector backend is enabled; external backends not exercised")


def cleanup_knowledge_bases(flow: UserFlow, workspace_id: str, kb_ids: list[str]) -> list[str]:
    errors: list[str] = []
    for kb_id in kb_ids:
        try:
            payload = flow.json_request(
                "DELETE", f"{flow.kb_root(kb_id)}", expected=202,
                label=f"cleanup-delete-knowledge-base-{kb_id}",
            )
            operation_id = payload.get("operation_id")
            require(isinstance(operation_id, str),
                    "knowledge base deletion returned no operation id")
            flow.wait_operation(operation_id, label=f"cleanup-delete-kb-{kb_id}")
        except Exception as exc:
            errors.append(f"knowledge base {kb_id}: {exc}")
    if workspace_id:
        try:
            flow.request(
                "DELETE", f"/workspaces/{workspace_id}", expected=204,
                label="cleanup-delete-workspace",
            )
        except Exception as exc:
            errors.append(f"workspace {workspace_id}: {exc}")
    if errors:
        flow.event("cleanup-failed", errors=errors, resources=flow.resources)
    else:
        flow.event("cleanup-completed", resources=flow.resources)
    return errors


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
        embedding_base_url=normalized_api_base(args.embedding_base_url),
        embedding_model=args.embedding_model,
        llm_base_url=normalized_api_base(
            configured_value("RAG_TEST_LLM_BASE_URL", dotenv) or "http://127.0.0.1:30018/v1"
        ),
        llm_model=configured_value("RAG_TEST_LLM_MODEL", dotenv) or "Qwen3.8-27B-FP8",
        model_api_key=args.model_api_key,
        vector_backend="postgresql",  # capabilities validation only; every enabled backend is tested.
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
    kb_ids: list[str] = []
    try:
        run(flow)
        if not config.keep_resources:
            kb_ids = [
                value
                for key, value in flow.resources.items()
                if key.startswith("knowledge_base_") and isinstance(value, str)
            ]
    except Exception as exc:
        failure = exc
        if not config.keep_resources:
            kb_ids = [
                value
                for key, value in flow.resources.items()
                if key.startswith("knowledge_base_") and isinstance(value, str)
            ]
    finally:
        try:
            if not config.keep_resources and flow.workspace_id:
                cleanup_errors = cleanup_knowledge_bases(
                    flow, flow.workspace_id, kb_ids
                )
            elif config.keep_resources:
                flow.event("cleanup-skipped", resources=flow.resources)
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
    print("PASS: unified vector backend management completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
