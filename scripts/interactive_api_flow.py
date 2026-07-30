"""Interactive end-to-end API check for the normal SaaS onboarding flow.

Requires a running Gateway and Agent Runtime.  The API key is read with
getpass and is never printed or written by this script.
"""
from __future__ import annotations

import json
import os
import sys
from getpass import getpass

import httpx


BASE_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
PASSWORD_MIN_LENGTH = 12


def show(step: str, response: httpx.Response) -> dict:
    """Print server output. Binding responses intentionally never contain a key."""
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {"raw": response.text}
    print(f"\n[{step}] HTTP {response.status_code}")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    response.raise_for_status()
    return body


def choose(label: str, rows: list[dict], key: str = "id") -> dict:
    if not rows:
        raise RuntimeError(f"服务没有返回可选{label}")
    print(f"\n可选{label}:")
    for row in rows:
        print(f"- {row[key]}  {row.get('name', '')}")
    allowed = {str(row[key]): row for row in rows}
    while True:
        selected = input(f"请选择{label}（输入 {key}）: ").strip()
        if selected in allowed:
            return allowed[selected]
        print(f"输入无效。请输入上方列表中的 {key}。")


def stream_message(client: httpx.Client, headers: dict[str, str], session_id: str, content: str) -> None:
    print("\n[智能体流式返回]")
    completed = False
    with client.stream(
        "POST",
        f"{BASE_URL}/api/v1/sessions/{session_id}/messages:stream",
        headers=headers,
        json={"content": content},
    ) as response:
        response.raise_for_status()
        event_name: str | None = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            elif line.startswith("data: ") and event_name:
                payload = json.loads(line.removeprefix("data: "))
                if event_name == "assistant.delta":
                    print(payload.get("delta", ""), end="", flush=True)
                elif event_name in {"tool.started", "tool.completed"}:
                    print(f"\n[{event_name}: {payload.get('tool', 'unknown')}]", flush=True)
                elif event_name == "message.failed":
                    raise RuntimeError(f"聊天失败: {payload}")
                elif event_name == "done":
                    completed = True
                event_name = None
    print()
    if not completed:
        raise RuntimeError("SSE 在 done 事件前结束")


def main() -> None:
    print(f"Gateway: {BASE_URL}")
    email = input("注册邮箱: ").strip().lower()
    password = getpass(f"注册密码（至少 {PASSWORD_MIN_LENGTH} 位）: ")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise SystemExit("密码长度不足")

    with httpx.Client(timeout=60, trust_env=False) as client:
        show("用户注册", client.post(f"{BASE_URL}/api/v1/auth/register", json={"email": email, "password": password}))
        login = show("用户登录", client.post(f"{BASE_URL}/api/v1/auth/login", json={"email": email, "password": password}))
        headers = {"Authorization": f"Bearer {login['access_token']}"}

        providers = show("可供选择的 Provider", client.get(f"{BASE_URL}/api/v1/providers", headers=headers))["providers"]
        provider = choose("Provider", providers)
        api_key = getpass(f"输入 {provider['id']} API Key: ")
        if not api_key:
            raise SystemExit("API Key 不能为空")
        binding = show(
            "Provider Binding",
            client.post(
                f"{BASE_URL}/api/v1/provider-bindings",
                headers=headers,
                json={"provider_id": provider["id"], "display_name": f"interactive-{provider['id']}", "api_key": api_key},
            ),
        )
        models = show(
            "可用模型列表",
            client.get(f"{BASE_URL}/api/v1/provider-bindings/{binding['id']}/models", headers=headers),
        )["models"]
        model = choose("模型", models)

        thinking_levels = model.get("thinking_levels", [])
        thinking_level = None
        if thinking_levels:
            print(f"可选推理强度: {', '.join(thinking_levels)}；直接回车使用默认值")
            while True:
                candidate = input("推理强度: ").strip()
                if not candidate:
                    break
                if candidate in thinking_levels:
                    thinking_level = candidate
                    break
                print("输入无效，请从上方列表选择。")

        profile = show(
            "创建模型 Profile",
            client.post(
                f"{BASE_URL}/api/v1/agent-profiles",
                headers=headers,
                json={"name": "interactive", "provider_binding_id": binding["id"], "model_id": model["id"], "thinking_level": thinking_level},
            ),
        )
        workspace = show("默认 Workspace", client.get(f"{BASE_URL}/api/v1/workspaces", headers=headers))["items"][0]
        session = show(
            "新开 Session",
            client.post(f"{BASE_URL}/api/v1/sessions", headers=headers, json={"profile_id": profile["id"], "workspace_id": workspace["id"]}),
        )
        instruction = input("\n输入指令: ").strip()
        if not instruction:
            raise SystemExit("指令不能为空")
        stream_message(client, headers, session["id"], instruction)
        print(f"\n流程完成。Session ID: {session['id']}")


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"\n测试失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
