# 20260513_006 — PDF 중복 제거 및 파일 저장

## 작업 내용

### 1. PDF 중복 제거 (deduplication)

동일 PDF 재전송 시 OCR을 다시 돌리지 않고 기존 결과를 즉시 반환한다.

**우선순위**:
1. SHA-256 hash 일치 → 즉시 캐시 반환
2. hash 없는 구 job: 파일명 + 페이지 수 일치 → 캐시 반환 + hash 소급 기록
3. 둘 다 없으면 → 새 job 생성

`POST /ocr` 응답에 `cached` 필드 추가:
```json
{"job_id": "기존id", "cached": true}   // 캐시 히트
{"job_id": "새id",   "cached": false}  // 신규 처리
```

### 2. PDF 파일 저장

업로드된 PDF를 `/data/pdfs/{sha256}.pdf`로 저장.
- 같은 파일은 중복 저장하지 않음 (`os.path.exists` 체크)
- `PDF_DIR` 환경변수로 경로 변경 가능 (기본값 `/data/pdfs`)
- `/data`는 이미 마운트된 볼륨이므로 compose 변경 없음

### 3. DB 스키마 변경

`jobs` 테이블에 `file_hash TEXT` 컬럼 추가. 기존 DB는 시작 시 자동 마이그레이션:

```python
async with _db.execute("PRAGMA table_info(jobs)") as c:
    cols = {row[1] for row in await c.fetchall()}
if "file_hash" not in cols:
    await _db.execute("ALTER TABLE jobs ADD COLUMN file_hash TEXT")
await _db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_file_hash ON jobs(file_hash)")
```

### 4. httpx 타임아웃 조정

서버↔vLLM 간 타임아웃을 300s → 3000s로 변경. 클라이언트 600s 타임아웃 문제로
원인 파악한 결과 서버 쪽 문제가 아님이 확인됐으나, 대용량 PDF 처리 안정성을 위해
유지.

---

## 트러블슈팅

**`no such column: file_hash` 시작 실패**

`executescript` 안에 `CREATE INDEX IF NOT EXISTS idx_jobs_file_hash ON jobs(file_hash)`
를 넣었는데, `executescript`는 `ALTER TABLE` 마이그레이션보다 먼저 실행됨.
기존 DB에서 컬럼이 없는 상태로 인덱스 생성 시도 → 오류.

해결: 인덱스 생성 구문을 `executescript` 밖으로 꺼내 `ALTER TABLE` 이후에 실행.
