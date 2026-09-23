#!/usr/bin/env python3
"""Black-box acceptance for step-by-step Agent execution visualization.

Covers requirement 8: 智能体执行过程可视化（工具调用、思考过程、中间结果）.

The script logs into a dedicated test account, creates an isolated Workspace,
Provider Binding, Agent Profile and Session, uploads a small text file, and asks
the Agent to read it through its tools.  It then verifies that the public SSE
stream and persisted history expose every step: assistant text deltas (thinking/
answer), tool calls with arguments, tool results with intermediate output, and a
terminal completion event.

Required environment variables (see test/agent_user_flow.py)::

    AGENT_TEST_EMAIL / AGENT_TEST_PASSWORD
    AGENT_TEST_PROVIDER_ID / AGENT_TEST_PROVIDER_API_KEY / AGENT_TEST_MODEL_ID

Run after the Gateway and its Docker Agent Runtime prerequisites are ready::

    uv run python test/交付要求/agent_visualization_flow.py --report /tmp/agent-visualization-flow.json

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
from agent_user_flow import (
    AgentUserFlow,
    Config as AgentConfig,
    FlowError,
    gateway_from_env_file,
    normalized_api_base,
    require,
)
from _delivery_log import log, log_agent_events, log_history

REPO_ROOT = Path(__file__).resolve().parents[2]


def upload_text_file(agent: AgentUserFlow) -> tuple[str, str]:
    require(agent.workspace_id is not None, "workspace is missing")
    marker = f"AGENT_VISUALIZATION_MARKER_{uuid4().hex}"
    path = f"e2e/visualization-{uuid4().hex[:10]}.txt"
    content = (f"# Agent visualization fixture\n\nThe marker is {marker}.\n"
               "Please read this file and report the marker.\n").encode()
    response = agent.json_request(
        "POST",
        f"/workspaces/{agent.workspace_id}/files",
        expected=201,
        label="upload-visualization-file",
        data={"path": path, "overwrite": "false"},
        files={"file": (Path(path).name, content, "text/plain")},
    )
    require(response.get("path") == path, "uploaded text file has incorrect path")
    log(f"[工作区文件上传] path={path} size_bytes={len(content)} marker={marker}")
    agent.resources["workspace_file_path"] = path
    return path, marker


def run(agent: AgentUserFlow) -> None:
    agent.login_and_preflight()
    log(f"[登录] user_id={agent.user_id} provider={agent.config.provider_id} model={agent.config.model_id} thinking_level={agent.config.thinking_level}")
    agent.create_workspace()
    log(f"[工作区] id={agent.workspace_id}")
    agent.create_binding_and_profile()
    log(f"[Provider 绑定] binding_id={agent.binding_id} provider_id={agent.config.provider_id}")
    log(f"[Agent Profile] profile_id={agent.profile_id} model_id={agent.config.model_id}")
    path, marker = upload_text_file(agent)
    agent.create_session()
    log(f"[会话] session_id={agent.session_id}")

    response_text, events = agent.stream(
        f"Read the Workspace file `{path}` using bash or the Python 3 standard library "
        f"(do not access the network) and report the marker. Then output exactly "
        f"VISUALIZATION_STEPS_OK.",
        label="visualize-agent-run",
    )
    log_agent_events(events, response_text)

    names = [name for name, _ in events]
    require(names and names[0] == "message.accepted",
            "SSE did not start with message.accepted")
    require("assistant.delta" in names,
            "SSE contained no assistant text deltas (thinking/answer steps)")
    require("message.failed" not in names, "Agent run emitted message.failed")
    require("message.completed" in names and names[-1] == "done",
            "SSE did not end with message.completed then done")

    # Tool-call steps must expose the tool name, arguments and intermediate result.
    started = [
        payload for name, payload in events if name == "tool.started"
    ]
    completed = [
        payload for name, payload in events if name == "tool.completed"
    ]
    require(started and completed, "SSE contained no tool steps to visualize")
    for payload in started:
        require(isinstance(payload.get("toolName"), str),
                f"tool.started lacks toolName: {payload!r}")
        require(isinstance(payload.get("toolCallId"), str),
                f"tool.started lacks toolCallId: {payload!r}")
        require(isinstance(payload.get("args"), dict),
                f"tool.started lacks arguments: {payload!r}")
    for payload in completed:
        require(isinstance(payload.get("toolName"), str),
                f"tool.completed lacks toolName: {payload!r}")
        require("result" in payload, f"tool.completed lacks result: {payload!r}")

    # Ordering invariant: each tool call starts before it completes in the stream.
    started_ids = {payload.get("toolCallId") for payload in started}
    completed_ids = {payload.get("toolCallId") for payload in completed}
    require(started_ids and started_ids <= completed_ids,
            "not every started tool call produced a matching completion")
    positions: dict[str, dict[str, int]] = {}
    for index, (name, payload) in enumerate(events):
        call_id = payload.get("toolCallId")
        if name == "tool.started" and isinstance(call_id, str):
            positions.setdefault(call_id, {})["started"] = index
        elif name == "tool.completed" and isinstance(call_id, str):
            positions.setdefault(call_id, {})["completed"] = index
    for call_id, marks in positions.items():
        require(marks.get("started") is not None and marks.get("completed") is not None,
                f"tool call {call_id} lacks a start or completion event")
        require(marks["started"] < marks["completed"],
                f"tool call {call_id} completed before it started")

    require(marker in response_text,
            "Agent response lacks the file marker (intermediate tool output not used)")
    require("VISUALIZATION_STEPS_OK" in response_text,
            "Agent response lacks the terminal confirmation")

    # Persisted history must retain the same step-by-step projections.
    items = agent.history(label="get-visualization-history")
    roles = [item.get("role") for item in items]
    require("user" in roles and "assistant" in roles,
            "history lacks user or assistant projection")
    tool_calls = [item for item in items if item.get("role") == "tool_call"]
    tool_results = [item for item in items if item.get("role") == "tool_result"]
    require(tool_calls and tool_results, "history lacks tool_call or tool_result rows")
    require(all(isinstance(item.get("arguments"), (dict, list, type(None)))
                for item in tool_calls),
            "tool_call history rows have invalid arguments")
    require(all("result" in item or item.get("is_error") is True
                for item in tool_results),
            "tool_result history rows lack intermediate results")
    require(any(marker in str(item.get("content", "")) for item in items),
            "history lacks the marker reported from tool output")
    log_history(items)


def parse_args() -> AgentConfig:
    dotenv = parse_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway",
        default=configured_value("AGENT_TEST_GATEWAY", dotenv) or gateway_from_env_file(),
    )
    parser.add_argument("--email", default=configured_value("AGENT_TEST_EMAIL", dotenv))
    parser.add_argument(
        "--provider-id", default=configured_value("AGENT_TEST_PROVIDER_ID", dotenv)
    )
    parser.add_argument(
        "--model-id", default=configured_value("AGENT_TEST_MODEL_ID", dotenv)
    )
    parser.add_argument(
        "--thinking-level", default=configured_value("AGENT_TEST_THINKING_LEVEL", dotenv)
    )
    parser.add_argument("--request-timeout-seconds", type=float, default=60)
    parser.add_argument("--run-timeout-seconds", type=float, default=300)
    parser.add_argument("--poll-interval-seconds", type=float, default=1)
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    for value, name in (
        (args.email, "AGENT_TEST_EMAIL"),
        (args.provider_id, "AGENT_TEST_PROVIDER_ID"),
        (args.model_id, "AGENT_TEST_MODEL_ID"),
    ):
        require(bool(value), f"required test configuration is missing: {name}")
    for value in (args.request_timeout_seconds, args.run_timeout_seconds, args.poll_interval_seconds):
        require(value > 0, "timeouts and poll interval must be positive")

    config = AgentConfig(
        gateway=normalized_api_base(args.gateway),
        email=args.email,
        password=configured_value("AGENT_TEST_PASSWORD", dotenv) or "",
        provider_id=args.provider_id,
        provider_api_key=configured_value("AGENT_TEST_PROVIDER_API_KEY", dotenv) or "",
        model_id=args.model_id,
        thinking_level=args.thinking_level,
        request_timeout_seconds=args.request_timeout_seconds,
        run_timeout_seconds=args.run_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        keep_resources=args.keep_resources,
        report=args.report.resolve() if args.report else None,
    )
    require(bool(config.password), "required test configuration is missing: AGENT_TEST_PASSWORD")
    require(bool(config.provider_api_key), "required test configuration is missing: AGENT_TEST_PROVIDER_API_KEY")
    return config


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
    agent = AgentUserFlow(config)
    failure: Exception | None = None
    cleanup_errors: list[str] = []
    try:
        run(agent)
    except Exception as exc:
        failure = exc
    finally:
        try:
            cleanup_errors = agent.cleanup()
        finally:
            status = "passed" if failure is None and not cleanup_errors else "failed"
            report = agent.report(status, failure, cleanup_errors)
            if config.report:
                config.report.parent.mkdir(parents=True, exist_ok=True)
                config.report.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                print(f"Report: {config.report}")
            agent.close()
    if failure is not None:
        print(f"FAIL: {failure}", file=sys.stderr)
        if agent.resources:
            print(f"Created resources: {json.dumps(agent.resources, ensure_ascii=False)}",
                  file=sys.stderr)
        return 1
    if cleanup_errors:
        print("FAIL: cleanup did not complete:", file=sys.stderr)
        for error in cleanup_errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("PASS: Agent execution visualization (tool calls, thinking, intermediate results) completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
