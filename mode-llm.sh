#!/bin/bash
# GPU 1: Qwen3-32B-AWQ LLM — 예산제: LLM_GPU_UTIL(기본 0.60) 만 선점, 나머지는
# FigEx / dolfinserver2 학습 등 배치 작업 공용 (devlog 045). .env 로 조절.
# OCR 은 GPU 0 의 chandra-a 하나로 한다. GPU 0 을 다른 작업이 쓰고 있으면
# OCR 이 0 이 되므로 아무것도 바꾸지 않고 거절한다 (devlog 053).
set -e
cd /srv/ocrserver
[ -f .env ] && set -a && . ./.env && set +a
. ./mode-lib.sh

if ! is_running chandra-a; then
    if ! gpu0_free_for_chandra_a; then
        echo "[mode] 거절: chandra-a 가 꺼져 있고 GPU 0 을 다른 작업이 쓰는 중 ($(gpu0_used_mib) MiB)."
        echo "[mode]       LLM 으로 바꾸면 OCR 백엔드가 0 이 된다. 지금 모드를 유지한다."
        # /api/mode 로 왔으면 wrapper 의 switching 플래그(/ocr 503)를 풀어야 한다
        recreate_wrapper
        exit 1
    fi
    echo "[mode] chandra-a 기동 (GPU 0) — chandra-b 를 끄기 전에 healthy 까지 기다린다..."
    docker compose up -d --no-deps chandra-a
    if ! wait_healthy chandra-a; then
        echo "[mode] 거절: chandra-a 가 안 떴다. 지금 모드를 유지한다."
        recreate_wrapper
        exit 1
    fi
fi

echo "[mode] chandra-b 중지..."
docker compose --profile ocr stop chandra-b 2>/dev/null || true

set_ocr_concurrency 6

echo "[mode] nginx config -> LLM (chandra-a only)..."
cp nginx.llm.conf nginx.conf

echo "[mode] LLM 기동 (GPU 1, gpu-memory-utilization=${LLM_GPU_UTIL:-0.60}, max-model-len=${LLM_MAX_MODEL_LEN:-16384})..."
docker compose --profile llm up -d llm llmwrapper
recreate_wrapper

echo "[mode] nginx reload..."
docker compose exec nginx nginx -s reload 2>/dev/null || \
    docker compose up -d --no-deps --force-recreate nginx

echo "[mode] OCR × 1 (GPU 0) + LLM Qwen3-32B-AWQ (GPU 1, util ${LLM_GPU_UTIL:-0.60}) — concurrency 6"
