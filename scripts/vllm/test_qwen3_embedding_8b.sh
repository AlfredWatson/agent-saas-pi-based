#!/usr/bin/env bash
# Exercise the OpenAI-compatible embeddings endpoint and validate its shape.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/../.." && pwd)"
port="${EMBEDDING_PORT:-31995}"
served_model_name="${EMBEDDING_SERVED_MODEL_NAME:-Qwen3-Embedding-8B}"
base_url="${EMBEDDING_BASE_URL:-http://127.0.0.1:${port}}"
python_bin="${PYTHON_BIN:-${project_root}/.venv/bin/python}"
response_file="$(mktemp /tmp/qwen3-embedding-response.XXXXXX.json)"
trap 'rm -f "${response_file}"' EXIT

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY
unset http_proxy https_proxy all_proxy
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="127.0.0.1,localhost${no_proxy:+,${no_proxy}}"

if [[ ! -x "${python_bin}" ]]; then
  python_bin="$(command -v python3)"
fi

http_code="$(curl --noproxy '*' --silent --show-error \
  --connect-timeout 5 --max-time 120 \
  -o "${response_file}" -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -X POST "${base_url}/v1/embeddings" \
  --data-binary "{\"model\":\"${served_model_name}\",\"input\":[\"北京是中国的首都。\",\"Paris is the capital of France.\"]}")"

if [[ "${http_code}" != "200" ]]; then
  echo "Embedding request failed with HTTP ${http_code}:" >&2
  "${python_bin}" -m json.tool "${response_file}" 2>/dev/null || sed -n '1,120p' "${response_file}" >&2
  exit 1
fi

"${python_bin}" - "${response_file}" <<'PY'
import json
import math
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    response = json.load(handle)

data = response.get("data")
if not isinstance(data, list) or len(data) != 2:
    raise SystemExit(f"Expected two embeddings, got: {data!r}")

dimensions = []
for index, item in enumerate(data):
    vector = item.get("embedding") if isinstance(item, dict) else None
    if not isinstance(vector, list) or not vector:
        raise SystemExit(f"Embedding {index} is missing or empty")
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector):
        raise SystemExit(f"Embedding {index} contains a non-finite value")
    dimensions.append(len(vector))

if len(set(dimensions)) != 1:
    raise SystemExit(f"Embedding dimensions differ: {dimensions}")

print(f"Embedding inference succeeded: count={len(data)}, dimension={dimensions[0]}")
print(f"model={response.get('model', '<not returned>')}")
PY
