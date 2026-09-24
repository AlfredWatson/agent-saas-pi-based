#!/usr/bin/env python3
"""Public HTTP acceptance for a reachable vLLM or SGLang Agent model.

Run once per provider. This script never calls Runtime internal routes.
Required: AGENT_TEST_EMAIL, AGENT_TEST_PASSWORD, --provider, --base-url,
--model-id, --context-window, --max-tokens. The model server must support
streaming Chat Completions and tool calls.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from agent_user_flow import AgentUserFlow, Config, FlowError, gateway_from_env_file, require
from flow_env import configured_value, parse_dotenv


def main() -> int:
    dotenv = parse_dotenv(Path(__file__).resolve().parents[1] / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default=configured_value("AGENT_TEST_GATEWAY", dotenv) or gateway_from_env_file())
    parser.add_argument("--email", default=configured_value("AGENT_TEST_EMAIL", dotenv))
    parser.add_argument("--password", default=configured_value("AGENT_TEST_PASSWORD", dotenv))
    parser.add_argument("--provider", choices=("vllm", "sglang"), required=True)
    parser.add_argument("--base-url", required=True, help="URL reachable from the Runtime container")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--context-window", type=int, required=True)
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--reasoning", action="store_true")
    parser.add_argument("--recreate-runtime", action="store_true", help="restart this test account's idle Runtime to load current code")
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    require(bool(args.email and args.password), "AGENT_TEST_EMAIL and AGENT_TEST_PASSWORD are required")
    require(args.context_window > args.max_tokens > 0, "invalid context/token limits")
    flow = AgentUserFlow(Config(
        gateway=args.gateway.rstrip("/"), email=args.email, password=args.password,
        provider_id=args.provider, provider_api_key=args.api_key, model_id=args.model_id,
        thinking_level=None, request_timeout_seconds=60, run_timeout_seconds=300,
        poll_interval_seconds=2, keep_resources=args.keep_resources, report=args.report,
    ))
    result = {"provider": args.provider, "status": "failed", "stages": []}
    try:
        login = flow.json_request("POST", "/auth/login", authenticated=False, label="login", json={"email": args.email, "password": args.password})
        flow.token = login["access_token"]
        if args.recreate_runtime:
            flow.json_request("POST", "/runtime:recreate", label="recreate-runtime")
        flow.create_workspace()
        root = f"/workspaces/{flow.workspace_id}/provider-bindings"
        binding = flow.json_request("POST", root, expected=201, label="create-local-binding", json={
            "provider_id": args.provider, "display_name": f"local-{uuid4().hex[:10]}",
            "base_url": args.base_url, "api_key": args.api_key,
        })
        flow.binding_id = binding["id"]
        result["stages"].append("binding-created")
        models = flow.json_request("GET", f"{root}/{flow.binding_id}/models", label="discovered-models")["models"]
        require(any(model["id"] == args.model_id and model["status"] == "pending" for model in models), "requested model was not discovered")
        configured = flow.json_request("PUT", f"{root}/{flow.binding_id}/models:configure", label="configure-model", json={
            "model_id": args.model_id, "context_window": args.context_window,
            "max_tokens": args.max_tokens, "reasoning": args.reasoning,
        })
        require(configured["status"] == "ready", "model was not configured")
        available = flow.json_request("GET", f"/workspaces/{flow.workspace_id}/available-models", label="available-models")["items"]
        require(any(item["provider_binding_id"] == flow.binding_id and item["id"] == args.model_id for item in available), "configured model is unavailable")
        result["stages"].append("model-configured")
        profile = flow.json_request("POST", "/agent-profiles", expected=201, label="create-profile", json={
            "name": f"local-{uuid4().hex[:10]}", "provider_binding_id": flow.binding_id,
            "model_id": args.model_id, "thinking_level": None,
        })
        flow.profile_id = profile["id"]
        flow.create_session()
        marker = f"LOCAL_TOOL_{uuid4().hex[:12]}"
        path = f"{marker}.txt"
        flow.json_request("POST", f"/workspaces/{flow.workspace_id}/files", expected=201, label="upload-tool-file",
                          data={"path": path}, files={"file": (path, marker.encode(), "text/plain")})
        reply, events = flow.stream(f"Use the read tool to read {path}. Then include its exact contents in your reply.", label="tool-chat")
        names = [name for name, _ in events]
        require("message.failed" not in names and "message.completed" in names, "chat did not complete")
        result["stages"].append("chat-completed")
        require("tool.started" in names and "tool.completed" in names, "model did not complete a tool call")
        require(marker in reply, "assistant did not return the tool result")
        history = flow.history(label="tool-history")
        calls = {item["tool_call_id"] for item in history if item["role"] == "tool_call"}
        results = {item["tool_call_id"] for item in history if item["role"] == "tool_result"}
        require(calls and calls <= results, "tool result is missing from history")
        result["stages"].append("chat-and-tool-call")
        refreshed = flow.json_request("POST", f"{root}/{flow.binding_id}/models:refresh", label="refresh-models")["models"]
        require(any(item["id"] == args.model_id and item["status"] == "ready" for item in refreshed), "refresh lost the configured model")
        result["stages"].append("catalog-refreshed")
        result["status"] = "passed"
    except (FlowError, KeyError) as exc:
        result["error"] = str(exc)
    finally:
        if not args.keep_resources and flow.token:
            for method, path in [
                ("DELETE", f"/sessions/{flow.session_id}") if flow.session_id else (None, None),
                ("DELETE", f"/agent-profiles/{flow.profile_id}") if flow.profile_id else (None, None),
                ("DELETE", f"/workspaces/{flow.workspace_id}/provider-bindings/{flow.binding_id}") if flow.binding_id else (None, None),
                ("DELETE", f"/workspaces/{flow.workspace_id}") if flow.workspace_id else (None, None),
            ]:
                if method and path:
                    try:
                        flow.request(method, path, expected=204, label="cleanup")
                    except FlowError as exc:
                        result.setdefault("cleanup_errors", []).append(str(exc))
        flow.close()
    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" and not result.get("cleanup_errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
