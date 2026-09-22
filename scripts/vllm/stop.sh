#!/usr/bin/env bash
# Stop either or both vLLM services using the PID files created by start scripts.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/../.." && pwd)"
pid_dir="${project_root}/logs/vllm"
target="${1:-all}"

usage() {
  echo "Usage: $0 [all|embedding|reranker|llm]" >&2
}

stop_one() {
  local name="$1" pid_file="$2" model_marker="$3"

  if [[ ! -f "${pid_file}" ]]; then
    echo "${name}: PID file not found; already stopped or never started."
    return 0
  fi

  local pid
  pid="$(<"${pid_file}")"
  if [[ ! "${pid}" =~ ^[0-9]+$ ]]; then
    echo "${name}: invalid PID file ${pid_file}; refusing to kill anything." >&2
    return 1
  fi
  if ! kill -0 "${pid}" 2>/dev/null; then
    echo "${name}: PID ${pid} is not running; removing stale PID file."
    rm -f "${pid_file}"
    return 0
  fi

  local cmdline
  cmdline="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
  if [[ "${cmdline}" != *"vllm"* || "${cmdline}" != *"${model_marker}"* ]]; then
    echo "${name}: PID ${pid} does not match the expected vLLM model; refusing to kill it." >&2
    echo "Command: ${cmdline}" >&2
    return 1
  fi

  local pgid
  pgid="$(ps -o pgid= -p "${pid}" | tr -d ' ')"
  if [[ "${pgid}" == "${pid}" ]]; then
    kill -TERM -- "-${pgid}"
  else
    kill -TERM "${pid}"
  fi

  for _ in {1..30}; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      rm -f "${pid_file}"
      echo "${name}: stopped PID ${pid}."
      return 0
    fi
    sleep 1
  done

  echo "${name}: PID ${pid} did not exit after 30 seconds; leaving PID file in place." >&2
  echo "Inspect it before using SIGKILL: ps -fp ${pid}" >&2
  return 1
}

case "${target}" in
  all)
    status=0
    stop_one "embedding" "${pid_dir}/qwen3-embedding-8b.pid" "Qwen3-Embedding-8B" || status=1
    stop_one "reranker" "${pid_dir}/qwen3-reranker-0.6b.pid" "Qwen3-Reranker-0.6B" || status=1
    stop_one "llm" "${pid_dir}/qwen3.8-27b-fp8.pid" "Qwen3.8-27B-FP8" || status=1
    exit "${status}"
    ;;
  embedding)
    stop_one "embedding" "${pid_dir}/qwen3-embedding-8b.pid" "Qwen3-Embedding-8B"
    ;;
  reranker)
    stop_one "reranker" "${pid_dir}/qwen3-reranker-0.6b.pid" "Qwen3-Reranker-0.6B"
    ;;
  llm)
    stop_one "llm" "${pid_dir}/qwen3.8-27b-fp8.pid" "Qwen3.8-27B-FP8"
    ;;
  *)
    usage
    exit 2
    ;;
esac
