#!/bin/bash
# OCR mode: chandra on every GPU that is free for it — GPU 1 chandra-b always,
# GPU 0 chandra-a unless another job holds GPU 0 (then OCR × 1 on GPU 1).
# Concurrency = 6 per live backend.
set -e
cd /srv/ocrserver
. ./mode-lib.sh

echo "[mode] LLM 중지..."
docker compose --profile llm stop llm 2>/dev/null || true

if is_running chandra-a; then
    echo "[mode] chandra-a (GPU 0) 이미 가동 중"
elif gpu0_free_for_chandra_a; then
    echo "[mode] chandra-a 기동 (GPU 0)..."
    docker compose up -d --no-deps chandra-a
else
    echo "[mode] GPU 0 사용 중 ($(gpu0_used_mib) MiB, chandra-a 아님) — chandra-a 는 띄우지 않는다"
fi

echo "[mode] chandra-b 기동 (GPU 1)..."
docker compose --profile ocr up -d --no-deps chandra-b

echo "[mode] health 대기 (vLLM compile + CUDA graph capture, ~5-8분)..."
n=0
if is_running chandra-a; then
    if wait_healthy chandra-a; then
        n=$((n + 1))
    else
        # nginx resolves the name, so a running-but-broken chandra-a would still get pages
        echo "[mode] chandra-a 중지 (healthy 아님)"
        docker compose stop chandra-a
    fi
fi
if wait_healthy chandra-b ocr; then n=$((n + 1)); fi
if [ "$n" -eq 0 ]; then
    echo "[mode] OCR 백엔드가 하나도 안 떴다 — nginx 는 그대로 둔다"
    recreate_wrapper
    exit 1
fi

set_ocr_concurrency $((6 * n))

echo "[mode] nginx config -> OCR (chandra-a + chandra-b, least_conn, resolve) + reload..."
# upstream servers use `resolve`, so a stopped chandra-a simply drops out
reload_nginx nginx.ocr.conf

echo "[mode] wrapper recreate (OCR_CONCURRENCY=$((6 * n)) 적용)..."
recreate_wrapper

echo "[mode] OCR × $n — concurrency $((6 * n))"
