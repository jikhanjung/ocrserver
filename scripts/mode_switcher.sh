#!/bin/bash
# Triggered by ocrserver-mode-switch.path when wrapper writes a mode request to
# /srv/ocrserver/data/mode_request. Reads the desired mode and runs the
# matching mode-*.sh script, which itself recreates the wrapper container.
#
# Request file format: a single line with "ocr" or "llm".
set -e

REQ=/srv/ocrserver/data/mode_request
[ -f "$REQ" ] || exit 0

MODE=$(head -1 "$REQ" | tr -d '[:space:]')

# Remove the request first so the .path unit can fire again on the next change.
rm -f "$REQ"

cd /srv/ocrserver

case "$MODE" in
    ocr) exec ./mode-ocr.sh ;;
    llm) exec ./mode-llm.sh ;;
    *)   echo "[mode_switcher] unknown mode: '$MODE'" >&2; exit 1 ;;
esac
