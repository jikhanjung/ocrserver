# HANDOFF — 2026-05-20

이 파일은 작업 인수인계용. 작업 단위로 갱신.

## 방금 한 작업 (2026-05-20 세션)

`/metrics` 페이지(호스트·GPU 메트릭) 신규 도입 → wrapper 이미지 버전 핀
→ 상태 페이지 2 GPU/LLM 부재 모드 대응 → 처리량 메트릭에 페이지 단위 추가.

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

상세는 `devlog/20260520_013_*.md` ~ `_017_*.md`.

## 현재 상태 (snapshot)

### 운영 모드
- nginx 모드: **OCR 2 GPU** (`nginx.ocr.conf` 활성, mode chip 상 `2ocr`)
- 운영 디렉터리: `/srv/ocrserver/` (jikhanjung 소유, sudo 없이 docker compose 가능)
- 운영 컴포즈는 prebuilt image 만 참조 (build 는 dev tree 에서)

### 컨테이너 / 이미지
```
SERVICE     IMAGE                         STATUS
chandra-a   honestjung/ocrserver:0.1.0    Up (healthy, recreated this session)
chandra-b   honestjung/ocrserver:latest   Up 25h (이전 세션 시작분, profile=ocr)
nginx       nginx:alpine                  Up 25h
wrapper     honestjung/ocrwrapper:0.1.2   Up 2h   ← 운영 중 (0.1.3 빌드/push 했지만 미배포)
```

- 빌드된 wrapper 태그: `0.1.0` (`5d6f28afa428`), `0.1.1` (`ec0e23a5b770`),
  `0.1.2` (`ae365f7ab413`), `0.1.3` (`ebc4d672c9fa`). 모두 Docker Hub
  `honestjung/ocrwrapper` 에 push 완료.
- chandra 이미지는 `:0.1.0` 으로 로컬 retag 만, Hub push 안 함
  (7일 전 pull 본 digest 라 stale 가능성 — `feedback_retag_push_safety` 참고).

### 호스트 메트릭
- `ocrserver-metrics.timer` 활성, 1분 주기 → `/srv/ocrserver/data/metrics.db`
- 누적 행 ~338건 (오늘 첫 가동, 약 5시간 30분치)
- wrapper 컨테이너는 read-only 로 `/data/metrics.db` mount 해서 `/api/metrics` 서빙

### 작업 상태 (`ocrserver.db.jobs`)
- done: 2295, done_with_errors: 6, failed: 5, processing: 2
- **현재 OCR 작업 진행 중** — `processing` 2건. 끝나기 전엔 wrapper 교체 보류.

### compose 파일 상태
- `/home/jikhanjung/projects/ocrserver/docker-compose.yml` : wrapper `0.1.3` (다음 배포본)
- `/srv/ocrserver/docker-compose.yml` : wrapper `0.1.3` (이미 sync 됨, 다음 `up -d` 가 픽업)

## 곧 해야 할 작업

1. **현재 OCR 작업 끝나면 wrapper 0.1.3 으로 swap**
   ```bash
   cd /srv/ocrserver && docker compose up -d wrapper
   ```
   이 한 줄이면 됨. compose 가 desired tag `:0.1.3` 와 떠있는 `:0.1.2` mismatch
   감지 → recreate. `--force-recreate` 불필요. lifespan resume 이 in-flight job
   픽업하므로 데이터 손실 없음 ([[devlog/20260515_010_lifespan_resume]]).

   교체 후 변경 사항이 한꺼번에 반영됨:
   - 페이지 단위 처리량 (0.1.1)
   - 새 `/status` 페이지 — 2 GPU + LLM 부재 모드 대응 (0.1.2)
   - `/metrics` 차트 활성 구간 평균 텍스트 (0.1.3)

2. **chandra-b 이미지 표시 정렬** (선택)
   - chandra-b 가 IMAGE 컬럼에 `:latest` 로 표시되는데, 운영본 compose 는
     `:0.1.0` 으로 적어둠. 같은 digest 라 동작은 동일하지만 보기엔 어색.
   - `docker compose up -d chandra-b` 하면 desired tag 일치 위해 recreate
     (`--profile ocr` 필요할 수 있음). 정리 차원이라 급하지 않음.

3. **chandra Docker Hub push 결정**
   - `honestjung/ocrserver:0.1.0` 로컬에만 있음.
   - 다른 호스트 배포 / 재현 빌드 필요해지면, 그때 Hub 의 현재 `:latest`
     digest 와 비교 후 push 결정 ([[feedback_retag_push_safety]]).

4. **HANDOFF.md 유지** — 다음 작업 끝낼 때 이 파일도 같이 갱신.

## 참고 위치

- 데브로그: `devlog/20260520_013` ~ `_017_*.md`
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
