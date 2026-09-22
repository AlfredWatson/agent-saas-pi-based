#!/usr/bin/env python3
"""Black-box acceptance for Agent session history persistence and export.

Covers requirement 4: 对话上下文持久化、历史回溯与用户会话导出.

The script logs into a dedicated test account, creates an isolated Workspace,
Provider Binding, Agent Profile and Session, streams one message, then fetches the
full public message projection and writes it (together with session metadata) to a
local JSON export file.  It verifies the export is valid JSON, contains the user and
assistant messages, and exposes no configured secrets.

The Gateway has no dedicated one-shot export endpoint; export is the client-side
serialization of ``GET /sessions/{id}`` and ``GET /sessions/{id}/messages``.

Required environment variables (see test/agent_user_flow.py)::

    AGENT_TEST_EMAIL / AGENT_TEST_PASSWORD
    AGENT_TEST_PROVIDER_ID / AGENT_TEST_PROVIDER_API_KEY / AGENT_TEST_MODEL_ID

Run after the Gateway and its Docker Agent Runtime prerequisites are ready::

    uv run python test/交付要求/session_export_flow.py --report /tmp/session-export-flow.json

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


def run(agent: AgentUserFlow, export_path: Path) -> None:
    marker = f"SESSION_EXPORT_OK_{uuid4().hex[:12]}"
    agent.login_and_preflight()
    log(f"[登录] user_id={agent.user_id} provider={agent.config.provider_id} model={agent.config.model_id} thinking_level={agent.config.thinking_level}")
    agent.create_workspace()
    log(f"[工作区] id={agent.workspace_id}")
    agent.create_binding_and_profile()
    log(f"[Provider 绑定] binding_id={agent.binding_id} provider_id={agent.config.provider_id}")
    log(f"[Agent Profile] profile_id={agent.profile_id} model_id={agent.config.model_id} thinking_level={agent.config.thinking_level}")
    agent.create_session()
    log(f"[会话] session_id={agent.session_id}")

    response_text, events = agent.stream(
        f"Reply exactly with {marker}.", label="stream-session-export"
    )
    require(marker in response_text,
            f"Agent response lacks the export marker: {response_text!r}")
    require(any(name == "message.completed" for name, _ in events),
            "Agent run did not complete")
    log_agent_events(events, response_text)

    # Persisted history is the export source of truth.
    session = agent.json_request(
        "GET", f"/sessions/{agent.session_id}", label="get-session-metadata"
    )
    require(isinstance(session.get("id"), str), "session metadata lacks id")
    log(f"[会话元数据] session_id={session.get('id')} status={session.get('status')} title={session.get('title')!r} knowledge_base_ids={session.get('knowledge_base_ids')}")
    messages = agent.history(label="get-session-history")
    require(messages, "session history is empty")
    log_history(messages)
    roles = [item.get("role") for item in messages]
    require("user" in roles and "assistant" in roles,
            f"history lacks user or assistant projection: {roles!r}")
    require(
        [item.get("sequence") for item in messages]
        == sorted(item.get("sequence") for item in messages),
        "session history sequence is not ordered",
    )
    require(any(marker in str(item.get("content", "")) for item in messages),
            "session history lacks the assistant marker reply")

    export = {"session": session, "messages": messages}
    serialized = json.dumps(export, ensure_ascii=False, indent=2, sort_keys=True)
    for secret in agent._secret_values:
        require(secret not in serialized, "session export exposed a configured secret")
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_text(serialized + "\n", encoding="utf-8")
    log(f"[会话导出] 已写出 {export_path}（session + {len(messages)} 条消息）")

    # Round-trip the export file to prove it is valid, complete JSON.
    reloaded = json.loads(export_path.read_text(encoding="utf-8"))
    require(reloaded.get("session", {}).get("id") == session.get("id"),
            "exported session metadata is not round-trip safe")
    require(len(reloaded.get("messages", [])) == len(messages),
            "exported message count changed after round-trip")
    log(f"[导出校验] 回读成功：session_id={reloaded['session']['id']} messages={len(reloaded['messages'])}")
    agent.resources["session_export_file"] = str(export_path)
    agent.resources["session_export_message_count"] = len(messages)


def parse_args() -> tuple[AgentConfig, Path]:
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
    parser.add_argument(
        "--export-file",
        type=Path,
        help="Where to write the exported session JSON; defaults to <report>.export.json.",
    )
    args = parser.parse_args()

    for value, name in (
        (args.email, "AGENT_TEST_EMAIL"),
        (args.provider_id, "AGENT_TEST_PROVIDER_ID"),
        (args.model_id, "AGENT_TEST_MODEL_ID"),
    ):
        require(bool(value), f"required test configuration is missing: {name}")
    for value in (args.request_timeout_seconds, args.run_timeout_seconds, args.poll_interval_seconds):
        require(value > 0, "timeouts and poll interval must be positive")

    export_path = args.export_file
    if export_path is None:
        base = args.report.resolve() if args.report else Path("/tmp/session-export.json")
        export_path = base.with_suffix(".export.json")

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
    return config, export_path.resolve()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    try:
        config, export_path = parse_args()
    except FlowError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    agent = AgentUserFlow(config)
    failure: Exception | None = None
    cleanup_errors: list[str] = []
    try:
        run(agent, export_path)
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
    print(f"PASS: session export completed; export file: {export_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
