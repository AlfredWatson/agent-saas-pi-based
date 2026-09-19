#!/usr/bin/env python3
"""Black-box acceptance for Gateway user registration.

The script uses only public Gateway endpoints.  It registers a fresh account,
verifies the initial authenticated identity and default Workspace, verifies that
the same credentials can log in, and checks that duplicate registration is
rejected.

The Gateway currently has no user-deletion API, so the registered account is
intentionally retained.  By default a unique address and password are
generated.  Supply both ``REGISTER_TEST_EMAIL`` and ``REGISTER_TEST_PASSWORD``
(or ``--email`` and ``--password``) when provisioning a known test account.

Run after Gateway is ready::

    uv run python test/user_registration.py --report /tmp/user-registration.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from flow_env import configured_value, parse_dotenv, update_dotenv


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_KEYS = {"access_token", "authorization", "password", "token"}


class FlowError(RuntimeError):
    """An HTTP request or assertion in the registration acceptance flow failed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FlowError(message)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if key.casefold() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


def gateway_from_env_file() -> str:
    values = parse_dotenv(ROOT / ".env")
    host = values.get("GATEWAY_HOST", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{values.get('GATEWAY_PORT', '8000')}/api/v1"


def normalized_api_base(url: str) -> str:
    return url.rstrip("/")


@dataclass(frozen=True)
class Config:
    gateway: str
    email: str
    password: str
    request_timeout_seconds: float
    report: Path | None
    credentials_env: Path


class RegistrationFlow:
    def __init__(self, config: Config):
        self.config = config
        self.client = httpx.Client(
            timeout=config.request_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )
        self.events: list[dict[str, Any]] = []
        self.resources: dict[str, str] = {"email": config.email}

    def close(self) -> None:
        self.client.close()

    def event(self, name: str, **details: Any) -> None:
        self.events.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "name": name,
                "details": redact(details),
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        label: str,
        expected: int | tuple[int, ...] = 200,
        token: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        expected_statuses = (expected,) if isinstance(expected, int) else expected
        headers = dict(kwargs.pop("headers", {}))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{self.config.gateway}{path}"
        started = time.monotonic()
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            self.event(label, method=method, path=path, error=f"{type(exc).__name__}: {exc}")
            raise FlowError(f"{label}: cannot reach {url}: {exc}") from exc
        try:
            body: Any = response.json() if response.content else None
        except ValueError:
            body = response.text[:2_000]
        self.event(
            label,
            method=method,
            path=path,
            status_code=response.status_code,
            elapsed_ms=round((time.monotonic() - started) * 1_000, 2),
            response=body,
        )
        if response.status_code not in expected_statuses:
            raise FlowError(
                f"{label}: expected HTTP {expected_statuses}, got {response.status_code}; "
                f"response={redact(body)!r}"
            )
        return response

    def json_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        response = self.request(*args, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise FlowError(f"{kwargs.get('label', 'request')}: expected JSON response") from exc
        require(isinstance(payload, dict), "expected JSON object response")
        return payload

    def run(self) -> None:
        credentials = {"email": self.config.email, "password": self.config.password}
        registered = self.json_request(
            "POST", "/auth/register", label="register", expected=201, json=credentials
        )
        token = registered.get("access_token")
        require(isinstance(token, str) and token, "registration response lacks access_token")
        require(registered.get("token_type") == "bearer", "registration response lacks bearer token_type")

        current_user = self.json_request("GET", "/auth/me", label="get-current-user", token=token)
        user_id = current_user.get("id")
        require(isinstance(user_id, str) and user_id, "current-user response lacks id")
        require(current_user.get("email") == self.config.email.lower(), "registered email mismatch")
        require(current_user.get("status") == "active", "registered user is not active")
        self.resources["user_id"] = user_id

        workspaces = self.json_request("GET", "/workspaces", label="list-default-workspace", token=token)
        items = workspaces.get("items")
        require(isinstance(items, list) and len(items) == 1, "registration must create exactly one default workspace")
        default_workspace = items[0]
        require(isinstance(default_workspace, dict), "default workspace has invalid shape")
        require(default_workspace.get("name") == "default", "default workspace name mismatch")
        require(default_workspace.get("status") == "active", "default workspace is not active")
        require(default_workspace.get("is_current") is True, "default workspace is not current")
        workspace_id = default_workspace.get("id")
        require(isinstance(workspace_id, str) and workspace_id, "default workspace lacks id")
        self.resources["default_workspace_id"] = workspace_id

        logged_in = self.json_request("POST", "/auth/login", label="login", json=credentials)
        login_token = logged_in.get("access_token")
        require(isinstance(login_token, str) and login_token, "login response lacks access_token")
        logged_in_user = self.json_request("GET", "/auth/me", label="get-logged-in-user", token=login_token)
        require(logged_in_user.get("id") == user_id, "login resolved a different user")

        duplicate = self.json_request(
            "POST", "/auth/register", label="reject-duplicate-registration", expected=409, json=credentials
        )
        require(duplicate.get("detail") == "email_exists", "duplicate registration returned an unexpected error")

    def report(self, status: str, error: Exception | None) -> dict[str, Any]:
        config = asdict(self.config)
        config["password"] = "***"
        config["report"] = str(self.config.report) if self.config.report else None
        config["credentials_env"] = str(self.config.credentials_env)
        return {
            "status": status,
            "finished_at": datetime.now(UTC).isoformat(),
            "config": config,
            "resources": self.resources,
            "error": str(error) if error else None,
            "events": self.events,
        }


def parse_args() -> Config:
    dotenv_values = parse_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway",
        default=configured_value("REGISTER_TEST_GATEWAY", dotenv_values) or gateway_from_env_file(),
        help="Gateway API base URL; defaults to GATEWAY_HOST/GATEWAY_PORT in .env.",
    )
    parser.add_argument("--email", default=configured_value("REGISTER_TEST_EMAIL", dotenv_values))
    parser.add_argument("--password", default=configured_value("REGISTER_TEST_PASSWORD", dotenv_values))
    parser.add_argument("--request-timeout-seconds", type=float, default=30)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--credentials-env",
        type=Path,
        default=ROOT / ".env",
        help="dotenv file updated with Agent and RAG test credentials after success.",
    )
    args = parser.parse_args()
    require(args.request_timeout_seconds > 0, "request timeout must be positive")
    require(
        (args.email is None) == (args.password is None),
        "REGISTER_TEST_EMAIL and REGISTER_TEST_PASSWORD must be supplied together",
    )
    generated_suffix = uuid4().hex
    # ``email-validator`` rejects the special-use ``.test`` TLD, while
    # example.com is explicitly valid for non-deliverable test addresses.
    email = args.email or f"registration-test-{generated_suffix}@example.com"
    password = args.password or f"Registration-{generated_suffix}-A!"
    return Config(
        gateway=normalized_api_base(args.gateway),
        email=email,
        password=password,
        request_timeout_seconds=args.request_timeout_seconds,
        report=args.report.resolve() if args.report else None,
        credentials_env=args.credentials_env.resolve(),
    )


def save_test_credentials(config: Config) -> None:
    update_dotenv(
        config.credentials_env,
        {
            "AGENT_TEST_EMAIL": config.email,
            "AGENT_TEST_PASSWORD": config.password,
            "RAG_TEST_EMAIL": config.email,
            "RAG_TEST_PASSWORD": config.password,
        },
    )


def main() -> int:
    try:
        config = parse_args()
    except FlowError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    flow = RegistrationFlow(config)
    failure: Exception | None = None
    try:
        flow.run()
        save_test_credentials(config)
    except Exception as exc:
        failure = exc
    finally:
        report = flow.report("passed" if failure is None else "failed", failure)
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
        return 1
    print(
        "PASS: user registration flow completed; "
        f"account retained: {config.email}; credentials saved: {config.credentials_env}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
