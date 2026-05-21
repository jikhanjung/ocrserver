# HANDOFF — 2026-05-21

이 파일은 작업 인수인계용. 작업 단위로 갱신.

## 방금 한 작업 (2026-05-20 세션)

`/metrics`(현재 라벨 "통계") 신규 도입부터 헤더 통일/페이지네이션까지
한 세션에 집중됨. wrapper 이미지는 `0.1.0 → 0.1.4` 까지 핀.

| 커밋 | 내용 |
|---|---|
| `b898d54` | scripts/metrics_collector.py + systemd timer + `/api/metrics` + `/metrics` 페이지 (Chart.js 6종) |
| `bdc50ee` | 이미지 0.1.0 핀, wrapper 네임스페이스 `ocrserver-wrapper` → `honestjung/ocrwrapper` |
| `93659aa` | wrapper 0.1.0 Docker Hub push 기록 |
| `c94ab4c` | `pages.completed_at` 컬럼 추가, `/api/metrics` 에 `pages_per_step` 시리즈, 차트 듀얼축 → 0.1.1 |
| `fc06241` | wrapper 0.1.1 push 기록 |
| `cab7ed3` | `/status` 재작성: 모드 배지, OCR 백엔드 카드, LLM 모드별 conditional, 빠른 테스트 정리 → 0.1.2 |
| `dfd5d6c` | wrapper 0.1.2 push 기록 |
| `e168055` | `/metrics` 차트 카드에 "활성 구간 평균 N 페이지/분 · M 문서/분" 텍스트 → 0.1.3 |
| `9a8685c` | devlog 017 |
| `4f4dfa0` | HANDOFF.md 신규 |
| `526755c`, `d14e923` | CLAUDE.md 신규 (세션 시작 시 HANDOFF.md 읽기 지시 + 프로젝트 개요) |
| `17ec29b` | 헤더 통일(`#1a2f5a`), context-strip 분리, `/api/jobs` 페이지네이션, "메트릭"→"통계" 라벨 → 0.1.4 |
| `91ede86` | wrapper 0.1.4 push 기록 |
| `cdf99c8`, `53d5ea2` | HANDOFF 갱신 + 0.1.4 deploy |
| `b54dd38` | CDN 4개 제거, bootstrap/chart.js/adapter 를 `wrapper/static/` 에 내장, nginx 에 `/static/` 라우트 추가 → 0.1.5 |
| `45a5546` | wrapper 0.1.5 push 기록 |
| `d7fdc32`, `2e12236` | HANDOFF 갱신 + 0.1.5 deploy |
| `6071f3d` | `/status` 에 컨테이너 이미지 태그 뱃지 (compose 파싱) → 0.1.6 |
| `ce8592b` | chandra :0.1.1 빌드 12% stuck 으로 :0.1.0 revert + CLAUDE.md 함정 명시 |

상세는 `devlog/20260520_013_*.md` ~ `_020_*.md`.

## 현재 상태 (snapshot)

### 운영 모드
- nginx 모드: **OCR 2 GPU** (`nginx.ocr.conf` 활성, mode chip 상 `2ocr`)
- 운영 디렉터리: `/srv/ocrserver/` (jikhanjung 소유, sudo 없이 docker compose 가능)
- 운영 컴포즈는 prebuilt image 만 참조 (build 는 dev tree 에서)

### 컨테이너 / 이미지
```
SERVICE     IMAGE                         STATUS
chandra-a   honestjung/ocrserver:0.1.0    Up 20h (healthy)
chandra-b   honestjung/ocrserver:latest   Up 42h (이전 세션 시작분, profile=ocr)
nginx       nginx:alpine                  Up 42h
wrapper     honestjung/ocrwrapper:0.1.6   Up (방금 swap, 0.1.5 → 0.1.6)
```

- 빌드된 wrapper 태그: `0.1.0` (`5d6f28afa428`), `0.1.1` (`ec0e23a5b770`),
  `0.1.2` (`ae365f7ab413`), `0.1.3` (`ebc4d672c9fa`), `0.1.4` (`fac3ca93673f`),
  `0.1.5` (`17d5cfbefbb5`), `0.1.6` (`308861f8931a`). 전부 Docker Hub
  `honestjung/ocrwrapper` 에 push 완료, `:latest` 는 0.1.6.
- chandra 이미지는 `:0.1.0` 으로 로컬 retag 만, Hub push 안 함
  (7일 전 pull 본 digest 라 stale 가능성 — `feedback_retag_push_safety` 참고).

### 호스트 메트릭
- `ocrserver-metrics.timer` 활성, 1분 주기 → `/srv/ocrserver/data/metrics.db`
- wrapper 컨테이너는 read-only 로 `/data/metrics.db` mount 해서 `/api/metrics` 서빙
- wrapper 가 `/srv/ocrserver/docker-compose.yml` 도 RO 마운트 (`/etc/ocrserver-compose.yml`)
  해서 `/api/services._meta.images` 로 노출 (60s TTL)

### 작업 상태 (`ocrserver.db.jobs`)
- done: 2953, done_with_errors: 6, failed: 5, processing: 0
- 모든 OCR 완료.

### compose / nginx 파일 상태
- dev tree 와 `/srv/ocrserver/` 모두 wrapper `0.1.6` + 새 nginx conf 일치.
- 정적 자산 + 이미지 표시 검증 완료.

## 곧 해야 할 작업

1. **chandra `:0.1.1` 외부 빌드 + Hub push** (이번 세션 deferred)
   - 이 호스트 빌드는 12% (2/17 files) 에서 reproducibly stuck — CLAUDE.md
     "Known gotcha" 섹션 참조.
   - 다른 망 머신(노트북, RunPod 등) 에서 `docker build -t honestjung/ocrserver:0.1.1
     -t honestjung/ocrserver:latest .` → `docker push` × 2.
   - 그 다음 이 호스트에서 `docker pull honestjung/ocrserver:0.1.1` →
     compose chandra-a/b 를 `:0.1.1` 로 bump → `docker compose up -d` × 2 (cold
     start 4-5분 × 2 GPU).

2. **chandra-b 이미지 표시 정렬** (선택)
   - chandra-b 가 IMAGE 컬럼에 `:latest` 로 표시되는데 운영본 compose 는
     `:0.1.0` 으로 적어둠. 같은 digest 라 동작 동일, 보기엔 어색.
   - 위 1번 작업 시 자연스럽게 같이 정리됨.

3. **HANDOFF.md 유지** — 다음 작업 끝낼 때 이 파일도 같이 갱신.

## 참고 위치

- 데브로그: `devlog/20260520_013_*.md` ~ `_020_*.md`
- 메모리(자동 컨텍스트): `~/.claude/projects/-home-jikhanjung-projects-ocrserver/memory/`
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
