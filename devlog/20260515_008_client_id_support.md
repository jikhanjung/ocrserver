# 20260515_008 — wrapper API에 client_id 추가

## 배경

여러 클라이언트(PaperMeister 외)가 같은 wrapper에 동시 접근하기 시작하면서:

- "이 job 누가 올린 건지" 추적 수단 필요
- 같은 PDF를 다른 클라이언트가 각자 보관하고 싶을 때 dedup이 충돌
- 대시보드/조회 시 클라이언트별 필터링이 필요

`client_id`를 1급 식별자로 도입.

## 변경 내용

### 1. POST /ocr — client_id 수용

- form 필드 `client_id`
- 또는 HTTP 헤더 `X-Client-ID`
- 둘 다 보내면 form 우선, 둘 다 없으면 NULL

```python
async def submit(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    client_id: str | None = Form(None),
    x_client_id: str | None = Header(None),
):
    if client_id is None:
        client_id = x_client_id
    ...
```

### 2. Dedup 키를 (file_hash, client_id)로 분리

기존: `WHERE file_hash=? AND status='done'` (전역 캐시)
신규: `WHERE file_hash=? AND client_id IS ? AND status='done'` (client별 캐시)

`IS`를 쓰는 이유: SQLite에서 `=` 비교는 NULL과 매칭되지 않음. `client_id IS ?`는 파라미터가 NULL이면 `IS NULL`, 값이면 동치로 동작 → 익명 잡끼리도 정상 dedup.

영향: 같은 PDF를 client A와 B가 각자 올리면 **각자 별도로 OCR 수행**. 같은 client가 두 번 올리면 캐시 hit. 익명(NULL)끼리도 캐시 hit.

### 3. GET /ocr — `?client_id=` 필터

```bash
curl 'http://localhost:8080/ocr?client_id=papermeister'
```

NULL filter는 미지원(미지정 시 전체 반환). 필요 시 추후 추가.

### 4. DB 스키마 마이그레이션

`db_init`에서 idempotent ALTER TABLE:

```python
if "client_id" not in cols:
    await _db.execute("ALTER TABLE jobs ADD COLUMN client_id TEXT")
await _db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_client_id ON jobs(client_id)")
```

기존 101개 잡은 모두 `client_id=NULL`로 보존.

### 5. 응답에 client_id 포함

`GET /ocr/{job_id}`, `GET /ocr` 모두 `client_id` 필드 포함.

## 스모크 테스트 결과

이미 done 상태인 Schmitt PDF(`client_id=NULL`)로 8개 시나리오 검증:

| # | 입력 | 기대 | 결과 |
|---|---|---|---|
| T1 | 익명 재업로드 | NULL/NULL 매칭 → cached:true | ✅ `cached:true` |
| T2 | form `client_id=acme` | NULL과 미스매치 → 신규 | ✅ 신규 job 생성 |
| T3 | header `X-Client-ID: bob` | 신규 | ✅ 신규 job 생성 |
| T4 | form=eve + header=mallory | form 우선 → eve로 저장 | ✅ DB에 `eve` 기록 |
| T5 | `GET /ocr?client_id=acme` | T2 잡 1건 | ✅ 1건 |
| T6 | `GET /ocr?client_id=eve` | T4 잡 1건 | ✅ 1건 |
| T7 | `GET /ocr?client_id=mallory` | T4가 form-우선이라 0건 | ✅ 0건 |
| T8 | acme로 두 번째 업로드 (T2 완료 후) | 캐시 hit | ✅ `cached:true` |

## 호환성

- 기존 클라이언트는 변경 없이 동작 (모든 신규 파라미터는 optional)
- 기존 done 잡들(client_id=NULL)은 익명 재업로드 시 그대로 캐시 hit

## 문서 업데이트

- `docs/WRAPPER_API.md`: POST/GET 명세, dedup 동작, 환경변수, 제약사항 갱신 (이전 "메모리 저장" 표현도 SQLite 영속화로 정정)
- `docs/ENDPOINTS.md`: 빠른 사용법에 client_id 예시 추가

## 향후 검토

- `client_id`별 quota / rate-limit
- `client_id`가 없는 익명 요청 거부 옵션 (env로 toggle)
- `GET /ocr?client_id=__none__` 같은 NULL 필터 syntactic sugar
