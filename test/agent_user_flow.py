#!/usr/bin/env python3
"""Black-box acceptance for the real-model Agent user flow.

The flow only calls the public Gateway API. It logs into a dedicated existing
test account, creates an isolated Workspace/Binding/Profile/Session, uploads a
generated XLSX file, and proves that the Agent read it through its tool path.

Required environment variables:

    AGENT_TEST_EMAIL
    AGENT_TEST_PASSWORD
    AGENT_TEST_PROVIDER_ID
    AGENT_TEST_PROVIDER_API_KEY
    AGENT_TEST_MODEL_ID

Run after the Gateway and its Docker Agent Runtime prerequisites are ready::

    uv run python test/agent_user_flow.py --report /tmp/agent-user-flow.json

Resources are cleaned up by default. ``--keep-resources`` retains them for
inspection if a real-provider failure needs investigation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "ciphertext",
    "nonce",
    "password",
    "token",
}


class FlowError(RuntimeError):
    """An HTTP request or assertion in the Agent acceptance flow failed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FlowError(message)


def parse_dotenv(path: Path) -> dict[str, str]:
    """Read just enough dotenv syntax to resolve the local Gateway URL."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#") or "=" not in candidate:
            continue
        key, value = candidate.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def gateway_from_env_file() -> str:
    values = parse_dotenv(ROOT / ".env")
    host = values.get("GATEWAY_HOST", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = values.get("GATEWAY_PORT", "8000")
    return f"http://{host}:{port}/api/v1"


def normalized_api_base(url: str) -> str:
    return url.rstrip("/")


@dataclass(frozen=True)
class Config:
    gateway: str
    email: str
    password: str
    provider_id: str
    provider_api_key: str
    model_id: str
    thinking_level: str | None
    request_timeout_seconds: float
    run_timeout_seconds: float
    poll_interval_seconds: float
    keep_resources: bool
    report: Path | None


class AgentUserFlow:
    def __init__(self, config: Config):
        self.config = config
        self.client = httpx.Client(
            timeout=config.request_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )
        self.token: str | None = None
        self.user_id: str | None = None
        self.workspace_id: str | None = None
        self.binding_id: str | None = None
        self.profile_id: str | None = None
        self.session_id: str | None = None
        self.events: list[dict[str, Any]] = []
        self.resources: dict[str, Any] = {}
        self._secret_values = {
            value for value in (config.password, config.provider_api_key) if value
        }

    @property
    def headers(self) -> dict[str, str]:
        require(self.token is not None, "request attempted before login")
        return {"Authorization": f"Bearer {self.token}"}

    def close(self) -> None:
        self.client.close()

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "***" if key.casefold() in SENSITIVE_KEYS else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            redacted = value
            for secret in self._secret_values:
                redacted = redacted.replace(secret, "***")
            return redacted
        return value

    def event(self, name: str, **details: Any) -> None:
        self.events.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "name": name,
                "details": self.redact(details),
            }
        )

    def url(self, path: str) -> str:
        return f"{self.config.gateway}{path}"

    def response_body(self, response: httpx.Response) -> Any:
        if not response.content:
            return None
        try:
            return self.redact(response.json())
        except ValueError:
            return self.redact(response.text[:2_000])

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: int | tuple[int, ...] = 200,
        label: str,
        authenticated: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        expected_statuses = (expected,) if isinstance(expected, int) else expected
        headers = dict(kwargs.pop("headers", {}))
        if authenticated:
            headers = {**self.headers, **headers}
        started = time.monotonic()
        try:
            response = self.client.request(
                method, self.url(path), headers=headers, **kwargs
            )
        except httpx.RequestError as exc:
            self.event(
                label, method=method, path=path, error=f"{type(exc).__name__}: {exc}"
            )
            raise FlowError(f"{label}: cannot reach {self.url(path)}: {exc}") from exc
        self.event(
            label,
            method=method,
            path=path,
            status_code=response.status_code,
            elapsed_ms=round((time.monotonic() - started) * 1_000, 2),
            response=self.response_body(response),
        )
        if response.status_code not in expected_statuses:
            raise FlowError(
                f"{label}: expected HTTP {expected_statuses}, got {response.status_code}; "
                f"response={self.response_body(response)!r}"
            )
        return response

    def json_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        response = self.request(*args, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise FlowError(
                f"{kwargs.get('label', 'request')}: expected JSON response"
            ) from exc
        require(isinstance(payload, dict), "expected JSON object response")
        return payload

    def login_and_preflight(self) -> None:
        login = self.json_request(
            "POST",
            "/auth/login",
            authenticated=False,
            label="login",
            json={"email": self.config.email, "password": self.config.password},
        )
        token = login.get("access_token")
        require(
            isinstance(token, str) and token,
            "login response did not include access_token",
        )
        self.token = token
        me = self.json_request("GET", "/auth/me", label="get-current-user")
        user_id = me.get("id")
        require(
            isinstance(user_id, str) and user_id,
            "current user response did not include id",
        )
        self.user_id = user_id

        status = self.json_request("GET", "/runtime", label="get-runtime-status")
        require(isinstance(status.get("state"), str), "runtime status lacks state")
        providers = self.json_request("GET", "/providers", label="list-providers").get(
            "providers"
        )
        require(isinstance(providers, list), "provider response has invalid providers")
        require(
            any(
                isinstance(provider, dict)
                and provider.get("id") == self.config.provider_id
                for provider in providers
            ),
            f"configured provider {self.config.provider_id!r} is unavailable",
        )

    def create_workspace(self) -> None:
        suffix = uuid4().hex[:10]
        workspace = self.json_request(
            "POST",
            "/workspaces",
            expected=201,
            label="create-workspace",
            json={"name": f"agent-e2e-{suffix}"},
        )
        workspace_id = workspace.get("id")
        require(isinstance(workspace_id, str), "workspace creation did not return id")
        self.workspace_id = workspace_id
        self.resources["workspace_id"] = workspace_id
        switched = self.json_request(
            "POST", f"/workspaces/{workspace_id}:switch", label="switch-workspace"
        )
        require(
            switched.get("id") == workspace_id and switched.get("is_current") is True,
            "workspace switch did not make the created workspace current",
        )

    def create_binding_and_profile(self) -> None:
        suffix = uuid4().hex[:10]
        binding = self.json_request(
            "POST",
            "/provider-bindings",
            expected=201,
            label="create-provider-binding",
            json={
                "provider_id": self.config.provider_id,
                "display_name": f"agent-e2e-{suffix}",
                "api_key": self.config.provider_api_key,
            },
        )
        binding_id = binding.get("id")
        require(isinstance(binding_id, str), "binding creation did not return id")
        self.binding_id = binding_id
        self.resources["binding_id"] = binding_id
        bindings = self.json_request(
            "GET", "/provider-bindings", label="list-provider-bindings"
        ).get("items")
        require(
            isinstance(bindings, list)
            and any(
                isinstance(item, dict) and item.get("id") == binding_id
                for item in bindings
            ),
            "created binding is absent from list",
        )
        models = self.json_request(
            "GET",
            f"/provider-bindings/{binding_id}/models",
            label="list-provider-models",
        ).get("models")
        require(isinstance(models, list), "model response has invalid models")
        model = next(
            (
                item
                for item in models
                if isinstance(item, dict) and item.get("id") == self.config.model_id
            ),
            None,
        )
        require(
            model is not None,
            f"configured model {self.config.model_id!r} is unavailable",
        )
        if self.config.thinking_level:
            require(
                self.config.thinking_level in model.get("thinking_levels", []),
                f"thinking level {self.config.thinking_level!r} is unavailable for configured model",
            )
        profile = self.json_request(
            "POST",
            "/agent-profiles",
            expected=201,
            label="create-agent-profile",
            json={
                "name": f"agent-e2e-{suffix}",
                "provider_binding_id": binding_id,
                "model_id": self.config.model_id,
                "thinking_level": self.config.thinking_level,
            },
        )
        profile_id = profile.get("id")
        require(isinstance(profile_id, str), "profile creation did not return id")
        self.profile_id = profile_id
        self.resources["profile_id"] = profile_id
        profiles = self.json_request(
            "GET", "/agent-profiles", label="list-agent-profiles"
        ).get("items")
        require(
            isinstance(profiles, list)
            and any(
                isinstance(item, dict) and item.get("id") == profile_id
                for item in profiles
            ),
            "created profile is absent from list",
        )

    def workbook(self) -> tuple[bytes, dict[str, dict[str, str]]]:
        marker = f"agent-user-flow-{uuid4().hex}"
        workbook = Workbook()
        summary = workbook.active
        summary.title = "E2E Summary"
        summary["A1"] = "marker"
        summary["B1"] = marker
        summary["A2"] = "status"
        summary["B2"] = "verified"
        data = workbook.create_sheet("E2E Data")
        data["A1"] = "record"
        data["B1"] = "value"
        data["A2"] = "agent-check"
        data["B2"] = "42"
        buffer = BytesIO()
        workbook.save(buffer)
        return buffer.getvalue(), {
            "E2E Summary": {"B1": marker, "B2": "verified"},
            "E2E Data": {"A2": "agent-check", "B2": "42"},
        }

    def upload_and_verify_file(self) -> tuple[str, str, dict[str, dict[str, str]]]:
        require(self.workspace_id is not None, "workspace is missing")
        content, expected_cells = self.workbook()
        digest = hashlib.sha256(content).hexdigest()
        path = f"e2e/agent-user-flow-{uuid4().hex[:10]}.xlsx"
        response = self.json_request(
            "POST",
            f"/workspaces/{self.workspace_id}/files",
            expected=201,
            label="upload-workbook",
            data={"path": path, "overwrite": "false"},
            files={
                "file": (
                    Path(path).name,
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        require(
            response.get("path") == path and response.get("size_bytes") == len(content),
            "uploaded workbook metadata is incorrect",
        )
        files = self.json_request(
            "GET",
            f"/workspaces/{self.workspace_id}/files",
            label="list-workspace-files",
        ).get("items")
        require(
            isinstance(files, list)
            and any(
                isinstance(item, dict)
                and item.get("path") == path
                and item.get("size_bytes") == len(content)
                for item in files
            ),
            "uploaded workbook is absent from workspace file list",
        )
        self.request(
            "POST",
            f"/workspaces/{self.workspace_id}/files",
            expected=409,
            label="reject-duplicate-workbook",
            data={"path": path, "overwrite": "false"},
            files={
                "file": (
                    Path(path).name,
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        overwritten = self.json_request(
            "POST",
            f"/workspaces/{self.workspace_id}/files",
            label="overwrite-workbook",
            data={"path": path, "overwrite": "true"},
            files={
                "file": (
                    Path(path).name,
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        require(
            overwritten.get("path") == path,
            "workbook overwrite returned incorrect path",
        )
        self.resources["workspace_file_path"] = path
        self.resources["workbook_sha256"] = digest
        return path, digest, expected_cells

    def create_session(self) -> None:
        require(
            self.profile_id is not None and self.workspace_id is not None,
            "profile or workspace is missing",
        )
        session = self.json_request(
            "POST",
            "/sessions",
            expected=201,
            label="create-session-from-current-workspace",
            json={"profile_id": self.profile_id},
        )
        session_id = session.get("id")
        require(isinstance(session_id, str), "session creation did not return id")
        self.session_id = session_id
        self.resources["session_id"] = session_id
        session_ids = self.json_request(
            "GET",
            f"/workspaces/{self.workspace_id}/sessions",
            label="list-workspace-sessions",
        ).get("session_ids")
        require(
            isinstance(session_ids, list) and session_id in session_ids,
            "session is absent from workspace",
        )

    def stream(
        self, content: str, *, label: str
    ) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
        require(self.session_id is not None, "session is missing")
        events: list[tuple[str, dict[str, Any]]] = []
        text: list[str] = []
        event_name: str | None = None
        timeout = httpx.Timeout(
            connect=10.0,
            read=None,
            write=self.config.request_timeout_seconds,
            pool=self.config.request_timeout_seconds,
        )
        started = time.monotonic()
        try:
            with self.client.stream(
                "POST",
                self.url(f"/sessions/{self.session_id}/messages:stream"),
                headers=self.headers,
                json={"content": content},
                timeout=timeout,
            ) as response:
                self.event(label, status_code=response.status_code)
                if response.status_code != 200:
                    raise FlowError(
                        f"{label}: expected HTTP 200, got {response.status_code}; response={self.response_body(response)!r}"
                    )
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        event_name = line.removeprefix("event: ")
                    elif line.startswith("data: ") and event_name:
                        try:
                            payload = json.loads(line.removeprefix("data: "))
                        except json.JSONDecodeError as exc:
                            raise FlowError(
                                f"{label}: invalid SSE data for {event_name}"
                            ) from exc
                        require(
                            isinstance(payload, dict),
                            f"{label}: SSE payload is not an object",
                        )
                        events.append((event_name, payload))
                        if event_name == "assistant.delta":
                            delta = payload.get("delta")
                            require(
                                isinstance(delta, str),
                                f"{label}: invalid assistant delta",
                            )
                            text.append(delta)
                        event_name = None
        except httpx.RequestError as exc:
            raise FlowError(f"{label}: SSE request failed: {exc}") from exc
        self.event(
            f"{label}-completed",
            elapsed_ms=round((time.monotonic() - started) * 1_000, 2),
            event_names=[name for name, _ in events],
        )
        return "".join(text), events

    def assert_workbook_run(
        self,
        response_text: str,
        events: list[tuple[str, dict[str, Any]]],
        digest: str,
        expected_cells: dict[str, dict[str, str]],
    ) -> None:
        names = [name for name, _ in events]
        require(
            names and names[0] == "message.accepted",
            "SSE did not start with message.accepted",
        )
        require("assistant.delta" in names, "SSE contained no assistant text")
        require("message.failed" not in names, "Agent run emitted message.failed")
        require(
            "message.completed" in names and names[-1] == "done",
            "SSE did not complete with message.completed then done",
        )
        started = {
            payload.get("toolCallId")
            for name, payload in events
            if name == "tool.started" and isinstance(payload.get("toolCallId"), str)
        }
        completed = {
            payload.get("toolCallId")
            for name, payload in events
            if name == "tool.completed" and isinstance(payload.get("toolCallId"), str)
        }
        require(
            started and started <= completed,
            "Agent did not complete every observed tool call",
        )
        require(
            digest in response_text.lower(),
            "Agent response lacks the uploaded workbook SHA-256",
        )
        require(
            "WORKBOOK_READ_OK" in response_text, "Agent response lacks WORKBOOK_READ_OK"
        )
        for sheet, cells in expected_cells.items():
            require(sheet in response_text, f"Agent response lacks sheet {sheet!r}")
            for cell, value in cells.items():
                require(
                    value in response_text, f"Agent response lacks {sheet}!{cell} value"
                )

    def history(self, *, label: str) -> list[dict[str, Any]]:
        require(self.session_id is not None, "session is missing")
        items = self.json_request(
            "GET", f"/sessions/{self.session_id}/messages", label=label
        ).get("items")
        require(
            isinstance(items, list) and all(isinstance(item, dict) for item in items),
            "invalid message history",
        )
        return items

    def assert_history(self, digest: str) -> int:
        items = self.history(label="get-message-history")
        require(
            [item.get("sequence") for item in items]
            == sorted(item.get("sequence") for item in items),
            "message history sequence is not ordered",
        )
        roles = [item.get("role") for item in items]
        require(
            "user" in roles and "assistant" in roles,
            "message history lacks user or assistant projection",
        )
        tool_calls = [item for item in items if item.get("role") == "tool_call"]
        tool_results = [item for item in items if item.get("role") == "tool_result"]
        require(tool_calls and tool_results, "message history lacks tool projections")
        call_ids = {item.get("tool_call_id") for item in tool_calls}
        result_ids = {item.get("tool_call_id") for item in tool_results}
        require(call_ids <= result_ids, "tool result history does not match tool calls")
        require(
            any(
                digest in str(item.get("content", "")).lower()
                for item in items
                if item.get("role") == "assistant"
            ),
            "history lacks workbook verification response",
        )
        serialized = json.dumps(items, ensure_ascii=False)
        for secret in self._secret_values:
            require(
                secret not in serialized, "message history exposed a configured secret"
            )
        return len(items)

    def verify_disconnect_persistence(self, history_count: int) -> None:
        require(self.session_id is not None, "session is missing")
        marker = f"PERSISTED_AFTER_DISCONNECT_{uuid4().hex[:12]}"
        timeout = httpx.Timeout(
            connect=10.0,
            read=None,
            write=self.config.request_timeout_seconds,
            pool=self.config.request_timeout_seconds,
        )
        try:
            with self.client.stream(
                "POST",
                self.url(f"/sessions/{self.session_id}/messages:stream"),
                headers=self.headers,
                json={"content": f"Reply exactly with {marker}."},
                timeout=timeout,
            ) as response:
                self.event("start-disconnected-run", status_code=response.status_code)
                require(
                    response.status_code == 200, "disconnected run was not accepted"
                )
                first_line = next(response.iter_lines(), "")
                require(
                    first_line == "event: message.accepted",
                    "disconnected run did not begin with message.accepted",
                )
        except httpx.RequestError as exc:
            raise FlowError(f"disconnected run failed: {exc}") from exc

        deadline = time.monotonic() + self.config.run_timeout_seconds
        last: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            last = self.history(label="poll-disconnected-history")
            if len(last) > history_count and any(
                marker in str(item.get("content", ""))
                for item in last
                if item.get("role") == "assistant"
            ):
                self.event("disconnected-run-persisted", history_count=len(last))
                return
            time.sleep(self.config.poll_interval_seconds)
        raise FlowError(
            f"SSE disconnect did not persist the requested follow-up; last_history={self.redact(last)!r}"
        )

    def delete_uploaded_file(self) -> None:
        require(self.workspace_id is not None, "workspace is missing")
        path = self.resources.get("workspace_file_path")
        require(isinstance(path, str), "workbook path is missing")
        self.request(
            "DELETE",
            f"/workspaces/{self.workspace_id}/files",
            expected=204,
            label="delete-workbook",
            params={"path": path},
        )
        files = self.json_request(
            "GET",
            f"/workspaces/{self.workspace_id}/files",
            label="verify-workbook-deleted",
        ).get("items")
        require(
            isinstance(files, list)
            and all(
                item.get("path") != path for item in files if isinstance(item, dict)
            ),
            "workbook remains after deletion",
        )

    def run(self) -> None:
        self.login_and_preflight()
        self.create_workspace()
        self.create_binding_and_profile()
        _, digest, expected_cells = self.upload_and_verify_file()
        self.create_session()
        prompt = self.workbook_prompt(
            self.resources["workspace_file_path"], expected_cells
        )
        response_text, events = self.stream(prompt, label="read-workbook-with-agent")
        self.assert_workbook_run(response_text, events, digest, expected_cells)
        history_count = self.assert_history(digest)
        self.verify_disconnect_persistence(history_count)
        self.delete_uploaded_file()

    @staticmethod
    def workbook_prompt(path: str, expected_cells: dict[str, dict[str, str]]) -> str:
        lines = [
            f"请只读取当前 Workspace 中的 `{path}`。",
            "使用 bash 和 Python 3 标准库（例如 zipfile 与 xml.etree）解析 XLSX；不要访问网络，也不要猜测内容。",
            "请报告工作表名称及以下单元格的实际值：",
        ]
        for sheet, cells in expected_cells.items():
            for cell in cells:
                lines.append(f"- {sheet}!{cell}")
        lines.extend(
            [
                "最后必须逐行输出：",
                "WORKBOOK_SHA256: <该文件的 sha256sum>",
                "SHEET: <每个工作表名称>（每个工作表各一行）",
                "WORKBOOK_READ_OK",
            ]
        )
        return "\n".join(lines)

    def cleanup(self) -> list[str]:
        if self.config.keep_resources:
            self.event("cleanup-skipped", resources=self.resources)
            return []
        errors: list[str] = []
        if self.workspace_id:
            try:
                self.request(
                    "DELETE",
                    f"/workspaces/{self.workspace_id}",
                    expected=204,
                    label="cleanup-delete-workspace",
                )
            except Exception as exc:
                errors.append(f"workspace {self.workspace_id}: {exc}")
        if self.profile_id:
            try:
                self.request(
                    "DELETE",
                    f"/agent-profiles/{self.profile_id}",
                    expected=204,
                    label="cleanup-delete-profile",
                )
            except Exception as exc:
                errors.append(f"profile {self.profile_id}: {exc}")
        if self.binding_id:
            try:
                self.request(
                    "DELETE",
                    f"/provider-bindings/{self.binding_id}",
                    expected=204,
                    label="cleanup-disable-provider-binding",
                )
            except Exception as exc:
                errors.append(f"binding {self.binding_id}: {exc}")
        if errors:
            self.event("cleanup-failed", errors=errors, resources=self.resources)
        else:
            self.event("cleanup-completed", resources=self.resources)
        return errors

    def report(
        self, status: str, error: Exception | None, cleanup_errors: list[str]
    ) -> dict[str, Any]:
        config = asdict(self.config)
        config["password"] = "***"
        config["provider_api_key"] = "***"
        config["report"] = str(self.config.report) if self.config.report else None
        return self.redact(
            {
                "status": status,
                "finished_at": datetime.now(UTC).isoformat(),
                "config": config,
                "resources": self.resources,
                "error": str(error) if error else None,
                "cleanup_errors": cleanup_errors,
                "events": self.events,
            }
        )


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise FlowError(f"required environment variable is missing: {name}")
    return value


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway", default=os.getenv("AGENT_TEST_GATEWAY", gateway_from_env_file())
    )
    parser.add_argument("--email", default=os.getenv("AGENT_TEST_EMAIL"))
    parser.add_argument("--provider-id", default=os.getenv("AGENT_TEST_PROVIDER_ID"))
    parser.add_argument("--model-id", default=os.getenv("AGENT_TEST_MODEL_ID"))
    parser.add_argument(
        "--thinking-level", default=os.getenv("AGENT_TEST_THINKING_LEVEL") or None
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
        require(bool(value), f"required environment variable is missing: {name}")
    require(args.request_timeout_seconds > 0, "request timeout must be positive")
    require(args.run_timeout_seconds > 0, "run timeout must be positive")
    require(args.poll_interval_seconds > 0, "poll interval must be positive")
    return Config(
        gateway=normalized_api_base(args.gateway),
        email=args.email,
        password=required_environment("AGENT_TEST_PASSWORD"),
        provider_id=args.provider_id,
        provider_api_key=required_environment("AGENT_TEST_PROVIDER_API_KEY"),
        model_id=args.model_id,
        thinking_level=args.thinking_level,
        request_timeout_seconds=args.request_timeout_seconds,
        run_timeout_seconds=args.run_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        keep_resources=args.keep_resources,
        report=args.report.resolve() if args.report else None,
    )


def main() -> int:
    try:
        config = parse_args()
    except FlowError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    flow = AgentUserFlow(config)
    failure: Exception | None = None
    cleanup_errors: list[str] = []
    try:
        flow.run()
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
                    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                print(f"Report: {config.report}")
            flow.close()
    if failure is not None:
        print(f"FAIL: {failure}", file=sys.stderr)
        if flow.resources:
            print(
                f"Created resources: {json.dumps(flow.resources, ensure_ascii=False)}",
                file=sys.stderr,
            )
        return 1
    if cleanup_errors:
        print("FAIL: cleanup did not complete:", file=sys.stderr)
        for error in cleanup_errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    if config.keep_resources:
        print(
            f"PASS: Agent user flow completed; resources retained: {json.dumps(flow.resources, ensure_ascii=False)}"
        )
    else:
        print("PASS: Agent user flow completed and active resources were cleaned up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
