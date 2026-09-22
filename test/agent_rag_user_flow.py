#!/usr/bin/env python3
"""Black-box Agent -> Runtime rag_search -> Gateway RAG acceptance.

The script creates a disposable Workspace and vectorized Markdown knowledge
base, binds that KB while creating an Agent Session, and proves through public
SSE/history that Pi invoked ``rag_search`` and answered from its evidence.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_user_flow import AgentUserFlow, Config as AgentConfig, FlowError, gateway_from_env_file, require
from flow_env import configured_value, parse_dotenv
from rag_user_flow import Config as RagConfig, UserFlow as RagUserFlow, normalized_api_base


ROOT = Path(__file__).resolve().parents[1]


def config_from_args() -> tuple[AgentConfig, RagConfig, Path | None]:
    dotenv = parse_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default=configured_value("AGENT_TEST_GATEWAY", dotenv) or configured_value("RAG_TEST_GATEWAY", dotenv) or gateway_from_env_file())
    parser.add_argument("--email", default=configured_value("AGENT_TEST_EMAIL", dotenv) or configured_value("RAG_TEST_EMAIL", dotenv))
    parser.add_argument("--password", default=configured_value("AGENT_TEST_PASSWORD", dotenv) or configured_value("RAG_TEST_PASSWORD", dotenv))
    parser.add_argument("--provider-id", default=configured_value("AGENT_TEST_PROVIDER_ID", dotenv))
    parser.add_argument("--provider-api-key", default=configured_value("AGENT_TEST_PROVIDER_API_KEY", dotenv))
    parser.add_argument("--model-id", default=configured_value("AGENT_TEST_MODEL_ID", dotenv))
    parser.add_argument("--thinking-level", default=configured_value("AGENT_TEST_THINKING_LEVEL", dotenv))
    parser.add_argument("--embedding-base-url", default=configured_value("RAG_TEST_EMBEDDING_BASE_URL", dotenv) or "http://127.0.0.1:31995/v1")
    parser.add_argument("--embedding-model", default=configured_value("RAG_TEST_EMBEDDING_MODEL", dotenv) or "Qwen3-Embedding-8B")
    parser.add_argument("--model-api-key", default=configured_value("RAG_TEST_MODEL_API_KEY", dotenv) or "EMPTY")
    parser.add_argument("--reranker-base-url", default=configured_value("RAG_TEST_RERANKER_BASE_URL", dotenv))
    parser.add_argument("--reranker-model", default=configured_value("RAG_TEST_RERANKER_MODEL", dotenv))
    parser.add_argument("--reranker-api-key", default=configured_value("RAG_TEST_RERANKER_API_KEY", dotenv))
    parser.add_argument("--vector-backend", choices=("postgresql", "milvus", "chroma", "qdrant"), default=configured_value("RAG_TEST_VECTOR_BACKEND", dotenv) or "postgresql")
    parser.add_argument("--request-timeout-seconds", type=float, default=120)
    parser.add_argument("--job-timeout-seconds", type=float, default=1_800)
    parser.add_argument("--run-timeout-seconds", type=float, default=300)
    parser.add_argument("--poll-interval-seconds", type=float, default=2)
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    for value, name in ((args.email, "AGENT_TEST_EMAIL or RAG_TEST_EMAIL"), (args.password, "AGENT_TEST_PASSWORD or RAG_TEST_PASSWORD"), (args.provider_id, "AGENT_TEST_PROVIDER_ID"), (args.provider_api_key, "AGENT_TEST_PROVIDER_API_KEY"), (args.model_id, "AGENT_TEST_MODEL_ID")):
        require(bool(value), f"required test configuration is missing: {name}")
    reranker_values = (args.reranker_base_url, args.reranker_model, args.reranker_api_key)
    require(all(reranker_values) or not any(reranker_values), "reranker base URL, model, and API key must be configured together")
    require(args.request_timeout_seconds > 0 and args.job_timeout_seconds > 0 and args.run_timeout_seconds > 0 and args.poll_interval_seconds > 0, "timeouts and poll interval must be positive")
    gateway = normalized_api_base(args.gateway)
    return (
        AgentConfig(gateway=gateway, email=args.email, password=args.password, provider_id=args.provider_id, provider_api_key=args.provider_api_key, model_id=args.model_id, thinking_level=args.thinking_level, request_timeout_seconds=args.request_timeout_seconds, run_timeout_seconds=args.run_timeout_seconds, poll_interval_seconds=args.poll_interval_seconds, keep_resources=args.keep_resources, report=None),
        RagConfig(gateway=gateway, email=args.email, password=args.password, files_dir=ROOT / "test/files", embedding_base_url=normalized_api_base(args.embedding_base_url), embedding_model=args.embedding_model, llm_base_url=normalized_api_base(configured_value("RAG_TEST_LLM_BASE_URL", dotenv) or "http://127.0.0.1:30018/v1"), llm_model=configured_value("RAG_TEST_LLM_MODEL", dotenv) or "Qwen3.8-27B-FP8", model_api_key=args.model_api_key, vector_backend=args.vector_backend, request_timeout_seconds=args.request_timeout_seconds, job_timeout_seconds=args.job_timeout_seconds, poll_interval_seconds=args.poll_interval_seconds, keep_resources=args.keep_resources, report=None, reranker_base_url=normalized_api_base(args.reranker_base_url) if args.reranker_base_url else None, reranker_model=args.reranker_model, reranker_api_key=args.reranker_api_key),
        args.report.resolve() if args.report else None,
    )


def create_vectorized_marker(flow: RagUserFlow) -> tuple[str, str, str]:
    flow.login_and_validate_capabilities()
    flow.create_workspace_and_knowledge_bases()
    require(flow.primary_kb_id is not None and flow.workspace_id is not None, "RAG setup did not create a primary knowledge base")
    marker = f"AGENT_RAG_E2E_MARKER_{uuid4().hex}"
    upload = flow.json_request(
        "POST", f"{flow.kb_root(flow.primary_kb_id)}/documents", label="upload-agent-rag-marker",
        files={"files": ("agent-rag-marker.md", f"# Runtime RAG acceptance\n\nThe required marker is {marker}.\n".encode(), "text/markdown")},
    )
    documents = [item.get("document") for item in upload.get("items", []) if isinstance(item, dict) and item.get("status") == "uploaded"]
    require(len(documents) == 1 and isinstance(documents[0], dict), "marker upload did not create one document")
    document_id = documents[0].get("id")
    require(isinstance(document_id, str), "marker document has no id")
    parsing = flow.submit_parsing([document_id], label="submit-agent-rag-parsing")
    flow.wait_for_stage([document_id], "parsing", parsing, label="wait-agent-rag-parsing")
    flow.json_request("PUT", f"{flow.kb_root(flow.primary_kb_id)}/embedding-model", label="configure-agent-rag-embedding", json={"protocol": "openai", "base_url": flow.config.embedding_base_url, "api_key": flow.config.model_api_key, "model_name": flow.config.embedding_model})
    chunking = flow.submit_stage("chunking", [document_id], label="submit-agent-rag-chunking")
    flow.wait_for_stage([document_id], "chunking", chunking, label="wait-agent-rag-chunking")
    vectorization = flow.submit_stage("vectorization", [document_id], label="submit-agent-rag-vectorization")
    flow.wait_for_stage([document_id], "vectorization", vectorization, label="wait-agent-rag-vectorization")
    if flow.reranker_enabled:
        require(flow.config.reranker_model is not None and flow.config.reranker_api_key is not None, "reranker configuration is incomplete")
        flow.json_request("PUT", f"{flow.kb_root(flow.primary_kb_id)}/reranker-model", label="configure-agent-rag-reranker", json={"protocol": "vllm", "base_url": flow.config.reranker_base_url, "api_key": flow.config.reranker_api_key, "model_name": flow.config.reranker_model})
    return flow.primary_kb_id, document_id, marker


def remove_agent_profile_and_binding(flow: AgentUserFlow) -> list[str]:
    errors: list[str] = []
    for path, label in ((f"/agent-profiles/{flow.profile_id}", "cleanup-agent-profile"), (f"/provider-bindings/{flow.binding_id}", "cleanup-agent-binding")):
        if path.endswith("None"):
            continue
        try:
            flow.request("DELETE", path, expected=204, label=label)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    return errors


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    try:
        agent_config, rag_config, report_path = config_from_args()
    except FlowError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    rag = RagUserFlow(rag_config)
    agent = AgentUserFlow(agent_config)
    failure: Exception | None = None
    cleanup_errors: list[str] = []
    marker: str | None = None
    try:
        knowledge_base_id, document_id, marker = create_vectorized_marker(rag)
        agent.login_and_preflight()
        agent.workspace_id = rag.workspace_id
        agent.create_binding_and_profile()
        agent.create_session([knowledge_base_id])
        response, events = agent.stream(
            f"Use rag_search in knowledge base {knowledge_base_id} with hybrid mode. Report the exact marker and cite document_id and chunk_id. Do not use workspace files or the network.",
            label="agent-rag-search",
        )
        require(any(name == "tool.started" and payload.get("toolName") == "rag_search" for name, payload in events), "Agent did not start rag_search")
        require(any(name == "tool.completed" and payload.get("toolName") == "rag_search" and payload.get("isError") is False for name, payload in events), "Agent did not complete rag_search successfully")
        require(marker in response, "Agent answer does not contain the retrieved marker")
        history = agent.history(label="agent-rag-history")
        require(any(item.get("tool_name") == "rag_search" and item.get("role") == "tool_result" for item in history), "public history lacks rag_search tool result")
        require(any(document_id in str(item.get("content", "")) or document_id in str(item.get("result", "")) for item in history if item.get("tool_name") == "rag_search"), "rag_search history lacks document provenance")
    except Exception as exc:
        failure = exc
    finally:
        try:
            if not agent_config.keep_resources:
                cleanup_errors.extend(rag.cleanup())
                # Workspace cleanup removes its Agent Sessions.  A Profile is
                # intentionally undeletable while a Session still references it.
                cleanup_errors.extend(remove_agent_profile_and_binding(agent))
        finally:
            report = {"status": "passed" if failure is None and not cleanup_errors else "failed", "finished_at": datetime.now(UTC).isoformat(), "error": str(failure) if failure else None, "cleanup_errors": cleanup_errors, "agent": agent.report("completed", None, []), "rag": rag.report("completed", None, [])}
            if report_path:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                print(f"Report: {report_path}")
            agent.close()
            rag.close()
    if failure or cleanup_errors:
        print(f"FAIL: {failure or cleanup_errors}", file=sys.stderr)
        return 1
    print("PASS: Agent Runtime invoked rag_search against a newly vectorized knowledge base")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
