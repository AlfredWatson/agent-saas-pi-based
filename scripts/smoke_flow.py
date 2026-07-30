"""Manual local smoke: requires PostgreSQL, Gateway and Faux-compatible Runtime."""
import os
import requests

base = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000")
email = "smoke@example.test"
password = "correct-horse-battery-staple"
response = requests.post(f"{base}/api/v1/auth/register", json={"email": email, "password": password})
if response.status_code == 409: response = requests.post(f"{base}/api/v1/auth/login", json={"email": email, "password": password})
response.raise_for_status()
print("authenticated", response.json()["token_type"])
