"""Interactive API test: register, log in, and create one Provider Binding."""
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


def choose_provider(providers: list[dict]) -> dict:
    if not providers:
        raise RuntimeError("服务没有返回可选 Provider")
    print("\n可选 Provider:")
    for item in providers:
        print(f"- {item['id']}  {item.get('name', '')}")
    by_id = {item["id"]: item for item in providers}
    while True:
        provider_id = input("选择 Provider（输入 id）: ").strip()
        if provider_id in by_id:
            return by_id[provider_id]
        print("输入无效，请从上方列表选择。")


def choose_model(models: list[dict]) -> dict:
    if not models:
        raise RuntimeError("该 Provider 没有返回可选模型")
    print("\n可选模型:")
    for item in models:
        print(f"- {item['id']}  {item.get('name', '')}")
    by_id = {item["id"]: item for item in models}
    while True:
        model_id = input("选择模型（输入 id）: ").strip()
        if model_id in by_id:
            return by_id[model_id]
        print("输入无效，请从上方列表选择。")


def choose_thinking_level(model: dict) -> str | None:
    levels = model.get("thinking_levels", [])
    if not levels:
        return None
    print(f"可选推理强度: {', '.join(levels)}；直接回车使用默认值")
    while True:
        level = input("推理强度: ").strip()
        if not level:
            return None
        if level in levels:
            return level
        print("输入无效，请从上方列表选择。")


def verify_default_workspace(client: httpx.Client, headers: dict[str, str]) -> dict:
    workspaces = show("注册后的 Workspaces", client.get(f"{BASE_URL}/api/v1/workspaces", headers=headers))["items"]
    default = next((item for item in workspaces if item["name"] == "default"), None)
    if default is None or default["status"] != "active" or not default["is_current"]:
        raise RuntimeError("注册后未找到已激活的 default Workspace")
    print(f"默认 Workspace: {default['id']}（容器内目录 /runtime-data/workspaces/default/）")
    return default


def main() -> None:
    print(f"Gateway: {BASE_URL}")
    email = input("注册邮箱: ").strip().lower()
    password = getpass("注册密码（至少 12 位）: ")
    if len(password) < 12:
        raise SystemExit("密码长度不足")
    with httpx.Client(timeout=30, trust_env=False) as client:
        show("用户注册", client.post(f"{BASE_URL}/api/v1/auth/register", json={"email": email, "password": password}))
        login = show("用户登录", client.post(f"{BASE_URL}/api/v1/auth/login", json={"email": email, "password": password}))
        headers = {"Authorization": f"Bearer {login['access_token']}"}
        verify_default_workspace(client, headers)
        provider = choose_provider(show("Provider 列表", client.get(f"{BASE_URL}/api/v1/providers", headers=headers))["providers"])
        api_key = getpass(f"输入 {provider['id']} API Key: ")
        if not api_key:
            raise SystemExit("API Key 不能为空")
        binding = show(
            "设置 Provider",
            client.post(
                f"{BASE_URL}/api/v1/provider-bindings",
                headers=headers,
                json={"provider_id": provider["id"], "display_name": f"api-test-{provider['id']}", "api_key": api_key},
            ),
        )
        model = choose_model(show("可用模型列表", client.get(f"{BASE_URL}/api/v1/provider-bindings/{binding['id']}/models", headers=headers))["models"])
        thinking_level = choose_thinking_level(model)
        profile = show(
            "创建模型 Profile",
            client.post(
                f"{BASE_URL}/api/v1/agent-profiles",
                headers=headers,
                json={
                    "name": f"api-test-{provider['id']}-{model['id']}",
                    "provider_binding_id": binding["id"],
                    "model_id": model["id"],
                    "thinking_level": thinking_level,
                },
            ),
        )
        print(f"\n完成。Provider Binding ID: {binding['id']}；Profile ID: {profile['id']}")


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"\n测试失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
