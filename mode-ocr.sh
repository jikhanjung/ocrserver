#!/bin/bash
# GPU 1: chandra-b (OCR × 2)
set -e
cd /srv/ocrserver

echo "[mode] LLM 중지..."
docker compose --profile llm stop llm 2>/dev/null || true

echo "OCR_CONCURRENCY=12" > .env

echo "[mode] chandra-b 기동 (GPU 1)..."
docker compose --profile ocr up -d chandra-b
docker compose up -d --no-deps --force-recreate wrapper

echo "[mode] nginx reload (DNS refresh)..."
docker compose exec nginx nginx -s reload 2>/dev/null || true

echo "[mode] OCR × 2 (GPU 0 + GPU 1) — concurrency 12"
