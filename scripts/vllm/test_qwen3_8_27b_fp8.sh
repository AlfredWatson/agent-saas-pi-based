#!/usr/bin/env bash
# Exercise the OpenAI-compatible chat-completions endpoint.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/../.." && pwd)"
port="${LLM_PORT:-30018}"
served_model_name="${LLM_SERVED_MODEL_NAME:-Qwen3.8-27B-FP8}"
base_url="${LLM_BASE_URL:-http://127.0.0.1:${port}}"
python_bin="${PYTHON_BIN:-${project_root}/.venv/bin/python}"
response_file="$(mktemp /tmp/qwen3.8-27b-response.XXXXXX.json)"
trap 'rm -f "${response_file}"' EXIT

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY
unset http_proxy https_proxy all_proxy
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="127.0.0.1,localhost${no_proxy:+,${no_proxy}}"

if [[ ! -x "${python_bin}" ]]; then
  python_bin="$(command -v python3)"
fi

http_code="$(curl --noproxy '*' --silent --show-error \
  --connect-timeout 5 --max-time 300 \
  -o "${response_file}" -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -X POST "${base_url}/v1/chat/completions" \
  --data-binary "{\"model\":\"${served_model_name}\",\"messages\":[{\"role\":\"user\",\"content\":\"只回答：推理成功\"}],\"temperature\":0,\"max_tokens\":128,\"chat_template_kwargs\":{\"enable_thinking\":false}}")"

if [[ "${http_code}" != "200" ]]; then
  echo "Chat request failed with HTTP ${http_code}:" >&2
  "${python_bin}" -m json.tool "${response_file}" 2>/dev/null || sed -n '1,160p' "${response_file}" >&2
  exit 1
fi

"${python_bin}" - "${response_file}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    response = json.load(handle)

choices = response.get("choices")
if not isinstance(choices, list) or not choices:
    raise SystemExit(f"Response has no choices: {response!r}")

message = choices[0].get("message") or {}
content = message.get("content")
if not isinstance(content, str) or not content.strip():
    raise SystemExit(f"Response content is empty: {message!r}")

print("Chat inference succeeded.")
print(f"model={response.get('model', '<not returned>')}")
print(f"content={content.strip()}")
PY
