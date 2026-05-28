# 2026-05-28 · POST /ocr dedup 이 in-flight 잡까지 매칭 (wrapper 0.1.13)

## 발견 경위

오늘 (devlog 029) wrapper 가 hang 한 직접 트리거 중 하나가 **같은 PDF
중복 잡**. PaperMeister 가 Stewart Antarctica encyclopedia (1773p) 를
처리 중인 상태에서 클라이언트 내부 상태를 잃고 (poll 끊김, 재시작 등)
같은 파일을 다시 POST → 서버가 두 번째 잡 (`782220c1`) 생성. 이미
processing 중인 `84c9d745` 와 별개로 또 1773p render 가 to_thread 에 들어가
GIL 압박 증폭.

원인은 `db_find_done_by_hash` 의 SQL:

```sql
WHERE file_hash=? AND client_id IS ? AND status='done'
```

`status='done'` 만 매칭. processing/queued 는 dedup 안 됨 → 같은 파일
재제출이 그대로 새 잡 생성.

## 수정

함수 이름 + SQL 둘 다 변경:

```python
async def db_find_existing_by_hash(file_hash, client_id):
    """Matches in-flight (queued/processing) as well as done. Failed jobs
    intentionally NOT matched so the client can retry. Done wins when both
    exist."""
    async with _db.execute(
        "SELECT job_id FROM jobs WHERE file_hash=? AND client_id IS ? "
        "AND status IN ('done','processing','queued') "
        "ORDER BY (status='done') DESC, submitted_at DESC LIMIT 1",
        (file_hash, client_id),
    ) as c:
        row = await c.fetchone()
    return await db_get_job(row["job_id"]) if row else None
```

submit handler 의 응답에 `in_progress: bool` 추가:

```python
if existing:
    return {
        "job_id": existing["job_id"],
        "cached": True,
        "in_progress": existing.get("status") != "done",
        "total_pages": existing.get("total_pages", 0),
    }
```

`in_progress=true` → 클라이언트가 "데이터 아직 없음, polling 필요" 로 해석.
`in_progress=false` → 즉시 markdown 사용 가능.

`db_find_done_by_filename` (legacy filename dedup, `file_hash IS NULL` 행
만 매칭) 은 그대로. 신규 잡은 항상 file_hash 가 있으므로 in-flight 매칭
은 사실상 hash 함수에서만 일어남.

## 설계 결정

- **client_id scoping 유지** — 다른 client 끼리 dedup 안 일어남 (데이터
  leak 방지). 같은 파일을 두 client 가 동시 처리해도 별도 잡.
- **failed 제외** — chandra 가 죽었을 때 페이지 실패 → 사용자가 재제출
  하면 새 시도 필요. 실패한 잡 id 돌려주면 의미 없음.
- **done_with_errors 제외** — 부분 성공한 잡은 "다시 시도해서 실패 페이지
  도 채워봐" 하는 의도일 수 있음. 안전하게 새 잡 생성.
- **tiebreak: done 우선** — 가끔 done 잡이 있는데 새로 또 누가 in-flight
  로 같은 파일 올렸을 때 (예: dedup race 윈도우), done 잡 돌려주는 게
  사용자에게 더 빠름.

## 클라이언트 영향 (PaperMeister 등)

- 기존 `cached: true` 처리 코드는 그대로 동작 — `in_progress` 필드는
  신규라 무시해도 backward-compat. polling 로직이 `cached` 와 무관하게
  `/ocr/{job_id}` status 보는 표준 패턴이면 자연스럽게 동작.
- 응답 `job_id` 가 클라이언트가 보낸 게 아니라 동일 파일의 이전 in-flight
  잡 id. 추적/로깅 시 헷갈릴 수 있음 — 같은 데이터 가리키니까 polling
  결과는 동일.
- 향후 client 가 `in_progress` 를 명시적으로 다루고 싶으면 "in-flight
  resubmission 감지 → 사용자에게 진행 상태 알림" 같은 UX 가능.

## 검증

로컬 smoke (`0.1.13` 이미지):

```
1. POST 첫 제출 → {job_id: X, cached: false, total_pages: 2}
2. 같은 파일 + 같은 client_id 재제출 (X 아직 processing)
   → {job_id: X (동일), cached: true, in_progress: true, total_pages: 2}
3. 같은 파일 + 다른 client_id 제출
   → {job_id: Y (신규), cached: false, total_pages: 2}
```

3 케이스 다 통과. 운영 배포 후 `/api/stats` 정상 응답 확인 (114ms).

## 배포

- 이미지: `0.1.12` → `0.1.13`
- 배포 직전 0건 in-flight 상태 (devlog 029 의 wipe 후) 라 lifespan resume
  부담 없음. ~1s 안에 startup 완료.
- 향후 동일 사고 (PaperMeister 가 in-flight Stewart 재제출) 시 두 번째
  POST 가 첫 번째 job_id 반환, 추가 _run() 안 띄움.

## 한계

- dedup race window 여전히 존재. 두 POST 가 거의 동시에 도착하면 둘 다
  `db_find_existing_by_hash` 가 None 반환 → 둘 다 새 잡 생성. 진짜
  엄격하게 막으려면 (file_hash, client_id) 에 UNIQUE 제약 + INSERT
  conflict handling 필요. 현재 빈도 낮아서 보류.
- 함수 이름 변경 외에는 schema 변경 없음. DB 마이그레이션 불필요.
