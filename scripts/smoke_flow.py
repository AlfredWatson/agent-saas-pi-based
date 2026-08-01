"""Manual FastAPI-only smoke. Faux is default; real provider needs explicit env."""
import os
import subprocess
import time
from uuid import uuid4
import httpx

base = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000")
email = f"smoke-{uuid4().hex[:12]}@example.com"
password = "correct-horse-battery-staple"
with httpx.Client(timeout=30, trust_env=False) as client:
    response = client.post(f"{base}/api/v1/auth/register", json={"email": email, "password": password})
    if response.status_code == 409: response = client.post(f"{base}/api/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    provider = os.getenv("SMOKE_PROVIDER", "faux")
    api_key = os.getenv("SMOKE_PROVIDER_API_KEY", "faux-key")
    if provider != "faux" and not os.getenv("SMOKE_PROVIDER_API_KEY"):
        raise SystemExit("real provider smoke requires SMOKE_PROVIDER_API_KEY")
    binding = client.post(f"{base}/api/v1/provider-bindings", headers=headers, json={"provider_id": provider, "display_name": "smoke", "api_key": api_key}); binding.raise_for_status()
    models = client.get(f"{base}/api/v1/provider-bindings/{binding.json()['id']}/models", headers=headers); models.raise_for_status()
    model_id = os.getenv("SMOKE_MODEL_ID") or (models.json()["models"] or [{}])[0].get("id")
    if not model_id: raise SystemExit("set SMOKE_MODEL_ID; runtime returned no catalog model")
    workspace = client.get(f"{base}/api/v1/workspaces", headers=headers); workspace.raise_for_status()
    profile = client.post(f"{base}/api/v1/agent-profiles", headers=headers, json={"name": "smoke", "provider_binding_id": binding.json()["id"], "model_id": model_id, "thinking_level": os.getenv("SMOKE_THINKING") or None}); profile.raise_for_status()
    session = client.post(f"{base}/api/v1/sessions", headers=headers, json={"profile_id": profile.json()["id"], "workspace_id": workspace.json()["items"][0]["id"]}); session.raise_for_status()
    session_id = session.json()["id"]
    with client.stream("POST", f"{base}/api/v1/sessions/{session_id}/messages:stream", headers=headers, json={"content": "Say OK."}) as stream:
        stream.raise_for_status(); events = [line for line in stream.iter_lines() if line.startswith("event: ")]
    assert "event: assistant.delta" in events and events[-1] == "event: done", events
    assert all(item in {"event: message.accepted", "event: assistant.delta", "event: tool.started", "event: tool.completed", "event: message.completed", "event: done"} for item in events), events
    history = client.get(f"{base}/api/v1/sessions/{session_id}/messages", headers=headers); history.raise_for_status()
    assert [item["role"] for item in history.json()["items"]] == ["user", "assistant"]
    # Closing the subscriber must not abort the run.  The Gateway consumer remains alive.
    with client.stream("POST", f"{base}/api/v1/sessions/{session_id}/messages:stream", headers=headers, json={"content": "Say OK."}) as detached:
        detached.raise_for_status(); next(detached.iter_lines())
    for _ in range(50):
        history = client.get(f"{base}/api/v1/sessions/{session_id}/messages", headers=headers); history.raise_for_status()
        if len(history.json()["items"]) >= 4: break
        time.sleep(0.1)
    assert len(history.json()["items"]) >= 4, "SSE disconnect did not persist the final message"
    restart = os.getenv("SMOKE_RUNTIME_RESTART_COMMAND")
    if restart:
        subprocess.run(restart, shell=True, check=True)
        with client.stream("POST", f"{base}/api/v1/sessions/{session_id}/messages:stream", headers=headers, json={"content": "Say OK after recovery."}) as resumed:
            resumed.raise_for_status(); assert "event: done" in [line for line in resumed.iter_lines() if line.startswith("event: ")]
print("PASS: binding, catalog, profile, pure-delta SSE, disconnect persistence, and optional runtime recovery")
