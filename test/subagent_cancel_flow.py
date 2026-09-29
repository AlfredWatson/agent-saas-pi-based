#!/usr/bin/env python3
"""Public HTTP cancellation smoke test for a queued 16-task subagent batch."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx

from agent_user_flow import AgentUserFlow, Config, FlowError, gateway_from_env_file, require
from flow_env import configured_value, parse_dotenv


def run() -> dict:
    dotenv = parse_dotenv(Path(__file__).resolve().parents[1] / ".env")
    def setting(name: str) -> str:
        value = configured_value(name, dotenv)
        require(bool(value), f"missing {name}")
        return value

    flow = AgentUserFlow(Config(
        gateway=(configured_value("AGENT_TEST_GATEWAY", dotenv) or gateway_from_env_file()).rstrip("/"),
        email=setting("AGENT_TEST_EMAIL"), password=setting("AGENT_TEST_PASSWORD"),
        provider_id=setting("AGENT_TEST_PROVIDER_ID"),
        provider_api_key=setting("AGENT_TEST_PROVIDER_API_KEY"),
        model_id=setting("AGENT_TEST_MODEL_ID"), thinking_level=None,
        request_timeout_seconds=60, run_timeout_seconds=120, poll_interval_seconds=0.1,
        keep_resources=False, report=None,
    ))
    result = {"status": "failed", "cancel_status": None, "children": 0,
              "cancelled": 0, "stream_finished": False, "cleanup_errors": []}
    stream_error: list[str] = []
    try:
        flow.login_and_preflight()
        flow.create_workspace()
        flow.create_binding_and_profile()
        definition = {"name": "worker", "description": "Returns a short answer.",
                      "system_prompt": "Respond to the user task with a short answer.",
                      "tools": []}
        session = flow.json_request("POST", "/sessions", expected=201, label="create-cancel-session",
            json={"profile_id": flow.profile_id, "tools": ["call_subagents"],
                  "subagents": [definition]})
        flow.session_id = session["id"]
        marker = uuid4().hex[:10]
        tasks = [{"name": "worker", "task": f"Reply exactly CANCEL_{marker}_{index:02d}"}
                 for index in range(16)]
        prompt = ("Call call_subagents exactly once with these tasks: "
                  f"{json.dumps(tasks)}. Wait for the results.")
        def stream() -> None:
            try:
                flow.stream(prompt, label="cancel-parent-stream")
            except Exception as exc:
                stream_error.append(str(exc))
        thread = threading.Thread(target=stream, daemon=True)
        thread.start()
        deadline = time.monotonic() + 45
        with httpx.Client(timeout=10, trust_env=False) as client:
            while time.monotonic() < deadline:
                response = client.get(flow.url(f"/sessions/{flow.session_id}/subagents"),
                                      headers=flow.headers)
                require(response.status_code == 200, "cannot list children")
                children = response.json()["items"]
                statuses = [item["latest_run"]["status"] for item in children]
                if len(children) == 16 and "running" in statuses and "queued" in statuses:
                    result["children"] = 16
                    aborted = client.post(flow.url(f"/sessions/{flow.session_id}/abort"),
                                          headers=flow.headers)
                    result["cancel_status"] = aborted.status_code
                    require(aborted.status_code == 202, f"abort returned {aborted.status_code}")
                    break
                time.sleep(0.1)
            else:
                raise FlowError("did not observe running and queued children")
            thread.join(timeout=60)
            result["stream_finished"] = not thread.is_alive()
            require(result["stream_finished"], "parent stream remained open after abort")
            require(not stream_error, f"parent stream failed: {stream_error}")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                response = client.get(flow.url(f"/sessions/{flow.session_id}/subagents"),
                                      headers=flow.headers)
                children = response.json()["items"]
                statuses = [item["latest_run"]["status"] for item in children]
                if len(children) == 16 and not ({"running", "queued"} & set(statuses)):
                    result["cancelled"] = statuses.count("cancelled")
                    require(result["cancelled"] >= 1, "no child was cancelled")
                    result["status"] = "passed"
                    break
                time.sleep(0.1)
            else:
                raise FlowError("children remained running or queued after abort")
    except (FlowError, KeyError, ValueError, httpx.HTTPError) as exc:
        result["error"] = str(exc)
    finally:
        if flow.session_id:
            try:
                flow.request("DELETE", f"/sessions/{flow.session_id}", expected=204,
                             label="cleanup-cancel-session")
            except FlowError as exc:
                result["cleanup_errors"].append(str(exc))
        flow.profile_id = None
        flow.binding_id = None
        result["cleanup_errors"].extend(flow.cleanup())
        flow.close()
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    outcome = run()
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome["status"] == "passed" and not outcome["cleanup_errors"] else 1)
