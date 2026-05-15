#!/bin/bash
# GPU 1: chandra-b (OCR × 2)
set -e
cd /srv/ocrserver

echo "[mode] LLM 중지..."
docker compose --profile llm stop llm 2>/dev/null || true

echo "OCR_CONCURRENCY=12" > .env

echo "[mode] chandra-b 기동 (GPU 1)..."
docker compose --profile ocr up -d chandra-b

echo "[mode] chandra-b health 대기 중 (vLLM compile + CUDA graph capture, ~4-5분)..."
until docker compose exec -T chandra-b curl -sf http://localhost:8000/health > /dev/null 2>&1; do
    sleep 5
done
echo "[mode] chandra-b ready"

echo "[mode] nginx config -> OCR (chandra-a + chandra-b, least_conn) + reload..."
cp nginx.ocr.conf nginx.conf
docker compose exec nginx nginx -s reload 2>/dev/null || \
    docker compose up -d --no-deps --force-recreate nginx

echo "[mode] wrapper recreate (OCR_CONCURRENCY=12 적용)..."
docker compose up -d --no-deps --force-recreate wrapper

echo "[mode] OCR × 2 (GPU 0 + GPU 1) — concurrency 12"
