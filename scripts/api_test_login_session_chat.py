"""Interactive API test: log in, select/create a Session, then stream one task."""
from __future__ import annotations

import json
import os
import sys
from getpass import getpass

import httpx

BASE_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:28297").rstrip("/")


def show(step: str, response: httpx.Response) -> dict:
    body = response.json()
    print(f"\n[{step}] HTTP {response.status_code}")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    response.raise_for_status()
    return body


def select_session(items: list[dict]) -> dict | None:
    if not items:
        print("\n没有历史 Session，将新开一个 Session。")
        return None
    print("\n历史 Sessions:")
    for item in items:
        print(f"- {item['id']}  status={item['status']}  title={item.get('title') or ''}")
    by_id = {item["id"]: item for item in items}
    while True:
        session_id = input("选择 Session（输入 id；直接回车新开）: ").strip()
        if not session_id:
            return None
        if session_id in by_id:
            return by_id[session_id]
        print("输入无效，请从上方列表选择。")


def create_session(client: httpx.Client, headers: dict[str, str]) -> dict:
    profiles = show("可用模型 Profiles", client.get(f"{BASE_URL}/api/v1/agent-profiles", headers=headers))["items"]
    if not profiles:
        raise RuntimeError("没有 Profile，无法新开 Session；请先完成 Provider 和模型 Profile 配置。")
    workspaces = show("默认 Workspace", client.get(f"{BASE_URL}/api/v1/workspaces", headers=headers))["items"]
    if not workspaces:
        raise RuntimeError("没有 Workspace，无法新开 Session")
    session = show("新开 Session", client.post(f"{BASE_URL}/api/v1/sessions", headers=headers, json={"profile_id": profiles[0]["id"], "workspace_id": workspaces[0]["id"]}))
    print(f"使用最新 Profile: {profiles[0]['name']} ({profiles[0]['model_id']})")
    return session


def stream_task(client: httpx.Client, headers: dict[str, str], session_id: str, task: str) -> None:
    print("\n[智能体流式返回]")
    done = False
    with client.stream("POST", f"{BASE_URL}/api/v1/sessions/{session_id}/messages:stream", headers=headers, json={"content": task}) as response:
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
                    done = True
                event_name = None
    print()
    if not done:
        raise RuntimeError("SSE 在 done 事件前结束")


def main() -> None:
    print(f"Gateway: {BASE_URL}")
    email = input("登录邮箱: ").strip().lower()
    password = getpass("登录密码: ")
    with httpx.Client(timeout=60, trust_env=False) as client:
        login = show("用户登录", client.post(f"{BASE_URL}/api/v1/auth/login", json={"email": email, "password": password}))
        headers = {"Authorization": f"Bearer {login['access_token']}"}
        session = select_session(show("历史 Sessions", client.get(f"{BASE_URL}/api/v1/sessions", headers=headers))["items"])
        if session is None:
            session = create_session(client, headers)
        task = input("\n用户任务指令: ").strip()
        if not task:
            raise SystemExit("任务指令不能为空")
        stream_task(client, headers, session["id"], task)
        print(f"\n流程结束。Session ID: {session['id']}")


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"\n测试失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
