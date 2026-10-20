#!/bin/bash
# Run backup_db.py inside the wrapper container (docker exec) — the host never opens the live DB files.
# compose mounts ./backup_stage:/stage and /mnt/disk1/backups/ocrserver:/backup into the wrapper.
# crontab (jikhanjung): 7 18 * * * /srv/ocrserver/scripts/backup_db.sh >> /mnt/disk1/backups/ocrserver/backup.log 2>&1
set -u
log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S') UTC] $*"; }
mountpoint -q /mnt/disk1 || { log 'ABORT: /mnt/disk1 가 마운트되지 않음 — 건너뜀'; exit 1; }
exec docker exec -i \
  -e BACKUP_SOURCE=/data/ocrserver.db -e BACKUP_DISK=/backup -e BACKUP_DIR=/backup -e BACKUP_STAGE_DIR=/stage \
  ocrserver-wrapper-1 python3 - < /srv/ocrserver/scripts/backup_db.py
