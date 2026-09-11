"""Interactive API test: log in, select/create a Session, then stream one task."""
from __future__ import annotations

import json
import sys
from getpass import getpass

import httpx

from gateway_config import gateway_url

BASE_URL = gateway_url()


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


def select_workspace(client: httpx.Client, headers: dict[str, str]) -> dict:
    while True:
        workspaces = show("用户 Workspaces", client.get(f"{BASE_URL}/api/v1/workspaces", headers=headers))["items"]
        current = next((item for item in workspaces if item["is_current"]), None)
        if current is None:
            raise RuntimeError("当前用户没有可用的 current Workspace")
        print("\n可用 Workspaces:")
        for item in workspaces:
            marker = "（当前）" if item["is_current"] else ""
            print(f"- {item['id']}  name={item['name']} status={item['status']} {marker}")
        choice = input("选择 Workspace（输入 id；n 新建；直接回车使用当前）: ").strip()
        if not choice:
            return current
        if choice == "n":
            name = input("新 Workspace 名称（小写字母/数字/-/_，1-64 字符）: ").strip()
            if not name:
                print("Workspace 名称不能为空。")
                continue
            created = show("新建 Workspace", client.post(f"{BASE_URL}/api/v1/workspaces", headers=headers, json={"name": name}))
            return show("切换 Workspace", client.post(f"{BASE_URL}/api/v1/workspaces/{created['id']}:switch", headers=headers))
        selected = next((item for item in workspaces if item["id"] == choice), None)
        if selected is None:
            print("输入无效，请从上方列表选择。")
            continue
        if selected["status"] != "active":
            print("该 Workspace 当前不可用。")
            continue
        if selected["is_current"]:
            return selected
        return show("切换 Workspace", client.post(f"{BASE_URL}/api/v1/workspaces/{selected['id']}:switch", headers=headers))


def sessions_for_workspace(client: httpx.Client, headers: dict[str, str], workspace: dict) -> list[dict]:
    session_ids = show("Workspace Sessions", client.get(f"{BASE_URL}/api/v1/workspaces/{workspace['id']}/sessions", headers=headers))["session_ids"]
    sessions = show("历史 Sessions", client.get(f"{BASE_URL}/api/v1/sessions", headers=headers))["items"]
    by_id = {item["id"]: item for item in sessions}
    return [by_id[session_id] for session_id in session_ids if session_id in by_id]


def create_session(client: httpx.Client, headers: dict[str, str], workspace: dict) -> dict:
    profiles = show("可用模型 Profiles", client.get(f"{BASE_URL}/api/v1/agent-profiles", headers=headers))["items"]
    if not profiles:
        raise RuntimeError("没有 Profile，无法新开 Session；请先完成 Provider 和模型 Profile 配置。")
    # Deliberately omit workspace_id: this verifies that the Gateway selects the
    # Workspace made current by the step above.
    session = show("新开 Session（使用当前 Workspace）", client.post(f"{BASE_URL}/api/v1/sessions", headers=headers, json={"profile_id": profiles[0]["id"]}))
    print(f"使用 Workspace: {workspace['name']} ({workspace['id']})")
    print(f"使用最新 Profile: {profiles[0]['name']} ({profiles[0]['model_id']})")
    return session


def show_history(client: httpx.Client, headers: dict[str, str], session_id: str) -> None:
    items = show("所选 Session 历史", client.get(f"{BASE_URL}/api/v1/sessions/{session_id}/messages", headers=headers))["items"]
    if not items:
        print("（暂无消息）")
        return
    for item in items:
        suffix = ""
        if item["role"] in {"tool_call", "tool_result"}:
            suffix = f" call={item.get('tool_call_id')} tool={item.get('tool_name')} error={item.get('is_error')}"
        print(f"#{item['sequence']} {item['role']}{suffix}: {item['content']}")


def stream_task(client: httpx.Client, headers: dict[str, str], session_id: str, task: str) -> None:
    print("\n[智能体流式返回]")
    done = False
    # A long agent turn can legitimately be quiet while a tool runs.  The
    # Gateway deliberately keeps that turn alive after an SSE subscriber
    # disconnects, so a client-side read timeout would leave the session busy
    # and make a later message conflict with it.  Keep normal request limits,
    # but wait indefinitely for stream events.
    stream_timeout = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=60.0)
    with client.stream("POST", f"{BASE_URL}/api/v1/sessions/{session_id}/messages:stream", headers=headers, json={"content": task}, timeout=stream_timeout) as response:
        if response.status_code == 409:
            detail = response.json().get("detail", "session_busy")
            if detail == "session_busy":
                raise RuntimeError("该 Session 仍在后台执行上一条任务；请等待其结束，或先调用 POST /api/v1/sessions/{session_id}/abort 后再继续对话。")
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
                    fields = {key: payload.get(key) for key in ("toolCallId", "toolName", "args", "result", "isError", "payload_truncated") if key in payload}
                    print(f"\n[{event_name}] {json.dumps(fields, ensure_ascii=False)}", flush=True)
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
        workspace = select_workspace(client, headers)
        session = select_session(sessions_for_workspace(client, headers, workspace))
        if session is None:
            session = create_session(client, headers, workspace)
        show_history(client, headers, session["id"])
        task = input("\n用户任务指令: ").strip()
        if not task:
            raise SystemExit("任务指令不能为空")
        stream_task(client, headers, session["id"], task)
        show_history(client, headers, session["id"])
        print(f"\n流程结束。Session ID: {session['id']}")


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"\n测试失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
