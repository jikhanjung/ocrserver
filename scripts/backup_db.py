#!/usr/bin/env python3
"""Daily local snapshot + integrity check of the wrapper's DB (/srv/ocrserver/data/ocrserver.db).

이 DB 는 **작업 큐이자 결과 캐시**다 — OCR 결과의 원본은 클라이언트(PaperMeister 캐시 JSON·Zotero 첨부)에 있고,
도판 결과도 클라이언트가 수거해 자기 DB 에 둔다. 잃으면 같은 PDF 재제출 때 다시 OCR 하고, 수거 전 결과와
대기 큐만 다시 내면 된다. 그래서 형제 repo 의 매시·오프사이트 백업 계약을 따르지 않고(사용자 결정, 2026-10-06):

- **하루 한 번, 로컬에 1개만**, 오프사이트 pull 없음.
- 목적은 복구보다 **무결성 감시**다. 스냅샷을 PRAGMA integrity_check 로 검사해 실패하면 DB 옆에
  INTEGRITY_FAIL 센티넬을 남긴다 → wrapper `/healthz` 가 `degraded`(0.3.10). 다음 날 검사가 통과하면 스스로 지운다.
  손상 직전 상태로 큐를 되살릴 때도 쓴다.
- 검사에 걸리거나 새 스냅샷을 못 만들면 **기존 스냅샷을 지우지 않는다**(깨진 것으로 성한 것을 밀어내지 않게).

원본 잠금 시간: ocrserver.db 는 rollback 저널(journal_mode=DELETE)이라 backup 이 원본을 읽는 동안 wrapper 는
쓰기 커밋을 기다린다(aiosqlite 기본 busy timeout 5 s). HDD 로 바로 쓰면 10 s 를 넘어 OCR 페이지 저장·도판 워커
heartbeat 가 "database is locked" 로 실패할 수 있어, **NVMe(STAGE_DIR)로 한 번에 복사**하고(실측 3.7 s,
2026-10-06) 검사·HDD 이동은 잠금이 풀린 뒤에 한다. 단계를 나눠 복사하면 원본이 바뀔 때마다 처음부터 다시 시작해
도판 워커의 잦은 쓰기 아래서 끝나지 않는다. 근본 해결은 원본을 WAL 로 바꾸는 것(wrapper 변경).

실행: jikhanjung 의 crontab, 매일 18:07 UTC(03:07 KST). 원본은 root 소유 0644 라 읽기만 하면 되고, 센티넬을 쓰는
data/ 와 보관 디렉터리는 jikhanjung 소유라 sudo 가 필요 없다.
    7 18 * * * /usr/bin/python3 /srv/ocrserver/scripts/backup_db.py >> /mnt/disk1/backups/ocrserver/backup.log 2>&1

복원: `docker compose stop wrapper` → 스냅샷을 data/ocrserver.db 로 복사(원본이 root 소유라 one-shot 컨테이너로,
`docker run --rm -v /srv/ocrserver/data:/data -v /mnt/disk1/backups/ocrserver:/b alpine cp /b/<snapshot> /data/ocrserver.db`)
→ `docker compose up -d --no-deps wrapper`.
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path('/srv/ocrserver/data/ocrserver.db')
NAME = 'ocrserver'
DISK = Path('/mnt/disk1')                         # 보관 디스크 — 마운트 확인용
BACKUP_DIR = DISK / 'backups' / 'ocrserver'       # ocrserver_YYYYMMDD.sqlite3 (UTC 날짜)
STAGE_DIR = Path('/srv/ocrserver/backup_stage')   # NVMe — 원본 잠금을 짧게 하려고 먼저 여기로 복사
# 로컬 1개 — 복구 백업이 아니라 무결성 감시용(위 docstring). 새 스냅샷이 채택된 뒤에만 옛 것을 지운다.
# 디스크: 2.1 GB (HDD). 오프사이트 pull 없음.
RETAIN_COUNT = 1
MIN_FREE_GB = 5        # 스냅샷 크기에 더해 남겨야 할 여유 — 임시 복사 디스크·보관 디스크 각각
SENTINEL_NAME = 'INTEGRITY_FAIL'                  # DB 옆. wrapper /healthz 가 stat 한다.


def log(msg: str) -> None:
    print(f'[{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")} UTC] {msg}', flush=True)


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


def check_space() -> bool:
    if not os.path.ismount(DISK):
        log(f'ABORT: {DISK} 가 마운트되지 않음 — 루트 디스크를 채우지 않도록 건너뜀')
        return False
    need = SOURCE.stat().st_size / 1024 ** 3 + MIN_FREE_GB
    for d in (STAGE_DIR, BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)
        if free_gb(d) < need:
            log(f'ABORT: {d} 여유 {free_gb(d):.1f} GB < 필요 {need:.1f} GB (스냅샷 + {MIN_FREE_GB} GB) — 건너뜀')
            return False
    return True


def integrity_check(path: Path) -> list[str]:
    """통과면 [], 실패면 문제 목록. 스냅샷을 읽기 전용으로 검사한다(라이브 DB 에 긴 읽기를 걸지 않는다)."""
    conn = None
    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        rows = conn.execute('PRAGMA integrity_check').fetchall()
    except sqlite3.DatabaseError as e:
        return [f'열기/PRAGMA 실패: {e}']
    finally:
        if conn is not None:
            conn.close()
    return [r[0] for r in rows if r and r[0] != 'ok']


def raise_sentinel(problems: list[str]) -> None:
    body = [f'{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC backup_db.py: PRAGMA integrity_check 실패.',
            '이 파일이 있는 한 /healthz 는 degraded 다. 백업 prune 은 중단됐다 —',
            f'{BACKUP_DIR} 의 과거 스냅샷이 복구 후보다. 다음 날 검사가 통과하면 자동으로 지워진다.', '',
            *problems[:20]]
    try:
        (SOURCE.parent / SENTINEL_NAME).write_text('\n'.join(body) + '\n')
    except OSError as e:
        log(f'경고: 센티넬 기록 실패({e}) — /healthz 가 손상을 못 알린다')


def clear_sentinel() -> None:
    s = SOURCE.parent / SENTINEL_NAME
    if s.exists():
        try:
            s.unlink(); log(f'integrity OK — 센티넬 해제({s})')
        except OSError as e:
            log(f'경고: 센티넬 해제 실패({e})')


def snapshot() -> Path | None:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d')
    dest = BACKUP_DIR / f'{NAME}_{stamp}.sqlite3'
    tmp = STAGE_DIR / f'{NAME}_{stamp}.sqlite3.tmp'
    tmp.unlink(missing_ok=True)
    src = dst = None
    try:
        # 읽기 전용으로 연다 — 원본은 root 소유이고, 이 스크립트가 원본을 바꿀 일은 없다.
        # (rollback 저널이 남은 "hot journal" 상태면 복구에 쓰기 권한이 필요해 여기서 실패한다 → 이번 회차 건너뜀)
        src = sqlite3.connect(f'file:{SOURCE}?mode=ro', uri=True, timeout=30)
        dst = sqlite3.connect(str(tmp))
        t0 = datetime.now()
        src.backup(dst)                        # 한 단계 — 원본 SHARED 잠금은 이 동안만(NVMe 실측 ~4 s)
        held = (datetime.now() - t0).total_seconds()
        src.close(); src = None                # 잠금을 바로 놓는다
        dst.execute('PRAGMA journal_mode=DELETE')   # 단독 파일 보장(원본도 DELETE 지만 명시)
        dst.close(); dst = None
    except sqlite3.Error as e:
        log(f'ERROR: 스냅샷 실패 — {e}')
        tmp.unlink(missing_ok=True)
        return None
    finally:
        for c in (dst, src):
            if c is not None:
                c.close()

    problems = integrity_check(tmp)
    if problems:
        log(f'!! INTEGRITY FAIL — 스냅샷 미채택, 라이브 DB 손상으로 간주 ({SOURCE})')
        for p in problems[:5]:
            log(f'   {p}')
        evidence = BACKUP_DIR / f'{NAME}_INTEGRITY_FAIL.corrupt'   # 확장자가 달라 prune 에 안 걸린다
        if evidence.exists():
            tmp.unlink(missing_ok=True)
        else:
            shutil.move(str(tmp), str(evidence)); log(f'증거 사본 보존 → {evidence}')
        raise_sentinel(problems)
        return None

    part = dest.with_suffix('.sqlite3.part')   # HDD 로 옮기는 중인 파일은 pull/prune 대상이 아니다
    shutil.move(str(tmp), str(part))
    part.replace(dest)
    clear_sentinel()
    log(f'backup OK ({dest.name}, {dest.stat().st_size / 1e9:.2f} GB, 원본 잠금 {held:.1f} s, integrity ok)')
    return dest


def prune() -> None:
    snaps = sorted(BACKUP_DIR.glob(f'{NAME}_????????.sqlite3'), reverse=True)
    for f in snaps[RETAIN_COUNT:]:
        try:
            f.unlink(); log(f'pruned {f.name}')
        except OSError as e:
            log(f'경고: prune 실패 {f.name}: {e}')


def main() -> int:
    if not SOURCE.exists():
        log(f'ERROR: 원본 없음 ({SOURCE}) — prune 하지 않음'); return 1
    if not check_space():
        return 1
    if snapshot() is None:
        log('스냅샷 미채택 — prune 건너뜀(과거 스냅샷 보존)'); return 1
    prune()
    return 0


if __name__ == '__main__':
    sys.exit(main())
