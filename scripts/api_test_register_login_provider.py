"""Interactive API test: register, log in, and create one Provider Binding."""
from __future__ import annotations

import json
import os
import sys
from getpass import getpass

import httpx

BASE_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")


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
        print(f"\n完成。Provider Binding ID: {binding['id']}")


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"\n测试失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
