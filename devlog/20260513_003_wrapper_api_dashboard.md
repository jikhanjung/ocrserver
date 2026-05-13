# 2026-05-13 — PDF Wrapper API + 웹 대시보드 구현

페이지별 vLLM 호출을 서버 내부로 숨기고, PDF 파일 단위로 제출·폴링하는 비동기 Job API를 FastAPI로 구현. SQLite 영속 저장과 웹 모니터링 대시보드를 함께 추가.

## 배경

기존 `batch_ocr.py` 방식은 클라이언트가 PDF를 직접 페이지로 쪼개고, 동시성(concurrency)을 직접 조율해야 했다. 두 가지 불만:

1. 클라이언트가 서버 내부 상태(GPU 부하, 최적 concurrency)를 알아야 함
2. 요청마다 HTTP 오버헤드

→ 서버에 thin wrapper를 두고 "PDF 하나 던지면 결과 JSON 받는" 인터페이스로 단순화. 내부 concurrency·재시도는 wrapper가 담당.

## 설계 결정: 비동기 Job 패턴

| 방식 | 이유 |
|---|---|
| 동기 응답 | ✗ 수십 초~수 분 대기, 타임아웃 리스크 |
| **비동기 Job** | ✓ POST로 즉시 job_id 반환, GET으로 폴링 |
| 스트리밍 | 클라이언트 구현 복잡 |

## 구현 (`wrapper/`)

### FastAPI 서버 (`main.py`)

- `POST /ocr` — PDF 접수 → job_id 즉시 반환, BackgroundTasks로 처리 시작
- `GET /ocr/{job_id}` — 상태 + 페이지별 결과 반환 (in-memory → DB 순으로 조회)
- `GET /ocr` — Job 목록 (pages 제외 요약)
- `GET /api/stats` — 집계 통계 (카운트, uptime, concurrency)
- `GET /` — 웹 대시보드 (HTML)

처리 흐름:
1. PDF → PyMuPDF로 전 페이지 JPEG 렌더링 (동기, 순차)
2. asyncio.gather + Semaphore(12)로 vLLM에 동시 전송
3. 페이지 완료마다 즉시 DB 기록

**502/503 재시도**: 5s → 15s → 30s → 60s 간격, 최대 4회. vLLM 기동 중 또는 일시적 게이트웨이 오류 대응.

### SQLite DB (`aiosqlite`)

호스트에 마운트: `/srv/ocrserver/data/ocrserver.db`

```
jobs  : job_id, filename, status, submitted_at, completed_at,
        total_pages, done_pages, failed_pages, error
pages : job_id, page_num, status, duration_ms, markdown, error
```

wrapper 재시작 후에도 전체 이력 유지. 활성 job은 in-memory(`_jobs` dict)가 우선, 완료된 job은 DB에서 조회.

### 웹 대시보드 (`dashboard.html`)

- 흰 배경 + Bootstrap 5 라이트 테마
- 상단 카드: 전체 / 처리 중 / 완료 / 일부 오류 / 실패 건수
- Job 테이블: 파일명, Job ID, 상태 배지, 진행률 바, 제출 시각, 소요 시간
- 행 클릭 → 페이지별 상세 인라인 (상태, 소요, OCR 미리보기)
- 5초 자동 폴링 (JS fetch, 페이지 리로드 없음)

## nginx 라우팅 변경

wrapper 추가에 따라 `/api/`, `/ocr`, `/` (루트) 경로를 wrapper로 라우팅:

```nginx
location = /    → wrapper   # 대시보드
location /ocr   → wrapper   # Job API (client_max_body_size 200m)
location /api/  → wrapper   # stats API
location /health→ chandra   # vLLM health
location /      → chandra   # 기존 vLLM API (/v1/*)
```

초기에 `/api/stats`가 chandra로 흘러 404가 난 것을 발견해 수정.

## docker-compose 변경

wrapper 서비스 추가, `/data` 볼륨 마운트:

```yaml
wrapper:
  image: ocrserver-wrapper:latest
  environment:
    VLLM_URL: http://nginx:80
    OCR_CONCURRENCY: "12"
    DB_PATH: /data/ocrserver.db
  volumes:
    - ./data:/data
```

wrapper → nginx:80 → chandra-a/b 로 요청 전달. wrapper는 vLLM 인스턴스 수를 모름 (nginx가 load balance).

## 동작 확인

```
POST /ocr  (kruskal1964.pdf, 15페이지)
→ {"job_id": "b41c324a-..."}

GET /ocr/b41c324a-...  (약 80초 후)
→ {"status": "done", "done_pages": 15, "failed_pages": 0, ...}
평균 29.3초/페이지 (기존과 동일)
```

## 운영 URL

```
http://172.16.112.150:8080/          # 웹 대시보드
http://172.16.112.150:8080/ocr       # Job API
http://172.16.112.150:8080/api/stats # 서버 통계
```
