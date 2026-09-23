#!/usr/bin/env python3
"""Black-box acceptance for base-model Agent tool binding and function calling.

Covers requirement 7: 基于基座模型的智能体系统，支持工具绑定与函数调用.

The script builds a small vectorized knowledge base, binds it while creating an
Agent Session (which registers the ``rag_search`` tool in the Runtime), and proves
through public SSE and history that the model invoked the bound tool with
arguments and received a successful result (function calling), with every tool
call paired to a matching tool result.

Required environment variables: the Agent set from test/agent_user_flow.py plus
the embedding variables from test/rag_user_flow.py.  Run after Gateway, the RAG
worker, the Agent Runtime, Redis, PostgreSQL and the embedding server are ready::

    uv run python test/交付要求/agent_tool_call_flow.py --report /tmp/agent-tool-call-flow.json

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
from agent_user_flow import AgentUserFlow, Config as AgentConfig, FlowError, gateway_from_env_file, require
from rag_user_flow import Config as RagConfig, UserFlow, normalized_api_base
from _delivery_log import (
    log,
    log_agent_events,
    log_document_uploaded,
    log_history,
    log_knowledge_base,
    log_model_config,
    log_stage_completed,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def config_from_args() -> tuple[AgentConfig, RagConfig, Path | None]:
    dotenv = parse_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway",
        default=configured_value("AGENT_TEST_GATEWAY", dotenv)
        or configured_value("RAG_TEST_GATEWAY", dotenv)
        or gateway_from_env_file(),
    )
    parser.add_argument(
        "--email",
        default=configured_value("AGENT_TEST_EMAIL", dotenv)
        or configured_value("RAG_TEST_EMAIL", dotenv),
    )
    parser.add_argument(
        "--password",
        default=configured_value("AGENT_TEST_PASSWORD", dotenv)
        or configured_value("RAG_TEST_PASSWORD", dotenv),
    )
    parser.add_argument("--provider-id", default=configured_value("AGENT_TEST_PROVIDER_ID", dotenv))
    parser.add_argument("--provider-api-key", default=configured_value("AGENT_TEST_PROVIDER_API_KEY", dotenv))
    parser.add_argument("--model-id", default=configured_value("AGENT_TEST_MODEL_ID", dotenv))
    parser.add_argument("--thinking-level", default=configured_value("AGENT_TEST_THINKING_LEVEL", dotenv))
    parser.add_argument(
        "--embedding-base-url",
        default=configured_value("RAG_TEST_EMBEDDING_BASE_URL", dotenv)
        or "http://127.0.0.1:31995/v1",
    )
    parser.add_argument(
        "--embedding-model",
        default=configured_value("RAG_TEST_EMBEDDING_MODEL", dotenv) or "Qwen3-Embedding-8B",
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
    parser.add_argument("--run-timeout-seconds", type=float, default=300)
    parser.add_argument("--poll-interval-seconds", type=float, default=2)
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    for value, name in (
        (args.email, "AGENT_TEST_EMAIL or RAG_TEST_EMAIL"),
        (args.password, "AGENT_TEST_PASSWORD or RAG_TEST_PASSWORD"),
        (args.provider_id, "AGENT_TEST_PROVIDER_ID"),
        (args.provider_api_key, "AGENT_TEST_PROVIDER_API_KEY"),
        (args.model_id, "AGENT_TEST_MODEL_ID"),
    ):
        require(bool(value), f"required test configuration is missing: {name}")
    for value in (args.request_timeout_seconds, args.job_timeout_seconds,
                  args.run_timeout_seconds, args.poll_interval_seconds):
        require(value > 0, "timeouts and poll interval must be positive")

    gateway = normalized_api_base(args.gateway)
    agent_config = AgentConfig(
        gateway=gateway,
        email=args.email,
        password=args.password,
        provider_id=args.provider_id,
        provider_api_key=args.provider_api_key,
        model_id=args.model_id,
        thinking_level=args.thinking_level,
        request_timeout_seconds=args.request_timeout_seconds,
        run_timeout_seconds=args.run_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        keep_resources=args.keep_resources,
        report=None,
    )
    rag_config = RagConfig(
        gateway=gateway,
        email=args.email,
        password=args.password,
        files_dir=REPO_ROOT / "test" / "files",
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
        report=None,
    )
    return agent_config, rag_config, args.report.resolve() if args.report else None


def create_vectorized_marker(rag: UserFlow) -> tuple[str, str, str]:
    """Create one vectorized knowledge-base document with a unique marker."""
    rag.login_and_validate_capabilities()
    log(f"[RAG 登录] user_id={rag.user_id}")
    rag.create_workspace_and_knowledge_bases()
    require(rag.primary_kb_id is not None and rag.workspace_id is not None,
            "RAG setup did not create a primary knowledge base")
    log(f"[工作区] id={rag.workspace_id}")
    log_knowledge_base(rag.json_request("GET", rag.kb_root(rag.primary_kb_id), label="log-knowledge-base"))
    marker = f"AGENT_TOOL_CALL_MARKER_{uuid4().hex}"
    content = f"# Agent tool binding\n\nThe required marker is {marker}.\n".encode()
    upload = rag.json_request(
        "POST", f"{rag.kb_root(rag.primary_kb_id)}/documents", label="upload-tool-marker",
        files={"files": ("agent-tool-marker.md", content, "text/markdown")},
    )
    documents = [
        item.get("document")
        for item in upload.get("items", [])
        if isinstance(item, dict) and item.get("status") == "uploaded"
    ]
    require(len(documents) == 1 and isinstance(documents[0], dict),
            "marker upload did not create one document")
    document_id = documents[0]["id"]
    log_document_uploaded(documents[0])

    parsing = rag.submit_parsing([document_id], label="submit-tool-parsing")
    rag.wait_for_stage([document_id], "parsing", parsing, label="wait-tool-parsing")
    log_stage_completed(rag, rag.primary_kb_id, document_id, "parsing", parsing, label="解析 parsing")
    embedding = rag.json_request(
        "PUT", f"{rag.kb_root(rag.primary_kb_id)}/embedding-model",
        label="configure-tool-embedding",
        json={"protocol": "openai", "base_url": rag.config.embedding_base_url,
              "api_key": rag.config.model_api_key, "model_name": rag.config.embedding_model},
    )
    log_model_config(embedding, kind="embedding")
    chunking = rag.submit_stage("chunking", [document_id], label="submit-tool-chunking")
    rag.wait_for_stage([document_id], "chunking", chunking, label="wait-tool-chunking")
    log_stage_completed(rag, rag.primary_kb_id, document_id, "chunking", chunking, label="分段 chunking")
    vectorization = rag.submit_stage("vectorization", [document_id], label="submit-tool-vectorization")
    rag.wait_for_stage([document_id], "vectorization", vectorization, label="wait-tool-vectorization")
    log_stage_completed(rag, rag.primary_kb_id, document_id, "vectorization", vectorization, label="向量化 vectorization")
    return rag.primary_kb_id, document_id, marker


def remove_agent_profile_and_binding(agent: AgentUserFlow) -> list[str]:
    errors: list[str] = []
    for path, label in (
        (f"/agent-profiles/{agent.profile_id}", "cleanup-agent-profile"),
        (f"/provider-bindings/{agent.binding_id}", "cleanup-agent-binding"),
    ):
        if path.endswith("None"):
            continue
        try:
            agent.request("DELETE", path, expected=204, label=label)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    return errors


def run(rag: UserFlow, agent: AgentUserFlow) -> None:
    knowledge_base_id, document_id, marker = create_vectorized_marker(rag)

    agent.login_and_preflight()
    log(f"[Agent 登录] user_id={agent.user_id} provider={agent.config.provider_id} model={agent.config.model_id} thinking_level={agent.config.thinking_level}")
    agent.workspace_id = rag.workspace_id
    agent.create_binding_and_profile()
    log(f"[Provider 绑定] binding_id={agent.binding_id} provider_id={agent.config.provider_id}")
    log(f"[Agent Profile] profile_id={agent.profile_id} model_id={agent.config.model_id}")
    agent.create_session([knowledge_base_id])
    log(f"[会话] session_id={agent.session_id} 绑定知识库={[knowledge_base_id]}")

    # The session creation response must record the tool binding (rag_search).
    session = agent.json_request("GET", f"/sessions/{agent.session_id}",
                                 label="get-session-binding")
    require(session.get("knowledge_base_ids") == [knowledge_base_id],
            f"session did not record its knowledge-base binding: {session!r}")
    log(f"[工具绑定] 会话绑定的 knowledge_base_ids={session.get('knowledge_base_ids')} → Runtime 注册 rag_search 工具")

    response_text, events = agent.stream(
        f"Use rag_search in knowledge base {knowledge_base_id} with hybrid mode. "
        f"Report the exact marker and cite document_id and chunk_id. "
        f"Do not use workspace files or the network.",
        label="agent-tool-call",
    )
    rag_started = [
        payload for name, payload in events
        if name == "tool.started" and payload.get("toolName") == "rag_search"
    ]
    rag_completed = [
        payload for name, payload in events
        if name == "tool.completed" and payload.get("toolName") == "rag_search"
    ]
    require(rag_started, "Agent did not start the bound rag_search tool")
    require(isinstance(rag_started[0].get("args"), dict),
            "rag_search tool call did not carry JSON arguments")
    require(rag_completed, "Agent did not complete rag_search")
    require(all(payload.get("isError") is False for payload in rag_completed),
            "rag_search completed with an error")
    require(any("result" in payload for payload in rag_completed),
            "rag_search completion did not carry a result")
    require(marker in response_text,
            "Agent answer does not contain the retrieved marker")
    log_agent_events(events, response_text)

    # Function-calling invariant: every started tool call has a matching completion.
    started_ids = {
        payload.get("toolCallId")
        for name, payload in events
        if name == "tool.started" and isinstance(payload.get("toolCallId"), str)
    }
    completed_ids = {
        payload.get("toolCallId")
        for name, payload in events
        if name == "tool.completed" and isinstance(payload.get("toolCallId"), str)
    }
    require(started_ids and started_ids <= completed_ids,
            "not every started tool call produced a matching completion")

    history = agent.history(label="agent-tool-history")
    require(any(item.get("tool_name") == "rag_search" and item.get("role") == "tool_result"
                for item in history),
            "public history lacks a rag_search tool result")
    require(any(document_id in str(item.get("content", ""))
                or document_id in str(item.get("result", ""))
                for item in history if item.get("tool_name") == "rag_search"),
            "rag_search history lacks document provenance")
    log_history(history)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    try:
        agent_config, rag_config, report_path = config_from_args()
    except FlowError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    rag = UserFlow(rag_config)
    agent = AgentUserFlow(agent_config)
    failure: Exception | None = None
    cleanup_errors: list[str] = []
    try:
        run(rag, agent)
    except Exception as exc:
        failure = exc
    finally:
        try:
            if not agent_config.keep_resources:
                cleanup_errors.extend(rag.cleanup())
                cleanup_errors.extend(remove_agent_profile_and_binding(agent))
        finally:
            report = {
                "status": "passed" if failure is None and not cleanup_errors else "failed",
                "finished_at": datetime.now(UTC).isoformat(),
                "error": str(failure) if failure else None,
                "cleanup_errors": cleanup_errors,
                "agent": agent.report("completed", None, []),
                "rag": rag.report("completed", None, []),
            }
            if report_path:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                print(f"Report: {report_path}")
            agent.close()
            rag.close()
    if failure or cleanup_errors:
        print(f"FAIL: {failure or cleanup_errors}", file=sys.stderr)
        return 1
    print("PASS: Agent tool binding and function calling completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
