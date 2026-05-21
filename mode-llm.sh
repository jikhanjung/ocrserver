#!/bin/bash
# GPU 1: Qwen3-14B LLM
set -e
cd /srv/ocrserver

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

echo "[mode] LLM 기동 (GPU 1)..."
docker compose --profile llm up -d llm
docker compose up -d --no-deps --force-recreate wrapper

echo "[mode] nginx reload..."
docker compose exec nginx nginx -s reload 2>/dev/null || \
    docker compose up -d --no-deps --force-recreate nginx

echo "[mode] OCR × 1 (GPU 0) + LLM Qwen3-14B (GPU 1) — concurrency 6"
