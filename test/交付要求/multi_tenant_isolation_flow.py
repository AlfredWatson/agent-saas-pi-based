#!/usr/bin/env python3
"""Black-box acceptance for multi-tenant RAG/Agent isolation.

Covers requirement 9: 多用户 RAG/Agent 应用部署，不同用户的数据、知识库、
应用实例完全隔离.

The script authenticates two distinct tenants (A and B), gives each one its own
Workspace and Knowledge Base, populates A with a parsed/chunked document, and then
proves that every cross-tenant access returns ``404`` (or ``422`` for invalid
knowledge-base binding) while each tenant can still access its own resources.

Agent isolation is exercised at the session layer: each tenant creates a Profile
and Session, and neither tenant can read the other's session or bind the other's
knowledge base.  The Agent Runtime is only used for catalog discovery, so no real
provider key is required.

Two accounts are required.  Tenant A uses ``RAG_TEST_EMAIL``/``RAG_TEST_PASSWORD``
by default; tenant B uses ``--email-b``/``--password-b`` (or
``ISOLATION_TEST_EMAIL_B``/``ISOLATION_TEST_PASSWORD_B``).  When tenant B is not
provided, a fresh account is registered automatically (the Gateway has no
user-deletion API, so the account is intentionally retained).

Run after Gateway, the RAG worker, Redis, PostgreSQL and the Agent Runtime are
ready (no embedding/LLM model is needed for this flow)::

    uv run python test/交付要求/multi_tenant_isolation_flow.py --report /tmp/multi-tenant-isolation-flow.json

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
    log_document_uploaded,
    log_knowledge_base,
    log_stage_completed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "test" / "files"
ISOLATION_FIXTURE = "markdown-test-1.md"


def kb_path(workspace_id: str, kb_id: str, suffix: str = "") -> str:
    return f"/workspaces/{workspace_id}/knowledge-bases/{kb_id}{suffix}"


def upload_markdown(flow: UserFlow, kb_id: str, path: Path, label: str) -> dict:
    with path.open("rb") as handle:
        payload = flow.json_request(
            "POST", f"{flow.kb_root(kb_id)}/documents", label=label,
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


def create_profile_and_session(
    flow: UserFlow, workspace_id: str, kb_ids: list[str]
) -> dict[str, str]:
    providers = flow.json_request("GET", "/providers", label="list-providers").get(
        "providers", []
    )
    require(isinstance(providers, list) and providers
            and isinstance(providers[0], dict) and providers[0].get("id"),
            "no provider is available for session isolation")
    provider_id = providers[0]["id"]
    binding = flow.json_request(
        "POST", "/provider-bindings", expected=201, label="create-provider-binding",
        json={"provider_id": provider_id, "display_name": f"iso-{uuid4().hex[:10]}",
              "api_key": "isolation-not-verified"},
    )
    binding_id = binding["id"]
    models = flow.json_request(
        "GET", f"/provider-bindings/{binding_id}/models", label="list-provider-models"
    ).get("models", [])
    require(isinstance(models, list) and models
            and isinstance(models[0], dict) and models[0].get("id"),
            "no model is available for session isolation")
    profile = flow.json_request(
        "POST", "/agent-profiles", expected=201, label="create-agent-profile",
        json={"name": f"iso-{uuid4().hex[:10]}", "provider_binding_id": binding_id,
              "model_id": models[0]["id"]},
    )
    profile_id = profile["id"]
    session = flow.json_request(
        "POST", "/sessions", expected=201, label="create-session",
        json={"profile_id": profile_id, "workspace_id": workspace_id,
              "knowledge_base_ids": kb_ids},
    )
    session_id = session["id"]
    flow.resources.update(
        {"agent_binding_id": binding_id, "agent_profile_id": profile_id,
         "agent_session_id": session_id}
    )
    return {"session_id": session_id, "binding_id": binding_id, "profile_id": profile_id}


def run(
    flow_a: UserFlow, flow_b: UserFlow, *, tenant_b_email: str, tenant_b_password: str,
    register_tenant_b: bool,
) -> None:
    # Tenant A: login, workspace, knowledge base, and one processed document.
    flow_a.login_and_validate_capabilities()
    log(f"[租户 A 登录] user_id={flow_a.user_id} email={flow_a.config.email}")
    flow_a.create_workspace_and_knowledge_bases()
    require(flow_a.primary_kb_id is not None and flow_a.workspace_id is not None,
            "tenant A did not create a knowledge base")
    a_ws = flow_a.workspace_id
    a_kb = flow_a.primary_kb_id
    log(f"[租户 A 工作区] workspace_id={a_ws}")
    log_knowledge_base(flow_a.json_request("GET", flow_a.kb_root(a_kb), label="log-a-kb"))
    a_document = upload_markdown(flow_a, a_kb, FIXTURES / ISOLATION_FIXTURE,
                                 label="upload-tenant-a-document")
    log_document_uploaded(a_document)
    a_doc_id = a_document["id"]
    parsing = flow_a.submit_parsing([a_doc_id], label="tenant-a-parsing")
    flow_a.wait_for_stage([a_doc_id], "parsing", parsing, label="tenant-a-wait-parsing")
    log_stage_completed(flow_a, a_kb, a_doc_id, "parsing", parsing, label="租户 A 解析 parsing")
    chunking = flow_a.submit_stage("chunking", [a_doc_id], label="tenant-a-chunking")
    flow_a.wait_for_stage([a_doc_id], "chunking", chunking, label="tenant-a-wait-chunking")
    log_stage_completed(flow_a, a_kb, a_doc_id, "chunking", chunking, label="租户 A 分段 chunking")

    # Tenant B: bootstrap a fresh account when none was supplied, then login.
    if register_tenant_b:
        flow_b.request(
            "POST", "/auth/register", authenticated=False, expected=(201, 409),
            label="register-tenant-b",
            json={"email": tenant_b_email, "password": tenant_b_password},
        )
        log(f"[租户 B 注册] 已引导创建账号 email={tenant_b_email}")
    flow_b.login_and_validate_capabilities()
    log(f"[租户 B 登录] user_id={flow_b.user_id} email={flow_b.config.email}")
    flow_b.create_workspace_and_knowledge_bases()
    require(flow_b.primary_kb_id is not None and flow_b.workspace_id is not None,
            "tenant B did not create a knowledge base")
    b_ws = flow_b.workspace_id
    b_kb = flow_b.primary_kb_id
    log(f"[租户 B 工作区] workspace_id={b_ws}")
    log_knowledge_base(flow_b.json_request("GET", flow_b.kb_root(b_kb), label="log-b-kb"))

    # RAG isolation: tenant B can never read tenant A's workspace/KB/documents.
    log("[隔离校验/RAG] 租户 B 访问租户 A 的 workspace / knowledge-base / documents / retrieve 均应返回 404")
    flow_b.request("GET", f"/workspaces/{a_ws}", expected=404,
                   label="b-get-a-workspace")
    flow_b.request("GET", f"/workspaces/{a_ws}/knowledge-bases", expected=404,
                   label="b-list-a-knowledge-bases")
    flow_b.request("GET", kb_path(a_ws, a_kb), expected=404, label="b-get-a-knowledge-base")
    flow_b.request("GET", kb_path(a_ws, a_kb, "/documents"), expected=404,
                   label="b-list-a-documents")
    flow_b.request("GET", kb_path(a_ws, a_kb, f"/documents/{a_doc_id}"), expected=404,
                   label="b-get-a-document")
    flow_b.json_request(
        "POST", kb_path(a_ws, a_kb, "/retrieve"), expected=404, label="b-retrieve-a-kb",
        json={"query": "Unity", "mode": "vector", "top_k": 5},
    )
    flow_b.request("GET", f"/workspaces/{a_ws}/sessions", expected=404,
                   label="b-list-a-workspace-sessions")
    log("[隔离校验/RAG] 通过：租户 B 无法读取租户 A 的任何 RAG 资源")

    # And the reverse direction.
    flow_a.request("GET", kb_path(b_ws, b_kb), expected=404, label="a-get-b-knowledge-base")
    flow_a.request("GET", f"/workspaces/{b_ws}/knowledge-bases", expected=404,
                   label="a-list-b-knowledge-bases")
    flow_a.request("GET", kb_path(b_ws, b_kb, "/documents"), expected=404,
                   label="a-list-b-documents")
    log("[隔离校验/RAG] 通过：租户 A 无法读取租户 B 的任何 RAG 资源")

    # Agent isolation: each tenant creates a session; neither can read the other's.
    a_agent = create_profile_and_session(flow_a, a_ws, [a_kb])
    b_agent = create_profile_and_session(flow_b, b_ws, [b_kb])
    log(f"[租户 A 会话] session_id={a_agent['session_id']} profile_id={a_agent['profile_id']}")
    log(f"[租户 B 会话] session_id={b_agent['session_id']} profile_id={b_agent['profile_id']}")
    flow_b.request("GET", f"/sessions/{a_agent['session_id']}", expected=404,
                   label="b-get-a-session")
    flow_b.request("GET", f"/sessions/{a_agent['session_id']}/messages", expected=404,
                   label="b-get-a-session-messages")
    flow_a.request("GET", f"/sessions/{b_agent['session_id']}", expected=404,
                   label="a-get-b-session")
    log("[隔离校验/Agent] 通过：双方均无法读取对方的会话与消息")

    # Cross-tenant knowledge-base binding must be rejected at session creation.
    flow_b.json_request(
        "POST", "/sessions", expected=422, label="b-reject-a-kb-binding",
        json={"profile_id": b_agent["profile_id"], "workspace_id": b_ws,
              "knowledge_base_ids": [a_kb]},
    )
    flow_a.json_request(
        "POST", "/sessions", expected=422, label="a-reject-b-kb-binding",
        json={"profile_id": a_agent["profile_id"], "workspace_id": a_ws,
              "knowledge_base_ids": [b_kb]},
    )
    log("[隔离校验/绑定] 通过：跨租户 knowledge_base 绑定在创建会话时被 422 拒绝")

    flow_a.resources["tenant_a_workspace_id"] = a_ws
    flow_a.resources["tenant_a_knowledge_base_id"] = a_kb
    flow_b.resources["tenant_b_workspace_id"] = b_ws
    flow_b.resources["tenant_b_knowledge_base_id"] = b_kb


def cleanup_profiles_and_bindings(
    flow: UserFlow, agent: dict[str, str] | None
) -> list[str]:
    errors: list[str] = []
    if not agent:
        return errors
    for resource_id, path_template, label in (
        (agent.get("profile_id"), "/agent-profiles/{}", "cleanup-agent-profile"),
        (agent.get("binding_id"), "/provider-bindings/{}", "cleanup-agent-binding"),
    ):
        if not resource_id:
            continue
        try:
            flow.request("DELETE", path_template.format(resource_id), expected=204, label=label)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    return errors


def parse_args() -> tuple[RagConfig, RagConfig, Path | None, str, str, bool]:
    dotenv = parse_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway",
        default=configured_value("RAG_TEST_GATEWAY", dotenv) or gateway_from_env_file(),
    )
    parser.add_argument(
        "--email-a",
        default=configured_value("RAG_TEST_EMAIL", dotenv)
        or configured_value("AGENT_TEST_EMAIL", dotenv),
    )
    parser.add_argument(
        "--password-a",
        default=configured_value("RAG_TEST_PASSWORD", dotenv)
        or configured_value("AGENT_TEST_PASSWORD", dotenv),
    )
    parser.add_argument(
        "--email-b",
        default=configured_value("ISOLATION_TEST_EMAIL_B", dotenv),
    )
    parser.add_argument(
        "--password-b",
        default=configured_value("ISOLATION_TEST_PASSWORD_B", dotenv),
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

    require(bool(args.email_a), "required test configuration is missing: tenant A email")
    require(bool(args.password_a), "required test configuration is missing: tenant A password")
    for value in (args.request_timeout_seconds, args.job_timeout_seconds, args.poll_interval_seconds):
        require(value > 0, "timeouts and poll interval must be positive")

    register_tenant_b = not bool(args.email_b and args.password_b)
    tenant_b_email = args.email_b or f"isolation-b-{uuid4().hex}@example.com"
    tenant_b_password = args.password_b or f"Isolation-B-{uuid4().hex}-A!"

    def make_config(email: str, password: str) -> RagConfig:
        return RagConfig(
            gateway=normalized_api_base(args.gateway),
            email=email,
            password=password,
            files_dir=FIXTURES,
            embedding_base_url=normalized_api_base(
                configured_value("RAG_TEST_EMBEDDING_BASE_URL", dotenv)
                or "http://127.0.0.1:31995/v1"
            ),
            embedding_model=configured_value("RAG_TEST_EMBEDDING_MODEL", dotenv)
            or "Qwen3-Embedding-8B",
            llm_base_url=normalized_api_base(
                configured_value("RAG_TEST_LLM_BASE_URL", dotenv)
                or "http://127.0.0.1:30018/v1"
            ),
            llm_model=configured_value("RAG_TEST_LLM_MODEL", dotenv) or "Qwen3.8-27B-FP8",
            model_api_key=configured_value("RAG_TEST_MODEL_API_KEY", dotenv) or "EMPTY",
            vector_backend=args.vector_backend,
            request_timeout_seconds=args.request_timeout_seconds,
            job_timeout_seconds=args.job_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            keep_resources=args.keep_resources,
            report=None,
        )

    return (
        make_config(args.email_a, args.password_a),
        make_config(tenant_b_email, tenant_b_password),
        args.report.resolve() if args.report else None,
        tenant_b_email,
        tenant_b_password,
        register_tenant_b,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    try:
        config_a, config_b, report_path, tenant_b_email, tenant_b_password, register_b = parse_args()
    except FlowError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    flow_a = UserFlow(config_a)
    flow_b = UserFlow(config_b)
    failure: Exception | None = None
    cleanup_errors: list[str] = []
    a_agent: dict[str, str] | None = None
    b_agent: dict[str, str] | None = None
    try:
        run(flow_a, flow_b, tenant_b_email=tenant_b_email,
            tenant_b_password=tenant_b_password, register_tenant_b=register_b)
        a_agent = {
            "session_id": flow_a.resources.get("agent_session_id"),
            "binding_id": flow_a.resources.get("agent_binding_id"),
            "profile_id": flow_a.resources.get("agent_profile_id"),
        }
        b_agent = {
            "session_id": flow_b.resources.get("agent_session_id"),
            "binding_id": flow_b.resources.get("agent_binding_id"),
            "profile_id": flow_b.resources.get("agent_profile_id"),
        }
    except Exception as exc:
        failure = exc
    finally:
        try:
            if not config_a.keep_resources:
                # Delete workspaces first (they cascade-delete sessions), then
                # the now-unreferenced profiles and bindings.
                cleanup_errors.extend(flow_a.cleanup())
                cleanup_errors.extend(flow_b.cleanup())
                cleanup_errors.extend(cleanup_profiles_and_bindings(flow_a, a_agent))
                cleanup_errors.extend(cleanup_profiles_and_bindings(flow_b, b_agent))
        finally:
            report = {
                "status": "passed" if failure is None and not cleanup_errors else "failed",
                "finished_at": datetime.now(UTC).isoformat(),
                "error": str(failure) if failure else None,
                "cleanup_errors": cleanup_errors,
                "tenant_a": flow_a.report("completed", None, []),
                "tenant_b": flow_b.report("completed", None, []),
            }
            if report_path:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                print(f"Report: {report_path}")
            flow_a.close()
            flow_b.close()
    if failure is not None:
        print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    if cleanup_errors:
        print("FAIL: cleanup did not complete:", file=sys.stderr)
        for error in cleanup_errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("PASS: multi-tenant RAG/Agent isolation completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
