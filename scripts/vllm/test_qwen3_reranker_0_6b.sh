#!/usr/bin/env bash
# Exercise vLLM's rerank endpoint and validate the Gateway-compatible response.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/../.." && pwd)"
port="${RERANKER_PORT:-30019}"
served_model_name="${RERANKER_SERVED_MODEL_NAME:-Qwen3-Reranker-0.6B}"
base_url="${RERANKER_BASE_URL:-http://127.0.0.1:${port}}"
endpoint="${base_url%/}"
if [[ "${endpoint}" != */v1 ]]; then
  endpoint="${endpoint}/v1"
fi
python_bin="${PYTHON_BIN:-${project_root}/.venv/bin/python}"
response_file="$(mktemp /tmp/qwen3-reranker-response.XXXXXX.json)"
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
  -X POST "${endpoint}/rerank" \
  --data-binary "{\"model\":\"${served_model_name}\",\"query\":\"中国的首都是哪里？\",\"documents\":[\"巴黎是法国的首都。\",\"北京是中国的首都。\",\"水在标准大气压下约100摄氏度沸腾。\"],\"top_n\":2}")"

if [[ "${http_code}" != "200" ]]; then
  echo "Rerank request failed with HTTP ${http_code}:" >&2
  "${python_bin}" -m json.tool "${response_file}" 2>/dev/null || sed -n '1,120p' "${response_file}" >&2
  exit 1
fi

"${python_bin}" - "${response_file}" <<'PY'
import json
import math
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    response = json.load(handle)

results = response.get("results")
if not isinstance(results, list) or len(results) != 2:
    raise SystemExit(f"Expected two rerank results, got: {results!r}")

seen = set()
scores = []
for result in results:
    if not isinstance(result, dict):
        raise SystemExit(f"Rerank result is not an object: {result!r}")
    index = result.get("index")
    score = result.get("relevance_score")
    if not isinstance(index, int) or isinstance(index, bool) or index not in {0, 1, 2}:
        raise SystemExit(f"Rerank result has an invalid document index: {result!r}")
    if index in seen:
        raise SystemExit(f"Rerank results contain a duplicate document index: {results!r}")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
        raise SystemExit(f"Rerank result has an invalid relevance score: {result!r}")
    seen.add(index)
    scores.append(score)

if scores != sorted(scores, reverse=True):
    raise SystemExit(f"Rerank results are not sorted by relevance score: {results!r}")
if results[0]["index"] != 1:
    raise SystemExit(f"Expected the China-capital document first, got: {results!r}")

print(f"Rerank inference succeeded: top indices={[result['index'] for result in results]}")
print(f"model={response.get('model', '<not returned>')}")
PY
