# 2026-05-27 · /api/ no-store + nginx error passthrough (wrapper 0.1.9)

## 증상

`/metrics` 페이지의 **6시간 탭만** 그래프가 안 나옴. 1h, 24h, 7d 등 다른
탭은 정상. Edge 콘솔:

```
metrics:230 SyntaxError: Unexpected token '<', "<!doctype "... is not valid JSON
```

즉 `fetch('/api/metrics?range=6h')` 가 **HTML 을 200 으로 받아오고** `r.json()`
파싱 단계에서 터짐. Chrome 에서도 같은 증상 재현됨 (브라우저 무관).

## 원인 (두 가지가 결합)

1. **nginx `/api/` 의 친절한 재시작 페이지**
   `nginx.ocr.conf` / `nginx.llm.conf` 의 `/api/` location 에
   ```
   proxy_intercept_errors on;
   error_page 502 504 = /__restarting;
   ```
   가 걸려 있었음. `error_page ... = /uri` 는 **새 URI 의 상태를 그대로 쓴다**
   (=`__restarting` 의 200) → wrapper 가 잠깐 죽으면 `/api/metrics?range=6h`
   가 `<!doctype html>...` body + HTTP 200 으로 응답된 적이 있음.

2. **응답에 Cache-Control 헤더가 없음**
   FastAPI 의 기본 응답에는 캐시 헤더가 없고, nginx 도 안 붙여 줌. 브라우저는
   "Cache-Control 없음 + 200 OK" 를 heuristic 으로 캐시. 이후 wrapper 가
   복구돼도 그 URL 만 stale HTML 을 계속 서빙.

### 왜 6h 만?

- 6시간 탭이 metrics 페이지의 **기본 active 탭** (`metrics.html:48`). 페이지
  열면 무조건 첫 fetch 가 `?range=6h`.
- wrapper 재시작 (예: 0.1.7→0.1.8 배포, freeze 인시던트 복구 등) 윈도우에서
  hit 된 URL 은 6h 뿐이라, 6h 만 stale HTML 이 캐싱됨.
- 1h, 24h 는 사용자가 클릭해야 fetch 되므로 그 타이밍에 잡힌 적이 없음 → 정상.

## 수정

### A. FastAPI middleware: `/api/*` 에 `Cache-Control: no-store`

`wrapper/main.py`:

```python
@app.middleware("http")
async def _no_store_api(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response
```

JSON 응답을 브라우저가 캐싱하면 안 되는 건 일반 원칙. 페이지 (`/`, `/status`,
`/metrics`, `/static/`) 는 캐싱 허용 (정적 자산이라 그래야 빠름).

### B. nginx `/api/` 에서 `error_page` 제거

`nginx.ocr.conf` / `nginx.llm.conf`:

```nginx
# Before
location /api/ {
    proxy_pass http://wrapper;
    proxy_http_version 1.1;
    proxy_intercept_errors on;
    error_page 502 504 = /__restarting;
}

# After
location /api/ {
    proxy_pass http://wrapper;
    proxy_http_version 1.1;
}
```

이제 wrapper 가 죽으면 nginx 의 기본 502 Bad Gateway (status 502, HTML body)
를 그대로 돌려준다. 브라우저는 502 를 캐싱하지 않고, JS 의 `fetch()` 는
**`r.ok === false`** 또는 `r.json()` reject 로 깨끗하게 에러 처리 가능.

페이지 라우트 (`/`, `/status`, `/metrics`, `/static/`, `/ocr`) 에는
`__restarting` 친절 페이지 유지 — 사용자가 페이지 열었을 때 빈 화면 대신
"잠시 후 다시 시도" 안내가 더 좋음.

### 배포

- `honestjung/ocrwrapper:0.1.8` → `0.1.9`
- `/srv/ocrserver/` 의 `nginx.conf`, `nginx.ocr.conf`, `nginx.llm.conf`,
  `docker-compose.yml` 갱신
- `docker compose exec nginx nginx -s reload`
- `docker compose up -d --no-deps wrapper`

### 검증

```
$ curl -sI /api/metrics?range=6h | grep -i cache
cache-control: no-store

$ curl -sI /metrics | grep -i cache
(none — HTML 페이지는 그대로)

$ for r in 1h 6h 24h 7d; do curl -sI "/api/metrics?range=$r" | grep -i content-type; done
Content-Type: application/json
Content-Type: application/json
Content-Type: application/json
Content-Type: application/json
```

## 교훈

- `error_page X Y = /uri` 의 `=` 는 응답 코드를 새 URI 의 코드로 덮어쓴다.
  JSON API location 에 절대 적용하지 말 것. 페이지 라우트에만.
- API 응답에 `Cache-Control: no-store` 는 기본값처럼 둬야 함. 안 두면 한 번의
  잘못된 200 응답이 브라우저 캐시에 박혀서 진단이 어려워진다.
- "특정 탭만 안 보임" → 그 탭의 default-load URL 이 캐싱된 거 의심. 다른
  탭은 hit 된 적이 없어서 깨끗한 케이스가 자주 있음.
