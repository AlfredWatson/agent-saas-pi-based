#!/usr/bin/env bash
# Start Qwen3-Reranker-0.6B as a vLLM reranking service.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/../.." && pwd)"
model_path="${RERANKER_MODEL_PATH:-/home/shared/Qwen/Qwen3-Reranker-0.6B}"
served_model_name="${RERANKER_SERVED_MODEL_NAME:-Qwen3-Reranker-0.6B}"
host="${RERANKER_HOST:-127.0.0.1}"
port="${RERANKER_PORT:-30019}"
cuda_devices="${RERANKER_CUDA_VISIBLE_DEVICES:-2}"
tensor_parallel_size="${RERANKER_TENSOR_PARALLEL_SIZE:-1}"
max_model_len="${RERANKER_MAX_MODEL_LEN:-8192}"
gpu_memory_utilization="${RERANKER_GPU_MEMORY_UTILIZATION:-0.50}"
vllm_bin="${VLLM_BIN:-${project_root}/.venv/bin/vllm}"
chat_template="${RERANKER_CHAT_TEMPLATE:-${model_path}/chat_template.jinja}"
log_dir="${project_root}/logs/vllm"
log_file="${log_dir}/qwen3-reranker-0.6b.log"
pid_file="${log_dir}/qwen3-reranker-0.6b.pid"

# vLLM needs these overrides to serve the original Qwen3 reranker through
# /v1/rerank rather than treating it as a generative Qwen3 model.
hf_overrides='{"architectures":["Qwen3ForSequenceClassification"],"classifier_from_token":["no","yes"],"is_original_qwen3_reranker":true}'

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
if [[ ! -f "${chat_template}" ]]; then
  echo "Reranker chat template does not exist: ${chat_template}" >&2
  exit 1
fi

mkdir -p "${log_dir}"

if [[ -f "${pid_file}" ]]; then
  old_pid="$(<"${pid_file}")"
  if [[ "${old_pid}" =~ ^[0-9]+$ ]] && kill -0 "${old_pid}" 2>/dev/null; then
    echo "Reranker service is already running (PID ${old_pid})." >&2
    exit 1
  fi
  rm -f "${pid_file}"
fi

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port ${port} is already in use." >&2
  exit 1
fi

extra_args=()
if [[ -n "${RERANKER_VLLM_EXTRA_ARGS:-}" ]]; then
  read -r -a extra_args <<<"${RERANKER_VLLM_EXTRA_ARGS}"
fi

nohup setsid env CUDA_VISIBLE_DEVICES="${cuda_devices}" \
  "${vllm_bin}" serve "${model_path}" \
    --served-model-name "${served_model_name}" \
    --runner pooling \
    --hf-overrides "${hf_overrides}" \
    --chat-template "${chat_template}" \
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
  echo "Reranker service exited during startup. Check ${log_file}" >&2
  tail -n 30 "${log_file}" >&2 || true
  exit 1
fi

echo "Qwen3-Reranker-0.6B startup initiated (PID ${pid}, port ${port}, GPU ${cuda_devices})."
echo "Log: ${log_file}"
echo "PID file: ${pid_file}"
echo "After the model is ready, run: ${script_dir}/test_qwen3_reranker_0_6b.sh"
