#!/usr/bin/env bash
# Start Qwen3.8-27B-FP8 as an OpenAI-compatible chat service.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/../.." && pwd)"
model_path="${LLM_MODEL_PATH:-/home/shared/Qwen/Qwen3.8-27B-FP8}"
served_model_name="${LLM_SERVED_MODEL_NAME:-Qwen3.8-27B-FP8}"
host="${LLM_HOST:-127.0.0.1}"
port="${LLM_PORT:-30018}"
cuda_devices="${LLM_CUDA_VISIBLE_DEVICES:-1}"
tensor_parallel_size="${LLM_TENSOR_PARALLEL_SIZE:-1}"
max_model_len="${LLM_MAX_MODEL_LEN:-8192}"
gpu_memory_utilization="${LLM_GPU_MEMORY_UTILIZATION:-0.90}"
vllm_bin="${VLLM_BIN:-${project_root}/.venv/bin/vllm}"
log_dir="${project_root}/logs/vllm"
log_file="${log_dir}/qwen3.8-27b-fp8.log"
pid_file="${log_dir}/qwen3.8-27b-fp8.pid"

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY
unset http_proxy https_proxy all_proxy
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="127.0.0.1,localhost${no_proxy:+,${no_proxy}}"

if [[ ! -x "${vllm_bin}" ]]; then
  echo "vLLM executable does not exist or is not executable: ${vllm_bin}" >&2
  exit 1
fi
if [[ ! -d "${model_path}" ]]; then
  echo "Model directory does not exist: ${model_path}" >&2
  exit 1
fi

mkdir -p "${log_dir}"

if [[ -f "${pid_file}" ]]; then
  old_pid="$(<"${pid_file}")"
  if [[ "${old_pid}" =~ ^[0-9]+$ ]] && kill -0 "${old_pid}" 2>/dev/null; then
    echo "Chat service is already running (PID ${old_pid})." >&2
    exit 1
  fi
  rm -f "${pid_file}"
fi

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port ${port} is already in use." >&2
  exit 1
fi

extra_args=()
if [[ -n "${LLM_VLLM_EXTRA_ARGS:-}" ]]; then
  read -r -a extra_args <<<"${LLM_VLLM_EXTRA_ARGS}"
fi

nohup setsid env CUDA_VISIBLE_DEVICES="${cuda_devices}" \
  "${vllm_bin}" serve "${model_path}" \
    --served-model-name "${served_model_name}" \
    --runner generate \
    --host "${host}" \
    --port "${port}" \
    --tensor-parallel-size "${tensor_parallel_size}" \
    --max-model-len "${max_model_len}" \
    --gpu-memory-utilization "${gpu_memory_utilization}" \
    "${extra_args[@]}" \
    >"${log_file}" 2>&1 </dev/null &
pid="$!"
printf '%s\n' "${pid}" >"${pid_file}"

sleep 1
if ! kill -0 "${pid}" 2>/dev/null; then
  rm -f "${pid_file}"
  echo "Chat service exited during startup. Check ${log_file}" >&2
  tail -n 30 "${log_file}" >&2 || true
  exit 1
fi

echo "Qwen3.8-27B-FP8 startup initiated (PID ${pid}, port ${port}, GPU ${cuda_devices})."
echo "Log: ${log_file}"
echo "PID file: ${pid_file}"
echo "After the model is ready, run: ${script_dir}/test_qwen3_8_27b_fp8.sh"
