# 2026-05-28 · Per-page render in OCR worker + POST handler async (wrapper 0.1.14)

## 증상

PaperMeister 가 Stewart Antarctica (1773p) 제출 후:

```
[21:07:19] Stewart → queued (1773 pages)
[21:07:27 ~ 21:11:31] OCR 0/1773 pages         ← 4+ 분간 0
```

같은 시간 대시보드 (브라우저) 는 "먹통" — `/api/stats` 같은 엔드포인트가
응답 못 함. 반면 PaperMeister 의 poll (`/ocr/{job_id}`) 은 timeout 길어서
결국 응답 받음.

## 원인

`_run()` 이 잡 시작될 때 **PDF 전체 페이지를 1번에 PyMuPDF 로 render +
JPEG + base64 해서 list 로 쌓아두고** 그 다음에 OCR `gather()`:

```python
async def _run(...):
    ...
    async with _render_sem:
        n, todo = await asyncio.to_thread(_render_pdf, pdf_bytes, skip_pages)
    ...
    await asyncio.gather(*[_ocr_page(job, i, b64, client) for i, b64 in todo])
```

1773p 의 경우 `_render_pdf` 는 1773개 페이지를 단일 스레드에서 직렬 처리
(`_render_sem` 가 2로 cap 했지만 잡 1개라 1개만 활성) → ~9분.

이 9분 동안:
- to_thread 안의 PyMuPDF Python loop (`for i in range(n): ... page.get_pixmap
  ... base64.b64encode ... .decode() ... .append`) 이 GIL 을 지속적으로
  잡음 → 메인 thread (asyncio event loop) starve
- Stewart 의 OCR 은 0/1773 (render 끝나야 gather 시작)
- 대시보드 fetch 들이 짧은 timeout 으로 포기

`_render_sem` 은 잡 여러 개 동시 resume 케이스만 잡아주는 부분 미봉책.
잡 1개라도 큰 거 들어오면 동일 hang.

## 수정

**핵심**: render 를 `_run()` 의 upfront 단계에서 빼서 각 `_ocr_page`
worker 안으로 옮김. render 동시성이 OCR 세마포어 (`_sem`, 12개) 와 자연
일치.

```python
def _pdf_page_count(pdf_bytes) -> int:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try: return len(doc)
    finally: doc.close()

def _render_one_page(pdf_bytes, page_num) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[page_num]
        ... # same DPI/zoom/pixmap/base64 logic, just for one page
        return base64.b64encode(pix.tobytes("jpeg")).decode()
    finally: doc.close()


async def _run(job_id, pdf_bytes, skip_pages=None):
    skip_pages = skip_pages or set()
    job = _jobs[job_id]
    try:
        n = await asyncio.to_thread(_pdf_page_count, pdf_bytes)   # ~ms
    except Exception as e:
        ... # mark job failed
    if not skip_pages:
        job.update(total_pages=n, status="processing", pages=[None]*n)
        await db_update_job(job_id, status="processing", total_pages=n)
    todo = [i for i in range(n) if i not in skip_pages]
    async with httpx.AsyncClient(timeout=3000) as client:
        await asyncio.gather(*[_ocr_page(job, i, pdf_bytes, client) for i in todo])


async def _ocr_page(job, page_num, pdf_bytes, client):
    async with _sem:                                              # 12 slots
        b64 = await asyncio.to_thread(_render_one_page, pdf_bytes, page_num)
        ... # existing retry + chandra post + db_upsert_page logic
```

**왜 이게 GIL convoy 안 일어나는가:**
- chandra 가 페이지당 ~25s
- render 는 페이지당 ~0.3s
- 따라서 12개 OCR 슬롯 중 어느 순간에든 보통 0-1개가 render 중, 나머지는
  `await client.post(chandra)` 대기
- 동시 render 스레드 수 ≈ 1 → GIL 압박 거의 없음

**부수 개선**: render 실패 (corrupt page) 가 한 페이지 단위로 catch 됨 →
잡 전체 fail 안 시키고 해당 페이지만 failed 마킹.

### `_render_sem` 제거

`_render_sem` 변수, lifespan 의 `_render_sem = asyncio.Semaphore(2)`,
`_run()` 의 `async with _render_sem` 모두 삭제. per-page 가 자연 페이싱.

### POST `/ocr` 핸들러도 async 정리

이전엔 POST 안에서 동기 작업이 메인 thread 점유:
- `hashlib.sha256(pdf_bytes).hexdigest()` — 500MB 면 ~1-2s
- `fitz.open + len + close` (hint 없을 때) — ~500ms-2s
- `with open(path, "wb"): f.write(pdf_bytes)` — disk write ~2-5s

세 가지 모두 `asyncio.to_thread` 로 옮김. POST 동안에도 대시보드 응답 가능.

## 배포 + 검증

`0.1.13` → `0.1.14`. 진행 중이던 Stewart 가 lifespan resume 으로 이어
처리 (45 → 49 페이지, 다운타임 중 chandra 가 응답 4개 보내준 거).

```
=== /api/stats 5 trials (Stewart OCR 진행 중) ===
  1: HTTP=200 time=2.401s   ← fresh container 첫 hit
  2: HTTP=200 time=0.039s
  3: HTTP=200 time=0.037s
  4: HTTP=200 time=0.053s
  5: HTTP=200 time=0.025s
```

이전: render 중 60s timeout, render-end 후 30ppm.
지금: render 중에도 ms 응답, throughput 동일.

## 한계 / 후속

- 페이지마다 `fitz.open()` 새로 호출 — 큰 PDF 의 헤더 파싱 비용 (~10ms/페이지)
  중복. 1773p 면 ~18s 누적 (전체 OCR 시간 ~수십 분 중). 실제 throughput
  영향 미미. 필요시 per-job PyMuPDF doc 핸들 캐시 가능 (thread safety
  확인 필요).
- `_ocr_page` 가 `pdf_bytes` reference 들고 있음 — Python 참조라 메모리
  하나 (1773개 task = pointer 1773개 = ~14KB). 실 데이터는 1 copy.
- lifespan resume 의 `with open(path, 'rb') as f: pdf_bytes = f.read()` 는
  여전히 sync 디스크 read (startup 동안). 잡 12개면 합 ~수 GB read, 약 50s
  startup 지연. 분 단위 다운은 아니지만 startup 길어짐. 후속 to_thread화
  가능.
