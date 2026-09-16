# 046 — wrapper 0.3.0: 도판 분할 잡 API 뼈대 (P02 1단계)

**날짜**: 2026-09-16
**상태**: 배포됨 (07:43 UTC). 워커(2단계)는 아직 없다 — 잡을 받기만 하고 처리하지는 않는다.
**설계**: `devlog/20260916_P02_figure_split_service_design.md` · PaperMeister `docs/figure_pipeline_client_plan.md`

## 무엇을 넣었나

`wrapper/figures.py` — FastAPI 라우터 하나. `main.py` 는 세 줄만 바뀐다(import · lifespan 에서 `db_init`/`cleanup` · role=ocr 일 때
`include_router`). OCR 경로는 건드리지 않았다.

| 경로 | 역할 |
|---|---|
| `HEAD/GET /pdfs/{hash}` · `POST /pdfs` | OCR 없이 PDF 보관/확인. RunPod 시절 논문용 (P16 §2.3) |
| `POST /figures/workspace` · `HEAD /figures/workspace/{hash}/{ocr_digest}` | 클라이언트가 올리는 쪽별 OCR 텍스트. 서버 DB 텍스트는 쓰지 않는다 (PaperMeister 3bd7df9 지적) |
| `POST /figures/{detect\|link\|panels}` | 잡 접수. 프롬프트·스키마는 요청에 실려 온다(D5). 구조 검증만 |
| `GET /figures/{kind}/{job_id}` · `GET /figures/jobs` | 결과·목록 |
| `POST /figures/{kind}/{job_id}/resume` · `POST /figures/worker/resume` | 실패 항목 재큐 · 치명 정지 해제 |
| `GET /api/figures` | 대시보드 요약. `/status` 에 "도판 분할" 카드 |
| `POST /internal/figures/claim` · `…/items/{id}/heartbeat` · `…/items/{id}/result` · `…/worker/status` · `GET …/workspace/…` | 호스트 워커 전용 |

테이블: `figure_workspaces` · `figure_jobs` · `figure_items` · `figure_calls` · `figure_worker`(단일 행). 기존 `jobs`/`pages` 무변.

## 정한 것

- **DB writer 는 wrapper 하나** (P16 §6.1a). 워커는 내부 API 로만 주고받는다. 인증은 `X-Worker-Token`
  (`FIGURES_WORKER_TOKEN`, `.env`) + nginx `/internal/` 은 loopback·docker 브리지(172.18.0.0/16) 만 허용.
- **dedup 키** = `(kind, file_hash, ocr_digest, item 내용, prompt, options)` 해시 + 같은 `client_id` 의 `done` 항목. 입력의
  정체만(P17 §3.5). `force` 로 우회. 캐시 히트는 결과를 복사한 `done` 항목으로 들어가 잡이 바로 끝난다.
- **공평 분배**: claim 은 "마지막 완료가 가장 오래된 클라이언트" 를 먼저(완료 기준 라운드 로빈), 그 안에서 가장 오래된 잡.
  OCR 의 `_FairScheduler` 와 원칙은 같고 구현은 SQL 한 줄.
- **시도·치명**: 항목 3회(`FIGURES_MAX_ATTEMPTS`). `failed` 는 시도가 남으면 즉시 재큐. `fatal`(로그인 만료·CLI 없음·한도)
  은 **시도로 세지 않고** 항목을 큐로 되돌리며 워커를 `paused` 로 — 그 뒤 claim 은 빈손. `budget_exhausted`(세션 상한) 는
  실패와 구분되는 종결 상태.
- **heartbeat 1800 s** 없으면 `processing` 항목을 큐로 (워커 크래시). 세션 상한(detect 10 / link 20 분)보다 길어야 한다.
- **TTL**: 결과 30일 · 작업 폴더 7일, 기동 시 정리. 진실의 원천은 클라이언트 DB.
- 쪽 번호 0-based. bbox 는 4 ints 0..1000, x0<x1, y0<y1.

## 검증

- `wrapper/tests/smoke_figures.py` — 앱을 프로세스 안에서 띄우고 계약 전체를 훑는다: PDF 보관 → 작업 폴더 → 제출(종류 3, 검증
  오류 8종, dedup·force·client 별) → claim(공평 분배) → heartbeat → 결과 4종(done / failed→재큐→소진 / fatal→pause→resume /
  budget) → 잡 롤업 → `/api/figures`. **56 checks passed.** 실행: 
  `docker run --rm -e FIGURES_WORKER_TOKEN=t -v $PWD/wrapper:/app -w /app honestjung/ocrwrapper:0.3.0 python -m tests.smoke_figures`
- 배포 후: `/api/figures` OK · `HEAD /pdfs/{known}` 200 / unknown 404 · `/internal/figures/claim` 토큰 없이 403, 토큰으로 200
  (`item: null`) · `/status` 에 카드 · OCR 회귀 `kruskal1964.pdf` 15/15 (아래 HANDOFF 참고).

## 다음 (2단계)

`scripts/figures_worker.py` + systemd — claim 루프 · 작업 폴더 빌더(내부 API 의 텍스트 + PDF 전 쪽 100dpi 렌더 + 힌트 상자) ·
`codex exec -C` 래퍼 · 스키마 검증 · 5분 간격 · 세션 상한 · 치명 판정(stderr 의 `failed to refresh available models` 노이즈 제외) ·
사용량 기록. 클라이언트의 프롬프트 3벌이 오기 전에도 `scripts/subfigure/astra_panels.py` 의 프롬프트로 panels 를 돌려 볼 수 있다.
