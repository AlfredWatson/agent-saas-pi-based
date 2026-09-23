#!/usr/bin/env python3
"""Black-box acceptance for custom document chunking strategies.

Covers requirement 3: 自定义文本切分策略 (fixed / regex / semantic).

The script logs into a dedicated test account, creates an isolated Workspace and
Knowledge Base, uploads three identical Markdown documents, assigns each one a
different document-level chunking strategy, then runs parsing → chunking →
vectorization and retrieves the results.  It also verifies that an invalid regex
is rejected with ``422 invalid_chunking_config``.

Semantic chunking requires a verified embedding model before the chunking job is
submitted, so the embedding model is configured after parsing and before chunking.

Run after Gateway, the RAG worker, Redis, PostgreSQL and the embedding server are
ready::

    uv run python test/交付要求/chunking_strategy_flow.py --report /tmp/chunking-strategy-flow.json

Resources are cleaned up by default.  ``--keep-resources`` retains them.
"""

from __future__ import annotations

import argparse
import json
import logging
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
CHUNKING_FIXTURE = "markdown-test-2.md"
RETRIEVAL_QUERY = "codely unity tools"


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


def set_chunking_config(
    flow: UserFlow, kb_id: str, document_id: str, strategy: str, config: dict, label: str
) -> dict:
    return flow.json_request(
        "PUT",
        f"{flow.kb_root(kb_id)}/documents/{document_id}/chunking-config",
        label=label,
        json={"strategy": strategy, "config": config},
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

    strategies = flow.capabilities["chunking_strategies"]
    require(
        set(strategies) == {"fixed", "regex", "semantic"},
        f"unexpected chunking strategies: {strategies!r}",
    )
    log(f"[切分策略] 从 capabilities 获得默认配置: {strategies}")

    # Upload three identical documents, one per strategy (duplicate uploads are allowed).
    documents: dict[str, dict] = {}
    for strategy in ("fixed", "regex", "semantic"):
        document = upload_markdown(flow, kb_id, FIXTURES / CHUNKING_FIXTURE,
                                   label=f"upload-{strategy}")
        log_document_uploaded(document)
        documents[strategy] = document
    document_ids = [document["id"] for document in documents.values()]

    log(f"[解析 parsing] 提交 {len(document_ids)} 个文档进入解析队列")
    parsing_jobs = flow.submit_parsing(document_ids, label="submit-parsing")
    flow.wait_for_stage(document_ids, "parsing", parsing_jobs, label="wait-parsing")
    for document_id, job_id in zip(document_ids, parsing_jobs, strict=True):
        log_stage_completed(flow, kb_id, document_id, "parsing", [job_id], label="解析 parsing")

    # Semantic chunking needs a verified embedding model before chunking is queued.
    embedding = flow.json_request(
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
    require(isinstance(embedding.get("embedding_dimension"), int),
            f"embedding model did not report a dimension: {embedding!r}")
    log_model_config(embedding, kind="embedding")

    # An invalid regex must be rejected without mutating the document configuration.
    flow.request(
        "PUT",
        f"{flow.kb_root(kb_id)}/documents/{documents['regex']['id']}/chunking-config",
        expected=422,
        label="reject-invalid-regex-chunking",
        json={"strategy": "regex", "config": {"max_token_size": 512, "re_expression": "("}},
    )
    log("[校验] 非法正则表达式已按预期被 422 invalid_chunking_config 拒绝")

    # Assign each strategy with the capabilities-provided default configuration.
    for strategy, document in documents.items():
        updated = set_chunking_config(
            flow, kb_id, document["id"], strategy, strategies[strategy],
            label=f"configure-{strategy}-chunking",
        )
        require(updated.get("chunking_strategy") == strategy,
                f"{strategy} chunking configuration was not stored: {updated!r}")
        log(f"[切分配置] 文档 {document['id']} 使用策略 {strategy!r} config={strategies[strategy]}")

    log(f"[分段 chunking] 提交 {len(document_ids)} 个文档进入切分队列")
    chunking_jobs = flow.submit_stage("chunking", document_ids, label="submit-chunking")
    flow.wait_for_stage(document_ids, "chunking", chunking_jobs, label="wait-chunking")
    for document_id, job_id in zip(document_ids, chunking_jobs, strict=True):
        log_stage_completed(flow, kb_id, document_id, "chunking", [job_id], label="分段 chunking")

    for strategy, document in documents.items():
        detail = flow.document_detail(document["id"], label=f"verify-{strategy}-chunked")
        require(detail["chunking_strategy"] == strategy,
                f"{strategy} document lost its chunking strategy: {detail!r}")
        require(detail["stages"]["chunking"]["status"] == "succeeded",
                f"{strategy} chunking did not succeed: {detail!r}")
        log(f"[校验] 文档 {document['id']} 最终 chunking_strategy={detail['chunking_strategy']!r} status=succeeded")

    # Vectorize and retrieve to prove every strategy produced searchable chunks.
    log(f"[向量化 vectorization] 提交 {len(document_ids)} 个文档进入向量化队列")
    vectorization_jobs = flow.submit_stage(
        "vectorization", document_ids, label="submit-vectorization"
    )
    flow.wait_for_stage(
        document_ids, "vectorization", vectorization_jobs, label="wait-vectorization"
    )
    for document_id, job_id in zip(document_ids, vectorization_jobs, strict=True):
        log_stage_completed(flow, kb_id, document_id, "vectorization", [job_id], label="向量化 vectorization")

    result = flow.json_request(
        "POST",
        f"{flow.kb_root(kb_id)}/retrieve",
        label="retrieve-all-strategies",
        json={"query": RETRIEVAL_QUERY, "mode": "vector", "top_k": 5},
    )
    items = result.get("items", [])
    require(isinstance(items, list) and items,
            "chunking strategy retrieval returned no items")
    require(all(item.get("document_id") in set(document_ids) for item in items),
            "retrieval returned a document outside the strategy fixture set")
    log_retrieve(result, mode="vector")


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
    print("PASS: fixed/regex/semantic chunking strategies completed and were retrievable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
