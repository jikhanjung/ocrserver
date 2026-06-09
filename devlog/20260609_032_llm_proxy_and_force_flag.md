# devlog 032 — LLM proxy wrapper + POST /ocr force flag (wrapper 0.2.1)

날짜: 2026-06-09
태그: `honestjung/ocrwrapper:0.2.1`

## 배경

지금까지 nginx 가 `/llm/*` 를 vLLM (`llm:8000`) 으로 직결시켰기 때문에
Qwen3-14B 로 오가는 prompt / completion 내용을 어디에도 기록하지 않았다.
vLLM 로그는 `POST /v1/chat/completions 200` 한 줄, 본문은 truncated DEBUG
로도 일부만. PaperMeister 등 client 가 무엇을 묻고 무엇을 받는지 사후
확인할 방법이 없었다.

별개로, OCR 결과 일부 페이지의 markdown 이 비어 있는 케이스가 있어 client
가 "이 파일 다시 OCR 해달라" 고 강제 요청할 수 있는 escape hatch 가
필요했다.

## 결정

### LLM proxy: 같은 이미지·다른 컨테이너

기존 `wrapper/` 코드베이스에 LLM proxy 기능을 합치되, OCR 큐에 LLM
streaming proxy 가 같은 event loop 를 공유하는 위험을 피하려고 **runtime
은 두 컨테이너로 분리**:

- `wrapper` — `WRAPPER_ROLE=ocr` (기본). 기존 OCR job API + 대시보드
  + lifespan resume + `/api/llm/*` (조회용, LLM DB RO 마운트).
- `llmwrapper` — `WRAPPER_ROLE=llm`. `/v1/*` proxy 만. 신규 컨테이너,
  compose profile `[llm]`.

같은 `honestjung/ocrwrapper:0.2.1` 이미지에서 분기. `WRAPPER_ROLE` 이
lifespan 단계에서 어떤 DB 를 어떻게 여는지 + 어떤 라우트를 등록하는지
결정한다.

### DB 분리

LLM 기록은 `data/llmserver.db` (WAL) 로 OCR DB 와 완전 분리. SQLite 는
파일 단위 writer lock 이라 같은 파일에 두 writer 가 붙으면 busy 발생.
도메인 join 도 없으니 분리가 깔끔. ocrwrapper 는 RO (`file:...?mode=ro`)
로 같은 파일을 마운트해서 `/api/llm/recent` / `/api/llm/stats` 가 읽는다.

### 라우팅: nginx 만 변경

외부 URL (`/llm/v1/chat/completions` 등) 은 그대로. nginx `/llm/` upstream
만 `llm:8000` → `llmwrapper:8000` 으로 바꾸고 SSE 가 죽지 않도록
`proxy_buffering off` + `proxy_request_buffering off`.

다만 `/llm/health` 는 별도 location 으로 분리해서 여전히 vLLM 직결.
`/status` 페이지의 LLM health probe 가 wrapper 자체 `/health` (빈 dict
리턴) 가 아니라 실제 백엔드 vLLM 의 health 를 봐야 의미가 있기 때문.

### SSE streaming 처리

`/v1/chat/completions` 의 `stream: true` 는 chunk 를 클라이언트로 그대로
통과시키면서 동시에 wrapper 측에서 누적해 한 row 로 저장. 핵심:

- `httpx.AsyncClient(timeout=None).stream("POST", ...)` 로 upstream 응답을
  열고 `aiter_lines()` 로 SSE 라인 단위 수신.
- 각 라인은 `yield (line + "\n").encode()` 로 즉시 클라이언트에 forward.
- `data: {...}` 페이로드는 별도 파싱: `delta.content` 누적, `usage` 객체
  (vLLM 이 `stream_options.include_usage=true` 일 때 마지막 직전 chunk 에
  보냄) 캡처. wrapper 가 요청 body 에 `stream_options.include_usage` 가
  없으면 자동 주입.
- generator `finally` 안에서 `asyncio.create_task(db_llm_insert(...))` 로
  log 스케줄. `await` 을 finally 에서 직접 호출하면 client_abort 시
  GeneratorExit 와 이벤트 루프 종료 path 가 race 한다.
- `GeneratorExit` / `CancelledError` 잡아서 `status=client_abort` 로 기록.

### force flag

POST `/ocr` 에 `force: bool = Form(False)`. `True` 면 `db_find_existing_
by_hash` + `db_find_done_by_filename` 둘 다 건너뛰고 새 `job_id` 로 처음부터
OCR. 기존 잡 row 는 그대로 남고, dedup `ORDER BY (status='done') DESC,
submitted_at DESC` 라 done 끼리는 최신 (즉 force 로 만든 새 잡) 이 우선
hit.

빈 페이지 정의 (markdown NULL / empty / whitespace-only) 는 client 가
판단. 서버는 force flag 만 처리.

## 변경 표면

- `wrapper/main.py`
  - 상수: `WRAPPER_ROLE`, `LLM_UPSTREAM`, `LLM_DB_PATH`, `LLM_LOG_MAX_BYTES`
  - 글로벌: `_llm_db`, `_llm_db_ro`
  - DB helpers: `db_llm_init`, `db_llm_insert`, `db_llm_recent`,
    `db_llm_stats`, `_get_llm_db_ro` (lazy RO open, ocrwrapper 가 llmwrapper
    보다 먼저 떠도 stale None 안 됨)
  - lifespan 분기: role=llm 은 LLM DB RW + db_llm_init 만, role=ocr 은
    기존 + LLM DB RO (lazy)
  - 라우트: role=ocr 에 `/api/llm/recent`, `/api/llm/stats`;
    role=llm 에 `/v1/{path:path}` (GET passthrough, POST log+forward,
    streaming 포함)
  - POST `/ocr` 에 `force: bool = Form(False)`, dedup conditional
- `wrapper/status.html` — Qwen3-14B 카드 아래 24h 통계 row + 최근 요청
  리스트. `refreshLlmTraffic()` 가 mode 에 LLM 있을 때만 polling.
- `docker-compose.yml` — wrapper 이미지 `0.1.14` → `0.2.1` +
  `WRAPPER_ROLE=ocr` + `LLM_DB_PATH`. 신규 서비스 `llmwrapper`
  (profiles: [llm], `WRAPPER_ROLE=llm`, `LLM_UPSTREAM=http://llm:8000`,
  depends_on: llm).
- `nginx.llm.conf` — `/llm/health` 만 vllm 직결 location 분리,
  `/llm/` 는 llmwrapper 로 + `proxy_buffering off` +
  `proxy_request_buffering off` + `client_max_body_size 32m`.

## 배포

1. compose / nginx 파일 sync (`/home/.../` → `/srv/ocrserver/`)
2. `docker compose --profile llm up -d llmwrapper` — 신규 컨테이너 +
   `llmserver.db` 첫 생성 (WAL 모드 자동, `-wal`/`-shm` 동시 생김)
3. `docker compose --profile llm up -d wrapper` — 이미지 bump 로 자동
   recreate. 큐가 비어 있어 in-flight 잡 0 건, lifespan resume 도 no-op.
4. `docker compose --profile llm up -d --force-recreate nginx` —
   single-file bind mount 의 inode 함정 회피 (memory 참조).

## 검증

- POST `/llm/v1/chat/completions` non-streaming → `status=ok`, prompt
  15 / completion 50 / total 65 토큰, 2813 ms 기록
- POST `/llm/v1/chat/completions` stream=true 정상 종료 → `streamed=1`,
  `status=ok`, 9/10/19 토큰 (usage chunk 캡처 확인), 614 ms
- POST stream=true 중간 끊김 (`head -20`) → `streamed=1`,
  `status=client_abort`, usage 없음 (자연스러움)
- `/api/llm/stats?range=24h` → total/ok/error/tokens/avg_latency 정상
- `/llm/health` → 200 (vllm 직결)
- POST `/ocr` 동일 파일 두 번 → 두 번째 `cached: true`, 같은 `job_id`
- POST `/ocr` `force=true` → `cached: false`, `forced: true`, 새 `job_id`

## 남은 일

- 클라이언트 측 (PaperMeister) 가 `force` 를 보낼 결정 로직 — 빈 페이지
  판정 (whitespace-only 포함) 은 클라이언트 책임으로 합의됨, 별도 PR.
- LLM 기록 retention 정책 — 현재는 무제한 누적. `/api/llm/*` 가 무거워
  지면 일단위 partition / TTL 둘 중 하나 도입 검토.
- llmwrapper 자체의 health probe — 지금 `/status` 의 LLM 카드는 vllm
  health 만 봄. llmwrapper 가 죽으면 `/llm/v1/*` 가 502 인데 카드는 정상
  표시됨. 추후 `/api/services` 에 llmwrapper probe 추가.
