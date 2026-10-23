#!/bin/bash
# Shared helpers for mode-ocr.sh / mode-llm.sh (sourced, not run).
#
# GPU 0 used to be chandra-a's alone ("always on"). Since 2026-10 other work
# (figsplit training) can hold GPU 0 while chandra-a is stopped, so the mode
# scripts must not assume chandra-a is up, and must never start it on top of
# someone else's job (devlog 053).

# MiB on GPU 0 above which, with chandra-a not running, GPU 0 counts as taken.
GPU0_BUSY_MIB=${GPU0_BUSY_MIB:-1024}
HEALTH_TIMEOUT_S=${HEALTH_TIMEOUT_S:-900}

is_running() {  # is_running <service> [profile]
    local svc=$1 prof=${2:+--profile $2}
    [ -n "$(docker compose $prof ps --status running -q "$svc" 2>/dev/null)" ]
}

gpu0_used_mib() {
    nvidia-smi -i 0 --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | tr -d ' '
}

gpu0_free_for_chandra_a() {  # true if chandra-a may be started on GPU 0
    local used
    used=$(gpu0_used_mib)
    [ -n "$used" ] && [ "$used" -lt "$GPU0_BUSY_MIB" ]
}

wait_healthy() {  # wait_healthy <service> [profile] — 0 when /health answers, 1 on timeout
    local svc=$1 prof=${2:+--profile $2} t=0
    until docker compose $prof exec -T "$svc" curl -sf http://localhost:8000/health > /dev/null 2>&1; do
        sleep 5; t=$((t + 5))
        if [ "$t" -ge "$HEALTH_TIMEOUT_S" ]; then
            echo "[mode] $svc not healthy after ${HEALTH_TIMEOUT_S}s"
            return 1
        fi
    done
    echo "[mode] $svc ready"
}

set_ocr_concurrency() {  # set_ocr_concurrency <n> — in place, keeps other .env vars (MODE_TOKEN, ...)
    touch .env
    if grep -q "^OCR_CONCURRENCY=" .env; then
        sed -i "s/^OCR_CONCURRENCY=.*/OCR_CONCURRENCY=$1/" .env
    else
        echo "OCR_CONCURRENCY=$1" >> .env
    fi
}

reload_nginx() {  # reload_nginx <conf> — cp keeps the inode the bind mount pins
    cp "$1" nginx.conf
    docker compose exec nginx nginx -s reload 2>/dev/null || \
        docker compose up -d --no-deps --force-recreate nginx
}

recreate_wrapper() {  # also clears the wrapper's in-memory "mode switching" flag (/ocr 503)
    docker compose up -d --no-deps --force-recreate wrapper
}
