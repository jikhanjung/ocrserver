#!/bin/bash
# Runtime dispatcher for Chandra2-vLLM.
# Detects GPU compute capability and picks a compatible dtype (fp16 on Turing,
# bf16 on Ampere+), unless VLLM_DTYPE is set explicitly.
set -e

CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader,nounits 2>/dev/null \
     | head -1 | tr -d '.')

if [ -z "$VLLM_DTYPE" ]; then
  if [ -z "$CC" ]; then
    echo "[entrypoint] WARN: failed to detect GPU compute capability -> dtype=auto"
    VLLM_DTYPE=auto
  elif [ "$CC" -lt 80 ]; then
    VLLM_DTYPE=float16
  else
    VLLM_DTYPE=bfloat16
  fi
fi

echo "[entrypoint] GPU CC=${CC:-unknown} -> VLLM_DTYPE=$VLLM_DTYPE"

exec python3 -m vllm.entrypoints.openai.api_server \
  --model "${VLLM_MODEL:-datalab-to/chandra-ocr-2}" \
  --served-model-name "${VLLM_SERVED_NAME:-chandra}" \
  --host 0.0.0.0 --port 8000 \
  --gpu-memory-utilization "${VLLM_GPU_UTIL:-0.90}" \
  --max-model-len "${VLLM_MAX_MODEL_LEN:-12384}" \
  --dtype "$VLLM_DTYPE" \
  --trust-remote-code \
  ${VLLM_EXTRA_ARGS}
