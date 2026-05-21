# HANDOFF — 2026-05-21 (chandra 0.1.1 build/push)

이 파일은 작업 인수인계용. 작업 단위로 갱신.

## 방금 한 작업 (2026-05-21 세션 #2)

- chandra (`honestjung/ocrserver`) **`:0.1.1` 빌드 + Docker Hub push** 완료.
  - Manifest digest: `sha256:f6117fbbb2caa866d8349b72bc0d7d11ecc176ff1e4790562f083774fa3d380c`
  - Image ID: `f6117fbbb2ca`, content 17 GB.
  - `:0.1.1` 과 `:latest` 둘 다 같은 digest 로 Hub 에 올라감.
  - 직전 세션(020)에서 deferred 됐던 항목. KOPRI 호스트의 12% stuck 함정이
    이번엔 **재현되지 않음** — 베이스 이미지를 fresh 로 받으면서 buildkit
    state 가 리셋된 영향일 가능성. 상세는 `devlog/20260521_021_*.md`.
- 직전 세션(2026-05-20)의 wrapper 0.1.0 → 0.1.6 작업은 그대로 운영 중.

| 커밋 (예정) | 내용 |
|---|---|
| TBD | `devlog/20260521_021_chandra_0_1_1_build.md` 추가 + HANDOFF 갱신 |

상세는 `devlog/20260521_021_chandra_0_1_1_build.md`.

## 현재 상태 (snapshot)

### 운영 모드
- nginx 모드: **OCR 2 GPU** (`nginx.ocr.conf` 활성, mode chip 상 `2ocr`)
- 운영 디렉터리: `/srv/ocrserver/` (jikhanjung 소유, sudo 없이 docker compose 가능)
- 운영 컴포즈는 prebuilt image 만 참조 (build 는 dev tree 에서)

### 컨테이너 / 이미지 (운영서버, 직전 세션 기준 — 아직 chandra bump 전)
```
SERVICE     IMAGE                         STATUS
chandra-a   honestjung/ocrserver:0.1.0    Up (healthy)
chandra-b   honestjung/ocrserver:latest   Up (이전 세션 시작분, profile=ocr)
nginx       nginx:alpine                  Up
wrapper     honestjung/ocrwrapper:0.1.6   Up
```

- Docker Hub `honestjung/ocrserver` 상태: `:0.1.0`, `:0.1.1`, `:latest` 셋 다 존재.
  `:0.1.1` 과 `:latest` 가 같은 digest (`f6117fbbb2ca...`).
- 운영서버 chandra 는 아직 `:0.1.0` — 다음 작업에서 `:0.1.1` 로 swap.

### 호스트 메트릭
- `ocrserver-metrics.timer` 활성, 1분 주기 → `/srv/ocrserver/data/metrics.db`
- wrapper 컨테이너는 read-only 로 `/data/metrics.db` mount
- wrapper 가 `/srv/ocrserver/docker-compose.yml` 도 RO 마운트 (`/etc/ocrserver-compose.yml`)
  해서 `/api/services._meta.images` 로 노출 (60s TTL)

## 곧 해야 할 작업

1. **운영서버에서 chandra `:0.1.1` swap** (이번 세션 산출물 활용)
   ```bash
   # 운영서버에서
   docker pull honestjung/ocrserver:0.1.1
   # dev tree 의 docker-compose.yml 에서 chandra-a/b 의 image 태그를
   # :0.1.0 → :0.1.1 로 bump → /srv/ocrserver/ 로 sync
   cp /home/jikhanjung/projects/ocrserver/docker-compose.yml /srv/ocrserver/
   cd /srv/ocrserver && docker compose up -d chandra-a chandra-b
   # 각 GPU 별 cold start 4–5분
   ```
   확인: `/status` 의 이미지 뱃지가 `chandra-a 0.1.1` / `chandra-b 0.1.1`
   로 바뀌면 ok.

2. **chandra-b 이미지 표시 정렬** (1번과 자연스럽게 같이 해소)
   - 운영본 compose 의 `chandra-b: image:` 도 `:0.1.1` 로 명시.

3. **이번 세션 결과 커밋**
   - `devlog/20260521_021_chandra_0_1_1_build.md`
   - `HANDOFF.md` (이 파일)
   - 단일 커밋 권장.

4. **HANDOFF.md 유지** — 다음 작업 끝낼 때 이 파일도 같이 갱신.

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
