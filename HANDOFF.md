# HANDOFF — 2026-05-21 (chandra 0.1.1 swap + web mode switch)

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
- 운영 호스트 세션 #5: 모드 버튼 다듬기 → wrapper `0.1.8`.
  - 현재 모드 버튼은 disabled + filled 색으로 강조 (클릭 자체 차단).
  - 버튼 라벨 `OCR×2` / `OCR+LLM` 로 명료화.
  - mode-{ocr,llm}.sh 의 `.env` overwrite 버그 수정 (sed in-place 로
    `OCR_CONCURRENCY` 만 갱신, `MODE_TOKEN` 등 보존).
  - 상세 `devlog/20260521_023_*.md`.
- 운영 호스트 세션 #4: 웹에서 모드 전환 + wrapper 재시작 동안 nginx 의
  "재시작 중" 자동 새로고침 페이지 (`/status` 의 `→ OCR` / `→ LLM` 버튼).
  - 새 wrapper 이미지 `:0.1.7` (digest `600a9d7213f1...`) build + Hub push +
    swap 완료.
  - `nginx-errors/` 디렉터리 dev tree 추가 + nginx 컨테이너에 RO 마운트.
    양쪽 nginx conf 에 `proxy_intercept_errors` + `error_page → /__restarting`.
  - 호스트 `/etc/systemd/system/ocrserver-mode-switch.{path,service}` 활성.
    `/srv/ocrserver/data/mode_request` 가 생기면 `mode_switcher.sh` 가
    `mode-{ocr,llm}.sh` 실행. docker socket 노출 없음.
  - `.env` 에 `MODE_TOKEN` 추가, wrapper env 로 전달. (값은 `.env` 파일 참조.)
  - 상세는 `devlog/20260521_022_*.md`.
- 직전 세션(2026-05-20)의 wrapper 0.1.0 → 0.1.6 작업은 그대로 운영 중.

상세는 `devlog/20260521_021_*.md` ~ `_022_*.md`.

## 현재 상태 (snapshot)

### 운영 모드
- nginx 모드: **OCR 2 GPU** (`nginx.ocr.conf` 활성, mode chip 상 `2ocr`)
- 운영 디렉터리: `/srv/ocrserver/` (jikhanjung 소유, sudo 없이 docker compose 가능)
- 운영 컴포즈는 prebuilt image 만 참조 (build 는 dev tree 에서)

### 컨테이너 / 이미지 (운영서버)
```
SERVICE     IMAGE                         STATUS
chandra-a   honestjung/ocrserver:0.1.1    Up (healthy)
chandra-b   honestjung/ocrserver:0.1.1    Up (healthy)
nginx       nginx:alpine                  Up (방금 recreate, errors/ 마운트)
wrapper     honestjung/ocrwrapper:0.1.8   Up (방금 swap)
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

## 🚨 진행 중 인시던트 — NVIDIA 드라이버 mismatch (2026-05-21 ~07:00 UTC)

세션 #6 에서 OCR×2 → OCR+LLM 모드 전환 (`/status` 버튼) 테스트하다 mode-llm.sh
가 llm 컨테이너 띄우는 단계에서 실패. 원인:
`Failed to initialize NVML: Driver/library version mismatch — NVML library
version: 595.71`. 호스트 unattended-upgrades 가 NVIDIA 드라이버 userland 만
업그레이드, 커널 모듈 reload 안 된 상태. 06:04 UTC 에 nvidia-persistenced
가 SIGTERM 으로 stop. 이미 떠있던 chandra-a 는 GPU 0 reserve 중이라 영향
없지만 새 GPU 컨테이너 (llm) 시작 불가.

### 현재 상태 (reboot 직전)
- chandra-a: Up healthy (GPU 0, 영향 없음)
- chandra-b: stopped (mode-llm.sh 가 시작에 stop 함)
- llm: 시작 실패 (NVML mismatch)
- nginx: Up — 다만 `nginx.conf` 가 mode-llm.sh 가 복사한 `nginx.llm.conf` 상태
  (`/llm/` route 있으나 llm 컨테이너 없음 → 502)
- wrapper 0.1.8: Up — `_mode_switching=True` 갇혀서 새 OCR POST 503 거부 중
- mode probe: `1ocr` (chandra-a 만 alive)

### 복구 절차 (reboot 후)
```bash
# 1. 부팅 확인 + nvidia-smi 정상 동작 (NVML version 일치)
nvidia-smi

# 2. 컨테이너 상태 확인
cd /srv/ocrserver && docker compose ps
#  - chandra-a 는 unless-stopped 로 auto-restart
#  - chandra-b 는 stopped 였으므로 auto-restart 안 함
#  - nginx 는 nginx.llm.conf 인 상태 그대로
#  - wrapper 는 fresh start → _mode_switching 자동 리셋

# 3. OCR×2 모드로 복귀 (mode-ocr.sh 가 모든 정리 해줌)
/srv/ocrserver/mode-ocr.sh
#  - chandra-b 기동 (이번엔 nvidia OK 라서 정상)
#  - chandra-b health 대기 ~5분
#  - nginx.conf ← nginx.ocr.conf
#  - wrapper recreate
#  - 끝나면 mode probe 가 '2ocr' 로 복귀

# 4. 검증
curl -s http://localhost:8080/api/services | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['_meta']['mode'])"
# 기대값: 2ocr
```

mode 전환 자체 검증은 OCR×2 정상화된 다음 다시 시도 (이번엔 nvidia OK
이므로 llm 컨테이너 정상 기동되어야 함).

## 곧 해야 할 작업

1. **위 복구 절차 실행 (reboot + mode-ocr.sh)** ← 최우선

2. **모드 전환 동작 실측 재시도** (1번 완료 후)

3. **HANDOFF.md 유지** — 복구 끝나면 이 인시던트 섹션 제거.

## 참고 위치

- 데브로그: `devlog/20260520_013_*.md` ~ `20260521_023_*.md`
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
