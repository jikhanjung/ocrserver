# 20260520_018 — 헤더 통일 + 대시보드 페이지네이션 + "메트릭"→"통계" 라벨 (wrapper 0.1.4)

## 변경 사항

### 헤더 통일

세 페이지(`/`, `/status`, `/metrics`) 가 색·구조·브랜드 표기가 제각각이라
페이지를 옮길 때 사이트가 바뀐 느낌이 났음. 통일:

- 배경: `#1a2f5a` (이전 dashboard 의 `#2c3e50` 폐기, status/metrics 톤으로 맞춤)
- 브랜드: `OCR Server` (왼쪽, 홈 링크)
- 네비 링크: `대시보드 / 서비스 상태 / 통계` 셋 모두 표기, 현재 페이지는
  `.nav-link-active` 로 굵게 흰색, 다른 페이지는 `text-white-50` 링크
- 우측: `자동 새로고침 · Ns` 펄스 뱃지 (페이지별 갱신 주기 5/10/30s 명시)

dashboard 에 있던 부가 정보(가동시간, 동시성, OCR 백엔드)는 nav 에서 빼고
navbar 바로 아래 회색 톤 `context-strip` 으로 분리. 현재 모드(`OCR 2 GPU` 등)도
함께 표시.

### "메트릭" → "통계"

UI 라벨만 변경, URL `/metrics` 는 유지 (외부 링크/devlog 안 깨지게).
페이지 `<title>`, 네비 라벨, `<title>` 다 갱신. 코드 식별자
(`metrics.html`, `/api/metrics`, `_METRIC_COLS`) 는 그대로.

### 대시보드 job 목록 페이지네이션

현재 jobs 가 ~2300건이라 매 5초마다 전체 list 끌어오면 부담. 서버 사이드
페이지네이션 도입.

**새 API**: `GET /api/jobs?page=N&per_page=25&client_id=...`

```json
{
  "items": [ ... 25 jobs ... ],
  "total": 2308,
  "page": 1,
  "per_page": 25,
  "pages": 93
}
```

기존 `/ocr` GET (전체 list) 은 외부 클라이언트 호환 위해 그대로 유지.
신규 `/api/jobs` 만 dashboard 가 사용.

내부 helper `db_page_jobs(client_id, limit, offset) → (rows, total)` 추가.
in-flight job(`_jobs` dict) overlay 는 `/api/jobs` 도 동일하게 적용 — queued/
processing 상태는 메모리 진행 상황이 DB 보다 신선.

**UI**:
- 테이블 하단에 `M–N / 총 T건` + `페이지당 [25/50/100]` 셀렉터 + Bootstrap
  pagination (`« ‹ … 3 4 [5] 6 7 … › »`)
- 페이지 이동 / per_page 변경 시 즉시 fetch
- 5초 폴링은 현재 보고 있는 페이지를 갱신 (페이지 1 머무는 동안 새 job 자동
  반영, 다른 페이지로 가면 그 페이지가 계속 갱신됨)

## 배포

이미지: `honestjung/ocrwrapper:0.1.4` (digest `fac3ca93673f...`).
운영본 swap 은 진행 중인 OCR 끝난 뒤:

```bash
cd /srv/ocrserver && docker compose up -d wrapper
```
