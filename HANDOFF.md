# HANDOFF — 2026-05-21 (chandra 0.1.1 build/push + ops swap)

이 파일은 작업 인수인계용. 작업 단위로 갱신.

## 방금 한 작업 (2026-05-21)

- 빌드 호스트(외부) 세션 #2: chandra (`honestjung/ocrserver`) **`:0.1.1` 빌드
  + Docker Hub push** 완료.
  - Manifest digest: `sha256:f6117fbbb2caa866d8349b72bc0d7d11ecc176ff1e4790562f083774fa3d380c`
  - Image ID: `f6117fbbb2ca`, content 17 GB.
  - `:0.1.1` 과 `:latest` 둘 다 같은 digest 로 Hub 에 올라감.
  - 020 에서 deferred 됐던 항목. KOPRI 호스트의 12% stuck 함정이 이번엔
    **재현되지 않음** — 베이스 이미지를 fresh 로 받으면서 buildkit state 가
    리셋된 영향일 가능성. 상세는 `devlog/20260521_021_*.md`.
- 운영 호스트(이 머신) 세션 #3: chandra-a + chandra-b 를 `:0.1.0` → `:0.1.1`
  로 swap. `docker pull honestjung/ocrserver:0.1.1` + `:latest` → compose
  bump → `docker compose up -d chandra-a chandra-b` → 두 GPU 모두 cold start
  통과 후 healthy. `/api/services._meta.images` 와 `/status` 뱃지에 0.1.1
  반영 확인.
- 직전 세션(2026-05-20)의 wrapper 0.1.0 → 0.1.6 작업은 그대로 운영 중.

상세는 `devlog/20260521_021_*.md`.

## 현재 상태 (snapshot)

### 운영 모드
- nginx 모드: **OCR 2 GPU** (`nginx.ocr.conf` 활성, mode chip 상 `2ocr`)
- 운영 디렉터리: `/srv/ocrserver/` (jikhanjung 소유, sudo 없이 docker compose 가능)
- 운영 컴포즈는 prebuilt image 만 참조 (build 는 dev tree 에서)

### 컨테이너 / 이미지 (운영서버)
```
SERVICE     IMAGE                         STATUS
chandra-a   honestjung/ocrserver:0.1.1    Up (healthy, 방금 swap)
chandra-b   honestjung/ocrserver:0.1.1    Up (healthy, 방금 swap)
nginx       nginx:alpine                  Up 45h
wrapper     honestjung/ocrwrapper:0.1.6   Up 3h
```

- Docker Hub `honestjung/ocrserver` 상태: `:0.1.0`, `:0.1.1`, `:latest` 셋 다 존재.
  `:0.1.1` 과 `:latest` 가 같은 digest (`f6117fbbb2ca...`).
- 운영서버 로컬에도 `:0.1.0`, `:0.1.1`, `:latest` 셋 다 존재. `:latest` 와
  `:0.1.1` 같은 digest 로 정렬됨 (이제는 chandra-b 의 표시 mismatch 도 해소).

### 호스트 메트릭
- `ocrserver-metrics.timer` 활성, 1분 주기 → `/srv/ocrserver/data/metrics.db`
- wrapper 컨테이너는 read-only 로 `/data/metrics.db` mount
- wrapper 가 `/srv/ocrserver/docker-compose.yml` 도 RO 마운트 (`/etc/ocrserver-compose.yml`)
  해서 `/api/services._meta.images` 로 노출 (60s TTL)

## 곧 해야 할 작업

1. **HANDOFF.md 유지** — 다음 작업 끝낼 때 이 파일도 같이 갱신.

(현 시점 운영-side 미결 작업 없음. chandra 0.1.1 swap 완료, image 뱃지 정합,
chandra-b 표시 mismatch 해소.)

## 참고 위치

- 데브로그: `devlog/20260520_013_*.md` ~ `20260521_021_*.md`
- 메모리(자동 컨텍스트): `~/.claude/projects/-home-jikhanjung-projects-ocrserver/memory/`
  - `feedback_dev_vs_ops_host.md` — 이 host 는 dev/빌드 트리, 운영 docker 상태 조회 X
- 메트릭 스크립트: `scripts/metrics_collector.py`, `scripts/systemd/`
- 운영 명령:
  ```bash
  # 모드 전환
  /srv/ocrserver/mode-ocr.sh   # 2 GPU OCR
  /srv/ocrserver/mode-llm.sh   # 1 GPU OCR + 1 GPU LLM
  # 로그
  docker compose -f /srv/ocrserver/docker-compose.yml logs -f wrapper
  # DB 인스펙트 (sudo 없이)
  docker exec ocrserver-wrapper-1 python3 -c "import sqlite3; ..." # /data/*.db
  ```
