# 20260520_015 — 처리량 메트릭에 페이지 카운트 추가 (wrapper 0.1.1)

## 변경

[[20260520_013_host_metrics]] 의 OCR 처리량 차트는 `jobs.completed_at` 기준으로
**문서/분** 만 보여줬음. PDF 한 건이 1쪽이든 500쪽이든 1로 카운트돼서 실제 OCR
백엔드 부하와 잘 안 맞음. 페이지 단위 처리량을 같이 노출.

### 스키마 마이그레이션

```sql
ALTER TABLE pages ADD COLUMN completed_at REAL;
CREATE INDEX IF NOT EXISTS idx_pages_completed_at ON pages(completed_at);
```

기존 행은 `completed_at = NULL` 그대로. 새 wrapper 가 페이지를 처리할 때부터
타임스탬프가 채워지고, 이전 페이지들은 차트에 안 나타남 — 과거 throughput 은
복원 불가능, 신규부터 누적되는 정책.

### 코드 변경

- `db_upsert_page` 가 `INSERT OR REPLACE` 시 `completed_at=time.time()` 동시 기록
  (positional VALUES 에서 named columns 로 전환 — 컬럼 추가 시 안전).
- `/api/metrics` 에 `pages_per_step` 시리즈 추가
  (`SELECT ... FROM pages WHERE status='ok' GROUP BY bucket`).
- `metrics.html` OCR 처리량 차트를 듀얼 Y축으로:
  - 좌축 (teal, 채움): 페이지/분 — 주 지표
  - 우축 (navy): 문서/분 — 보조

## 배포

이미지: `honestjung/ocrwrapper:0.1.1` (id `ec0e23a5b770`).

운영 중 OCR 가 끝난 뒤 swap 예정:

```bash
cd /srv/ocrserver && docker compose up -d wrapper
```

`docker-compose.yml` 의 `image:` 가 `:0.1.1` 로 이미 갱신돼 있어
`--force-recreate` 불필요. lifespan resume 이 processing 중인 job 을
픽업하므로 OCR 가 끊기더라도 페이지 진행 상태에서 이어서 처리됨
([[20260515_010_lifespan_resume]] 참조).

## 측정 기준 메모

- **pages_per_step**: `pages.status='ok'` 만 카운트 (실패/재시도 제외). 단위
  시간당 실제로 OCR 백엔드가 성공시킨 페이지 수.
- **jobs_per_step**: `jobs.status IN ('done','done_with_errors')`. 부분
  실패한 PDF 도 "완료" 로 침. 사용자 체감 throughput 에 가까움.

두 값의 비율(페이지/문서)이 평균 PDF 크기 추정치로 쓸 수 있음.
