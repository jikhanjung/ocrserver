#!/bin/bash
# GPU 1: Qwen3-32B-AWQ LLM — 예산제: LLM_GPU_UTIL(기본 0.60) 만 선점, 나머지는
# FigEx / dolfinserver2 학습 등 배치 작업 공용 (devlog 045). .env 로 조절.
set -e
cd /srv/ocrserver
[ -f .env ] && set -a && . ./.env && set +a

echo "[mode] chandra-b 중지..."
docker compose --profile ocr stop chandra-b 2>/dev/null || true

# Update OCR_CONCURRENCY in place, preserving other env vars (MODE_TOKEN, etc).
touch .env
if grep -q "^OCR_CONCURRENCY=" .env; then
    sed -i "s/^OCR_CONCURRENCY=.*/OCR_CONCURRENCY=6/" .env
else
    echo "OCR_CONCURRENCY=6" >> .env
fi

echo "[mode] nginx config -> LLM (chandra-a only)..."
cp nginx.llm.conf nginx.conf

echo "[mode] LLM 기동 (GPU 1, gpu-memory-utilization=${LLM_GPU_UTIL:-0.60}, max-model-len=${LLM_MAX_MODEL_LEN:-16384})..."
docker compose --profile llm up -d llm llmwrapper
docker compose up -d --no-deps --force-recreate wrapper

echo "[mode] nginx reload..."
docker compose exec nginx nginx -s reload 2>/dev/null || \
    docker compose up -d --no-deps --force-recreate nginx

echo "[mode] OCR × 1 (GPU 0) + LLM Qwen3-32B-AWQ (GPU 1, util ${LLM_GPU_UTIL:-0.60}) — concurrency 6"
