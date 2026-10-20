#!/bin/bash
# Run backup_db.py inside a one-shot container (wrapper image) — the host never opens the live DB files.
# crontab (jikhanjung): 7 18 * * * /srv/ocrserver/scripts/backup_db.sh >> /mnt/disk1/backups/ocrserver/backup.log 2>&1
set -u
log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S') UTC] $*"; }
# /mnt/disk1 이 빠져 있으면 바인드 마운트가 루트 디스크의 빈 폴더를 가리키므로 호스트에서 먼저 확인한다.
mountpoint -q /mnt/disk1 || { log 'ABORT: /mnt/disk1 가 마운트되지 않음 — 건너뜀'; exit 1; }
image=$(docker inspect ocrserver-wrapper-1 --format '{{.Config.Image}}' 2>/dev/null) \
  || { log 'ABORT: ocrserver-wrapper-1 이미지 확인 실패'; exit 1; }
exec docker run --rm --network none --entrypoint python3 \
  -v /srv/ocrserver/data:/data \
  -v /srv/ocrserver/backup_stage:/stage \
  -v /mnt/disk1/backups/ocrserver:/backup \
  -v /srv/ocrserver/scripts/backup_db.py:/backup_db.py:ro \
  -e BACKUP_SOURCE=/data/ocrserver.db -e BACKUP_DISK=/backup -e BACKUP_DIR=/backup -e BACKUP_STAGE_DIR=/stage \
  "$image" /backup_db.py
