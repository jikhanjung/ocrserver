# 2026-05-28 · `_run()` 페이지 래스터화를 thread 로 분리 (wrapper 0.1.10)

## 증상

PaperMeister 클라이언트가 5개 연속 POST `/ocr` 에서 60s `Read timed out`.
`nginx` access log 는 모두 **499** (client closed connection before response).
같은 윈도우에 `/api/*` 폴링도 멈춰 보임. wrapper 로그에는 그 시간대 POST
가 아예 안 찍힘 (uvicorn 은 응답 시점에 log).

## 원인

`wrapper/main.py:637 _run()` — `async def` 인데 안에서 PDF 모든 페이지를
**동기 루프**로 래스터화:

```python
async def _run(job_id, pdf_bytes, skip_pages=None):
    ...
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n = len(doc)
    ...
    todo = []
    for i in range(n):
        if i in skip_pages:
            continue
        page = doc[i]
        ...
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))    # sync C
        todo.append((i, base64.b64encode(pix.tobytes("jpeg")).decode()))  # sync
    doc.close()
    # 여기까지 await 가 하나도 없음
    async with httpx.AsyncClient(...) as client:
        await asyncio.gather(*[_ocr_page(...) for ...])
```

페이지당 render+jpeg+base64 가 ~0.3s (150 DPI, 일반 학술 PDF 기준). 사건
당시 두 큰 잡이 같은 시간에 접수됨:

- ueberdassiluris (220p) → ~66s freeze
- canadiannaturali (518p) → ~150s freeze

두 `_run()` 이 연속으로 동기 루프 돌면서 wrapper 이벤트 루프가 200+ 초간
freeze. 그 윈도우에 들어온 5개 POST 가 wrapper 까지는 가서 (`await
file.read()` 도 안 시작) nginx 가 30s `proxy_read_timeout` 에 504 만들고
client (PaperMeister, 60s timeout) 가 그 전에 끊음 → nginx 입장에서는 499.

작은 잡들이 평소 잘 돌던 건 render 가 1초 미만이라 freeze 가 안 보였을 뿐.
518p 같은 outlier 가 들어오면 즉시 터짐.

## 수정

`_run()` 의 sync 부분을 헬퍼 함수 `_render_pdf()` 로 분리하고
`asyncio.to_thread()` 로 호출:

```python
def _render_pdf(pdf_bytes, skip_pages):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        n = len(doc)
        todo = []
        for i in range(n):
            if i in skip_pages: continue
            ...
            pix = page.get_pixmap(...)
            todo.append((i, base64.b64encode(pix.tobytes("jpeg")).decode()))
        return n, todo
    finally:
        doc.close()


async def _run(job_id, pdf_bytes, skip_pages=None):
    skip_pages = skip_pages or set()
    job = _jobs[job_id]
    try:
        n, todo = await asyncio.to_thread(_render_pdf, pdf_bytes, skip_pages)
    except Exception as e:
        ...
    ...
```

`fitz.get_pixmap` 등 PyMuPDF C 코드는 GIL 을 풀어 주므로 thread executor
에서 진짜 병렬로 돈다. 2개 큰 잡이 동시에 들어와도 각각 다른 스레드에서
render 하면서 이벤트 루프는 자유.

POST `/ocr` handler 의 sync 부분 (`hashlib.sha256(pdf_bytes)`, `fitz.open
+ len`) 은 작은 비용이라 (39MB 짜리 PDF 도 ~200ms) 이번에는 손 안 댐.
나중에 동일 패턴 나오면 같이 to_thread 화.

## 배포 + 그 와중 잡힌 부수 버그

`honestjung/ocrwrapper:0.1.9` → `0.1.10`. 운영 디렉터리에 compose 복사,
`docker compose up -d --no-deps wrapper`.

배포 직후 `/api/stats` 가 HTML (`__restarting` 페이지) 을 200 으로
반환하는 버그 재발견. 어제 (devlog 027) 에서 `nginx.ocr.conf` `/api/` 의
`error_page = /__restarting` 을 제거했는데, **그 변경이 실제 nginx
컨테이너에는 적용 안 돼 있었다**.

**원인**: Docker single-file bind mount (`./nginx.conf:/etc/nginx/nginx.conf:ro`)
는 컨테이너 시작 시점의 **호스트 inode 를 잡는다**. 호스트에서 `cp` 로
파일을 교체하면 새 inode 가 생기고, 컨테이너는 옛 inode 의 옛 내용을
계속 참조. `nginx -s reload` 도 컨테이너 내부 fd 로 옛 파일을 다시 읽어
효과 없음.

```
# host: 2026-05-27 에 cp 로 교체
$ stat -c '%i %y' /srv/ocrserver/nginx.conf
3674466 2026-05-27 05:55:26

# container: 여전히 5/21 inode
$ docker exec ocrserver-nginx-1 stat -c '%i %y' /etc/nginx/nginx.conf
3670057 2026-05-21 07:32:50
```

**복구**: `docker compose up -d --no-deps --force-recreate nginx`. 컨테이너
재기동 시점에 새 inode 로 bind. 이후 컨테이너/호스트 inode 일치, `/api/`
location 에서 `error_page` 사라진 게 실제로 적용됨.

**향후**: nginx config 변경 후에는 `nginx -s reload` 가 아니라
`docker compose up -d --no-deps --force-recreate nginx` 를 써야 한다.
`mode-*.sh` 가 `cp nginx.X.conf nginx.conf` 한 다음 reload 로 끝내고 있는
것도 같은 버그에 취약 — 별도 fix 필요 (`mode-ocr.sh` / `mode-llm.sh`).

## 검증

배포 ~5분 후 (큰 잡 두 개 render 완료 후):

```
=== throughput ===
last 1m: 33 ok pages (33.0 ppm)
last 3m: 95 ok pages (31.7 ppm)
last 5m: 149 ok pages (29.8 ppm)

=== /api/stats 5 trials (이전에는 60s timeout) ===
trial 1: HTTP=200 time=0.104s
trial 2: HTTP=200 time=0.051s
trial 3: HTTP=200 time=0.050s
trial 4: HTTP=200 time=0.035s
trial 5: HTTP=200 time=0.039s
```

이벤트 루프 freeze 해소 확인. throughput 정상.

## 한계 / 후속

- `_run()` 은 여전히 **전체 render → 일괄 gather()** 구조라, 큰 잡은
  render 끝날 때까지 OCR 0 페이지. 페이지 단위 pipeline 으로 바꾸면 더
  매끄럽지만 변경 범위 커서 일단 보류.
- 12-slot 세마포어를 한 잡이 독점하면 후속 잡은 OCR 대기. 페어니스
  스케줄링이 필요해지면 별도 작업.
- `mode-*.sh` 의 nginx reload 가 file inode 갈아치우는 변경에 무효 → 별건.
