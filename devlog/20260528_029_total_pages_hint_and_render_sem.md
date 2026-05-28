# 2026-05-28 · `total_pages` form hint + render concurrency cap (wrapper 0.1.11 → 0.1.12)

## 두 가지 변경, 한 번의 incident

### 변경 (a) — POST /ocr 에 `total_pages` form hint

**원인 (PaperMeister 쪽 진단)**: `wrapper_submit()` 이 제출 직후 1회만
`/ocr/{id}` 폴링 (10s timeout) 해서 `total_pages` 를 받았는데, 서버가 큰
PDF 를 아직 파싱 중이면 `total_pages=0` 반환. 클라이언트의
`_submit_next` 가 `tp or 1` 폴백을 써서 큰 책이 "1 페이지짜리 잡" 으로 잡힘
→ 큐 깊이 계산이 과소계상되어 `_queued_pages() < min_queued_pages(=12)` 가
계속 참 → 12개 연속 제출 누적.

**서버 쪽 매칭 변경 (이번 PR)**:
```python
@app.post("/ocr")
async def submit(
    ...,
    total_pages: int | None = Form(None),   # ← 신규
    ...,
):
    ...
    if total_pages is None or total_pages <= 0:
        # 기존 경로: 서버가 파싱해서 카운트
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            total_pages = len(doc)
            doc.close()
        except Exception:
            total_pages = 0
    ...
    _jobs[job_id] = { ..., "total_pages": total_pages, ... }   # 0 대신 실제값
    await db_create_job(...)
    if total_pages:
        await db_update_job(job_id, total_pages=total_pages)
    ...
    return {"job_id": job_id, "cached": False, "total_pages": total_pages}
```

- 클라이언트가 hint 주면 sync 파싱 스킵
- 첫 poll 부터 정확한 `total_pages` 반환
- `_run()` 이 `_render_pdf` 로 실제 카운트 잡아 DB+_jobs 덮어쓰므로 client
  가 거짓말해도 자동 보정
- POST 응답에도 `total_pages` 포함 → 클라이언트가 poll 안 해도 됨

### 변경 (b) — `_render_sem` 도입 (긴급 추가)

(a) 배포 시 wrapper recreate → lifespan resume → 12개 in-flight 잡이 모두
동시에 `_run()` 시작 → 각각 `await asyncio.to_thread(_render_pdf, ...)`.
**12 thread 가 동시에 PyMuPDF 호출하면서 GIL convoy 발생 → 이벤트 루프
starve, wrapper 100% CPU 인데 어떤 HTTP 요청도 응답 못 함.**

PyMuPDF C 코드는 부분적으로 GIL 을 풀어주지만, per-page Python loop
오버헤드 + base64.b64encode + .decode() 등이 충분히 GIL 을 잡고 있어서
N 개 동시 render 가 메인 thread (asyncio event loop) 를 굶긴다.

**수정**: `_render_sem = asyncio.Semaphore(2)` 추가, `_run()` 의
to_thread 호출을 감쌈. 최대 2개 render 만 동시 — CPU 는 충분히 쓰면서
이벤트 루프 살아 있음.

```python
_render_sem: asyncio.Semaphore

async def lifespan(app):
    ...
    _render_sem = asyncio.Semaphore(2)
    ...

async def _run(...):
    ...
    async with _render_sem:
        n, todo = await asyncio.to_thread(_render_pdf, pdf_bytes, skip_pages)
    ...
```

## Incident 진행 + 복구

1. `0.1.11` 빌드 → 배포 → lifespan resume 시 12잡 동시 render → wrapper
   completely unresponsive (`/api/stats` 60s timeout, `/ocr` 도 같은 상태).
2. `_render_sem=2` 추가 → `0.1.12` 빌드 → 배포 → **여전히 hang** (12잡 동시
   resume 자체가 trigger, semaphore 만으론 부족).
3. **데이터 보존 결정**: Stewart Antarctica (1343/1773 = 76% done) 만 살리고
   나머지 11잡 (모두 0/N 상태) 를 DB 에서 `failed` 마킹.
   - wrapper stop
   - 1회용 컨테이너로 SQL UPDATE (host python 은 root-owned DB write 불가)
     ```
     docker run --rm -v /srv/ocrserver/data:/data \
       --entrypoint python3 honestjung/ocrwrapper:0.1.12 \
       -c "import sqlite3; db=sqlite3.connect('/data/ocrserver.db'); \
           db.execute(\"UPDATE jobs SET status='failed', error='...' \
                        WHERE status='processing' AND \
                        (done_pages IS NULL OR done_pages=0)\"); \
           db.commit()"
     ```
   - wrapper start → lifespan resume "[resume] re-spawned 1 'processing' job(s)"
4. `/api/stats` 50-110ms 회복, mode=2ocr, both chandra healthy.

## 검증

```
=== /api/stats 5 trials ===
trial 1: HTTP=200 time=0.108s
trial 2: HTTP=200 time=0.050s
...

=== POST /ocr with hint=4 ===
{"job_id":"fa171051-...","cached":false,"total_pages":4}
```

## 한계 / 후속

- **render_sem=2 는 미봉책**. lifespan resume 에서 12+ in-flight 잡이 다시
  쌓이면 동일 hang 가능 (semaphore 가 2 로 막아도 메모리/스레드 lifecycle
  이 압박). 진짜 해법은 **render 를 per-page 로 OCR worker 안으로** 옮기는
  것 (각 _ocr_page 가 자기 페이지만 그 자리에서 render → chandra). render
  concurrency 가 OCR 세마포어 (12) 와 자연스럽게 일치.
- 12잡 동시 resume 자체도 한꺼번에 PDF 로드 (~50s wait) 후 폭발적 task
  생성. resume 을 staggered 하게 (sleep 사이에 끼워서) 띄우는 것도 도움.
- 11잡 abandoned: PaperMeister 의 client fix (devlog 028 의 큐 깊이 정확화)
  적용된 버전이 다시 보내면 1-2개씩 정상 처리. 사용자 작업물 손실 없음
  (모두 0 페이지 done 이었음).

## 교훈 (메모리화)

PyMuPDF (그리고 비슷한 ext C 라이브러리) 를 `asyncio.to_thread` 로
여러 잡에서 동시에 호출하면 GIL convoy 로 이벤트 루프가 굶을 수 있음.
"C 코드면 GIL 풀어줘서 괜찮겠지" 는 partial. CPU 무거운 사이클이 N 개
스레드에서 동시에 돌면 메인 thread 슬라이스 거의 0 이 된다. 해법:
- semaphore 로 동시 to_thread 호출 제한 (현재 임시 해법, N=2)
- 혹은 process pool (ProcessPoolExecutor) — 진짜 병렬, 단 직렬화 오버헤드
- 혹은 work 단위를 작게 잘라 한 OCR 슬롯 안에서 동기적으로 처리 (per-page
  render)
