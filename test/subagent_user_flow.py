#!/usr/bin/env python3
"""Public HTTP smoke test for a real call_subagents tool call.

Uses the existing AGENT_TEST_* settings in .env, creates isolated resources,
and removes them after checking the parent/child API contract.
"""

from __future__ import annotations

import json
import logging
import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
from agent_user_flow import AgentUserFlow, Config, FlowError, gateway_from_env_file, require
from flow_env import configured_value, parse_dotenv


ROOT = Path(__file__).resolve().parents[1]


def run(task_count: int = 2, knowledge_base: bool = False) -> dict:
    dotenv = parse_dotenv(ROOT / ".env")
    required = ("AGENT_TEST_EMAIL", "AGENT_TEST_PASSWORD", "AGENT_TEST_PROVIDER_ID",
                "AGENT_TEST_PROVIDER_API_KEY", "AGENT_TEST_MODEL_ID")
    values = {key: configured_value(key, dotenv) for key in required}
    require(all(values.values()), "missing AGENT_TEST_* configuration")
    flow = AgentUserFlow(Config(
        gateway=(configured_value("AGENT_TEST_GATEWAY", dotenv) or gateway_from_env_file()).rstrip("/"),
        email=values["AGENT_TEST_EMAIL"], password=values["AGENT_TEST_PASSWORD"],
        provider_id=values["AGENT_TEST_PROVIDER_ID"],
        provider_api_key=values["AGENT_TEST_PROVIDER_API_KEY"],
        model_id=values["AGENT_TEST_MODEL_ID"], thinking_level=None,
        request_timeout_seconds=60, run_timeout_seconds=600, poll_interval_seconds=2,
        keep_resources=False, report=None,
    ))
    result = {"status": "failed", "checks": [], "child_ids": [], "max_running": 0,
              "observed_queued": False, "matched_markers": 0, "busy_config_status": None}
    try:
        flow.login_and_preflight()
        flow.create_workspace()
        flow.create_binding_and_profile()
        knowledge_base_id = None
        if knowledge_base:
            kb = flow.json_request("POST",
                f"/workspaces/{flow.workspace_id}/knowledge-bases",
                expected=201, label="create-empty-knowledge-base",
                json={"name": f"subagent-e2e-{uuid4().hex[:10]}",
                      "file_backend": "postgresql", "block_backend": "postgresql",
                      "chunk_backend": "postgresql", "vector_backend": "postgresql",
                      "graph_backend": "postgresql"})
            knowledge_base_id = kb["id"]
        marker = uuid4().hex[:12]
        definitions = [
            {"name": "alpha", "description": "Returns the exact alpha marker.",
             "system_prompt": "Follow the user's task exactly. Respond with only the requested marker.",
             "tools": ["rag_search"] if knowledge_base else []},
            {"name": "beta", "description": "Returns the exact beta marker.",
             "system_prompt": "Follow the user's task exactly. Respond with only the requested marker.",
             "tools": ["rag_search"] if knowledge_base else []},
        ]
        session = flow.json_request("POST", "/sessions", expected=201, label="create-subagent-session",
            json={"profile_id": flow.profile_id, "tools": ["call_subagents"],
                  "subagents": definitions,
                  "knowledge_base_ids": [knowledge_base_id] if knowledge_base_id else []})
        flow.session_id = session["id"]
        require(session["config_version"] == 1, "incorrect initial config version")
        config = flow.json_request("GET", f"/sessions/{flow.session_id}/agent-config", label="get-agent-config")
        require(config["tools"] == ["call_subagents"] and len(config["subagents"]) == 2,
                "saved tool configuration differs")
        result["checks"].append("configuration")
        flow.request("PUT", f"/sessions/{flow.session_id}/agent-config", expected=409,
            label="reject-stale-config", json={"expected_config_version": 2,
            "tools": ["call_subagents"], "subagents": definitions})
        result["checks"].append("stale-version")
        task_specs = [
            {"name": "alpha" if index % 2 == 0 else "beta",
             "task": f"Reply with exactly RESULT_{marker}_{index:02d}"}
            for index in range(task_count)
        ]
        prompt = ("Call call_subagents exactly once with this JSON task list: "
                  f"{json.dumps(task_specs)}. Wait until every task finishes, then summarize all results.")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(flow.stream, prompt, label="call-subagents")
            with httpx.Client(timeout=10, trust_env=False) as observer:
                while not future.done():
                    response = observer.get(
                        flow.url(f"/sessions/{flow.session_id}/subagents"),
                        headers=flow.headers,
                    )
                    if response.status_code == 200:
                        statuses = [item["latest_run"]["status"] for item in response.json()["items"]]
                        result["max_running"] = max(result["max_running"], statuses.count("running"))
                        result["observed_queued"] |= "queued" in statuses
                        if "running" in statuses and result["busy_config_status"] is None:
                            busy = observer.put(
                                flow.url(f"/sessions/{flow.session_id}/agent-config"),
                                headers=flow.headers,
                                json={"expected_config_version": 1,
                                      "tools": ["call_subagents"], "subagents": definitions},
                            )
                            result["busy_config_status"] = busy.status_code
                    time.sleep(0.1)
            reply, events = future.result()
        names = [name for name, _ in events]
        require("message.completed" in names and "message.failed" not in names,
                f"parent did not complete: {names}")
        calls = [payload for name, payload in events
                 if name == "tool.started" and payload.get("toolName") == "call_subagents"]
        completions = [payload for name, payload in events
                       if name == "tool.completed" and payload.get("toolName") == "call_subagents"]
        require(len(calls) == len(completions) == 1, "expected one completed call_subagents invocation")
        result["checks"].append("real-tool-call")
        children = flow.json_request("GET", f"/sessions/{flow.session_id}/subagents",
            label="list-child-sessions")["items"]
        require(len(children) == task_count, f"expected {task_count} children, got {len(children)}")
        children.sort(key=lambda item: item["task_index"])
        require([item["subagent"]["name"] for item in children] ==
                [task["name"] for task in task_specs],
                "child task order differs")
        require(all(item["read_only"] and item["latest_run"]["status"] == "completed"
                    for item in children), "children are not completed and read-only")
        child_ids = [item["id"] for item in children]
        result["child_ids"] = child_ids
        parent_messages = flow.json_request("GET", f"/sessions/{flow.session_id}/messages",
            label="parent-messages")["items"]
        tool_rows = [item for item in parent_messages
                     if item["role"] == "tool_result" and item["tool_name"] == "call_subagents"]
        require(len(tool_rows) == 1, "parent tool result missing")
        require(all(child_id in tool_rows[0]["content"] for child_id in child_ids),
                "parent tool result omits child IDs")
        require(flow.config.provider_api_key not in json.dumps(parent_messages, ensure_ascii=False),
                "provider key leaked into parent trajectory")
        listed = flow.json_request("GET", "/sessions", label="list-main-sessions")["items"]
        require(flow.session_id in [item["id"] for item in listed], "parent missing from main list")
        require(all(child_id not in [item["id"] for item in listed] for child_id in child_ids),
                "child leaked into main list")
        for child, task in zip(children, task_specs, strict=True):
            child_id = child["id"]
            expected = task["task"].removeprefix("Reply with exactly ")
            detail = flow.json_request("GET", f"/sessions/{flow.session_id}/subagents/{child_id}",
                label="child-details")
            require(detail["id"] == child_id and
                    detail["tools"] == (["rag_search"] if knowledge_base else []),
                    "child config differs")
            require(detail["provider_binding_id"] == flow.binding_id and
                    detail["model_id"] == flow.config.model_id and
                    detail["latest_run"]["provider_binding_id"] == flow.binding_id and
                    detail["latest_run"]["model_id"] == flow.config.model_id,
                    "child did not inherit the parent run's model")
            require(detail["knowledge_base_ids"] ==
                    ([knowledge_base_id] if knowledge_base_id else []),
                    "child knowledge base binding differs")
            messages = flow.json_request("GET",
                f"/sessions/{flow.session_id}/subagents/{child_id}/messages",
                label="child-messages")["items"]
            require(flow.config.provider_api_key not in json.dumps(messages, ensure_ascii=False),
                    "provider key leaked into child trajectory")
            answers = [item["content"] for item in messages
                       if item["role"] == "assistant" and item["content"] != "[tool calls]"]
            require(answers, f"child answer missing for task {child['task_index']}")
            if any(expected in answer for answer in answers):
                result["matched_markers"] += 1
            elif task_count == 2:
                raise FlowError(f"child answer missing {expected}: {answers!r}")
            flow.request("POST", f"/sessions/{child_id}/messages:stream", expected=404,
                label="reject-child-message", json={"content": "continue"})
            flow.request("PUT", f"/sessions/{child_id}/agent-config", expected=404,
                label="reject-child-config", json={"expected_config_version": 1, "tools": []})
            flow.request("PUT", f"/sessions/{child_id}/model-config", expected=404,
                label="reject-child-model-config",
                json={"provider_binding_id": flow.binding_id, "model_id": flow.config.model_id})
            flow.request("PATCH", f"/sessions/{child_id}", expected=404,
                label="reject-child-title", json={"title": "cannot rename"})
            flow.request("POST", f"/sessions/{child_id}/abort", expected=404,
                label="reject-child-control")
            flow.request("DELETE", f"/sessions/{child_id}", expected=404,
                label="reject-child-delete")
        result["checks"].extend(["child-trajectory", "child-read-only",
                                 "main-list-isolation", "credential-redaction"])
        if knowledge_base:
            result["checks"].append("knowledge-base-inheritance")
        require(result["max_running"] <= 4, "more than four children ran concurrently")
        require(result["busy_config_status"] == 409,
                f"running config update was not rejected: {result['busy_config_status']}")
        result["checks"].append("busy-config-rejected")
        if task_count == 16:
            require(result["observed_queued"], "no queued child was observed")
            result["checks"].append("batch-16-queue-and-concurrency")
        else:
            require(all(task["task"].removeprefix("Reply with exactly ") in reply
                        for task in task_specs), "parent did not use both child outputs")
        updated_definitions = [{**item, "description": item["description"] + " Updated."}
                               for item in definitions]
        updated = flow.json_request("PUT", f"/sessions/{flow.session_id}/agent-config",
            label="update-idle-config", json={"expected_config_version": 1,
            "tools": ["call_subagents"], "subagents": updated_definitions})
        require(updated["config_version"] == 2, "config version was not advanced")
        unchanged_child = flow.json_request("GET",
            f"/sessions/{flow.session_id}/subagents/{child_ids[0]}",
            label="verify-definition-snapshot")
        require(unchanged_child["subagent"]["description"] == definitions[0]["description"],
                "old child definition snapshot changed")
        result["checks"].extend(["idle-config-update", "definition-snapshot"])
        if task_count == 2:
            followup_marker = f"UPDATED_{marker}"
            followup_reply, followup_events = flow.stream(
                "Call call_subagents exactly once with one task: "
                f"{{name: alpha, task: 'Reply exactly {followup_marker}'}}. "
                "Wait for the task, then return its reply.",
                label="call-subagents-after-config-update",
            )
            require(any(name == "tool.completed" and payload.get("toolName") == "call_subagents"
                        for name, payload in followup_events), "tool unavailable after config update")
            require(followup_marker in followup_reply, "updated session did not use child result")
            all_children = flow.json_request("GET", f"/sessions/{flow.session_id}/subagents",
                label="list-after-config-update")["items"]
            require(len(all_children) == 3, "follow-up child was not created")
            new_child = next(item for item in all_children if item["id"] not in child_ids)
            require(new_child["subagent"]["description"] ==
                    updated_definitions[0]["description"], "new child used stale definition")
            result["child_ids"].append(new_child["id"])
            result["checks"].append("runtime-config-rebuild")
        result["status"] = "passed"
    except (FlowError, KeyError, TypeError, ValueError) as exc:
        result["error"] = str(exc)
    finally:
        if flow.session_id:
            try:
                flow.request("DELETE", f"/sessions/{flow.session_id}", expected=204,
                    label="cleanup-parent-session")
                for child_id in result["child_ids"]:
                    flow.request("GET", f"/sessions/{flow.session_id}/subagents/{child_id}",
                        expected=404, label="verify-child-deleted")
                result["checks"].append("cascade-delete")
            except FlowError as exc:
                result.setdefault("cleanup_errors", []).append(str(exc))
        # Workspace deletion cascades its Binding and Profile.
        flow.profile_id = None
        flow.binding_id = None
        result.setdefault("cleanup_errors", []).extend(flow.cleanup())
        flow.close()
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, choices=(2, 16), default=2)
    parser.add_argument("--knowledge-base", action="store_true")
    args = parser.parse_args()
    outcome = run(args.tasks, args.knowledge_base)
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome["status"] == "passed" and not outcome["cleanup_errors"] else 1)
