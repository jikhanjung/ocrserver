# Wrapper API 명세

PDF 파일을 받아 페이지별 OCR을 수행하고 결과를 반환하는 비동기 Job API.  
내부적으로 vLLM(`datalab-to/chandra-ocr-2`)에 페이지 단위로 요청을 분산하며, 클라이언트는 서버 내부 구조를 알 필요 없다.

**Base URL**: `http://<host>:8080`

현재 배포 서버는 `http://172.16.112.150:8080` (KOPRI 내부망 전용, 인증 없음).
다른 컴퓨터에서 호출하는 방법과 주의사항은 [`ENDPOINTS.md` → 다른 컴퓨터에서 접속하기](./ENDPOINTS.md#다른-컴퓨터에서-접속하기) 참조.

---

## 엔드포인트 목록

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/ocr` | PDF 제출, job_id 즉시 반환 |
| `GET` | `/ocr/{job_id}` | Job 상태 및 결과 조회 |
| `GET` | `/ocr` | 전체 Job 목록 조회 (pages 제외) |
| `GET` | `/api/stats` | Job 카운트 통계 |
| `GET` | `/api/services` | 백엔드 헬스 + OCR backend 가용성/권장 동시성 |

---

## POST /ocr

PDF 파일을 제출하고 job_id를 즉시 반환한다. 처리는 백그라운드에서 비동기로 진행된다.

### Request

```
Content-Type: multipart/form-data
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `file` | binary | ✓ | PDF 파일 (최대 500MB, nginx `client_max_body_size`) |
| `client_id` | string | – | 호출자 식별자. 미지정 시 dedup 키는 NULL. 헤더 대신 사용 가능 |
| `total_pages` | int | – | 페이지 수 힌트. 주면 첫 폴링부터 `total_pages`가 채워져 클라이언트가 큐 크기를 잡을 수 있다. 없거나 0 이하면 서버가 PDF를 파싱해 센다. 값이 틀려도 렌더 시점에 실제 값으로 덮어써진다 |
| `force` | bool | – | `true`면 **dedup을 완전히 건너뛰고** 새 job_id로 다시 OCR한다. 아래 **강제 재처리** 참조. 기본 `false` |

`client_id`는 form 필드 대신 **`X-Client-ID` HTTP 헤더**로도 전달할 수 있다. 둘 다 보낼 경우 form 필드가 우선한다.

### Response `200 OK`

신규 job이 만들어진 경우:

```json
{
  "job_id": "b41c324a-941c-41f6-bae3-efba4f9c44a4",
  "cached": false,
  "forced": false,
  "total_pages": 15
}
```

dedup에 걸려 기존 job을 돌려준 경우:

```json
{
  "job_id": "b41c324a-941c-41f6-bae3-efba4f9c44a4",
  "cached": true,
  "in_progress": false,
  "total_pages": 15
}
```

| 필드 | 있을 때 | 설명 |
|---|---|---|
| `job_id` | 항상 | Job 식별자 (UUID v4) |
| `cached` | 항상 | `true`면 기존 job을 그대로 반환(신규 OCR 미수행). 아래 **중복 제거** 참조 |
| `total_pages` | 항상 | 페이지 수 (요청 힌트 → 없으면 서버 파싱 → 기존 job이면 그 job의 값). 파싱 실패 시 0 |
| `forced` | `cached=false` | 요청의 `force` 값을 그대로 반환 |
| `in_progress` | `cached=true` | 매칭된 job이 아직 `queued`/`processing`이면 `true`. 이때는 `GET /ocr/{job_id}` 폴링을 계속해야 한다 |

### 503 — 모드 전환 중

`mode-ocr.sh`/`mode-llm.sh`로 GPU 모드를 바꾸는 동안 `POST /ocr`은 `503 {"detail": "mode switch in progress, please retry shortly"}`를 돌려준다. 수십 초 뒤 재시도하면 된다.

### 중복 제거 (dedup)

`force`가 없으면 제출 시 아래 순서로 기존 job을 찾고, 있으면 그 `job_id`를 돌려준다(GPU 시간 절약).

1. **해시 매칭** — 같은 `(file_hash, client_id)`이고 status가 `done`·`processing`·`queued` 중 하나인 job. `done`이 우선, 같은 status면 최신 것. 진행 중인 job도 매칭되므로 클라이언트가 job_id를 잃어버리고(재시작, 새로고침) 다시 올려도 같은 파일이 두 번 돌지 않는다. **`failed`는 매칭하지 않는다** — 실패한 파일은 그냥 다시 올리면 재시도된다.
2. **파일명 fallback** — 1에서 못 찾으면 `(filename, total_pages, client_id)`가 같고 `file_hash`가 NULL인 `done` job. 해시를 저장하지 않던 옛 버전의 row를 구제하기 위한 것으로, 매칭되면 그 row에 지금 해시를 채워 넣는다. 해시가 이미 있는 row는 대상이 아니므로 새로 만든 job끼리 파일명만 같다고 섞이지는 않는다.

`client_id`가 다르면 같은 PDF여도 **별개 job으로 새로 처리**된다. `client_id` 미지정(NULL)끼리도 서로 dedup된다.

### 강제 재처리 (`force=true`)

이전 결과에 빈 페이지가 섞였다든지 해서 다시 뽑고 싶을 때 쓴다. 동작:

- dedup을 전혀 타지 않고 **새 job_id**로 처음부터 OCR한다. 응답은 `cached=false, forced=true`.
- 이전 job row와 그 결과는 **지우지 않는다.** 같은 파일에 job이 여러 개 남게 되고, 이후 `force` 없는 재제출은 그중 **가장 최근 job**을 돌려준다. 옛 결과가 필요하면 job_id를 따로 보관해 둘 것.
- 재실행 결과가 이전과 같다는 보장은 없다. wrapper는 OCR 요청에 temperature/seed를 고정하지 않으므로 `force`는 "재생성"이지 "재현"이 아니다. 재현성이 필요한 쪽은 job_id 기준으로 결과를 보관하는 편이 맞다.

### 예시

```bash
# 익명 (client_id 없이)
curl -X POST http://localhost:8080/ocr \
  -F "file=@paper.pdf"

# form 필드로 client_id 지정
curl -X POST http://localhost:8080/ocr \
  -F "file=@paper.pdf" \
  -F "client_id=papermeister"

# 헤더로 client_id 지정
curl -X POST http://localhost:8080/ocr \
  -H "X-Client-ID: papermeister" \
  -F "file=@paper.pdf"

# 페이지 수 힌트 + 강제 재처리
curl -X POST http://localhost:8080/ocr \
  -F "file=@paper.pdf" \
  -F "client_id=papermeister" \
  -F "total_pages=15" \
  -F "force=true"
```

---

## GET /ocr/{job_id}

Job 상태와 전체 결과(pages 포함)를 반환한다.

### Response `200 OK`

```json
{
  "job_id": "b41c324a-941c-41f6-bae3-efba4f9c44a4",
  "filename": "paper.pdf",
  "client_id": "papermeister",
  "status": "done",
  "submitted_at": 1778650895.506,
  "total_pages": 15,
  "done_pages": 15,
  "failed_pages": 0,
  "pages": [
    {
      "page": 0,
      "markdown": "<div data-bbox=\"...\">...</div>",
      "duration_ms": 28413,
      "status": "ok"
    },
    ...
  ]
}
```

### Response `404 Not Found`

```json
{ "detail": "job not found" }
```

### status 값

| 값 | 의미 |
|---|---|
| `queued` | 접수됨, 아직 처리 시작 전 |
| `processing` | 페이지 처리 중 |
| `done` | 전 페이지 성공 완료 |
| `done_with_errors` | 완료했으나 일부 페이지 실패 |
| `failed` | PDF 파싱 실패 등 job 전체 오류 |

### pages 배열

- 인덱스는 0-based 페이지 번호
- 처리 전 페이지는 `null`
- 성공 페이지:

```json
{
  "page": 0,
  "markdown": "...",
  "duration_ms": 28413,
  "status": "ok"
}
```

- 실패 페이지:

```json
{
  "page": 3,
  "error": "HTTP 500 ...",
  "duration_ms": 1200,
  "status": "failed"
}
```

### 예시

```bash
# 상태 확인
curl http://localhost:8080/ocr/b41c324a-941c-41f6-bae3-efba4f9c44a4

# 완료 여부만 확인
curl -s http://localhost:8080/ocr/<job_id> \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])"
```

---

## GET /ocr

전체 Job 목록을 반환한다. `pages` 배열은 포함되지 않는다.

### Query parameters

| 이름 | 타입 | 설명 |
|---|---|---|
| `client_id` | string | 지정 시 해당 client의 job만 반환. 미지정 시 전체 반환(NULL 포함) |

### Response `200 OK`

```json
[
  {
    "job_id": "b41c324a-...",
    "filename": "paper.pdf",
    "client_id": "papermeister",
    "status": "done",
    "submitted_at": 1778650895.506,
    "total_pages": 15,
    "done_pages": 15,
    "failed_pages": 0
  }
]
```

### 예시

```bash
# 전체
curl http://localhost:8080/ocr

# 특정 client
curl 'http://localhost:8080/ocr?client_id=papermeister'
```

---

## GET /api/stats

Job 카운트 + OCR 백엔드 capacity. 클라이언트가 자주 폴링해도 부담 없도록 백엔드 헬스 프로브 결과를 5초간 캐시한다.

### Response `200 OK`

```json
{
  "counts": {
    "total": 143,
    "queued": 0,
    "processing": 2,
    "done": 136,
    "done_with_errors": 2,
    "failed": 3
  },
  "ocr_backends_alive": 1,
  "ocr_backends_total": 2,
  "recommended_concurrency": 6,
  "mode": "llm+ocr",
  "uptime_s": 14,
  "concurrency": 6,
  "vllm_url": "http://nginx:80"
}
```

| 필드 | 설명 |
|---|---|
| `counts` | status별 job 개수 |
| `ocr_backends_alive` | health 200 응답한 OCR 백엔드 수 |
| `ocr_backends_total` | 등록된 OCR 백엔드 수 (`OCR_BACKENDS` env) |
| `recommended_concurrency` | **호출자의** in-flight 페이지 권장값 = 사용 가능 슬롯(`min(concurrency, alive × OCR_PER_BACKEND_CONCURRENCY)`)을 활성 클라이언트 수로 나눈 몫(올림). `?client_id=` 또는 `X-Client-ID`로 자신을 밝히면 정확. 아래 **클라이언트 간 공평 분배** 참조 |
| `recommended_concurrency_new_client` | 지금 **새 클라이언트가 합류하면** 받을 값 (0.2.5+). `client_id` 없이 불렀고 아직 제출 전이면 **이 값을 쓸 것**. 자신을 밝힌 호출자에겐 위 값과 같다 |
| `client_id` | 호출자가 밝힌 id 그대로, 없으면 `null`. 두 권장값이 다르면 id를 안 준 것 |
| `mode` | 운영 모드 라벨 (아래 표) |
| `concurrency` | wrapper 전체 in-flight 페이지 상한 (`OCR_CONCURRENCY`, 백엔드 수와 무관) |
| `active_clients` | 지금 페이지가 처리 중이거나 대기 중인 `client_id` 수 |

#### `mode` 값

GPU 2장 환경 기준 실제 등장하는 값은 다음과 같다.

| 값 | 의미 | 비고 |
|---|---|---|
| `2ocr` | OCR 백엔드 2개 alive (LLM 없음) | `mode-ocr.sh` |
| `llm+ocr` | OCR 1개 + LLM | `mode-llm.sh` (기본) |
| `1ocr` | OCR 1개만 alive, LLM 없음 | OCR 모드에서 한쪽 다운 등 |
| `llm` | OCR 없음, LLM만 alive | OCR 일시 다운 |
| `down` | OCR 백엔드 없음 (LLM도 없음) | |

### 클라이언트 사용 예 (큐 깊이 자동 조정)

```python
stats = requests.get("http://localhost:8080/api/stats").json()
target_inflight = stats["recommended_concurrency"]   # 내 몫: 혼자면 12(OCR×2)/6(OCR+LLM), 둘이면 절반
# 큐에 target_inflight 미만 남으면 추가 PDF 제출
```

---

## GET /api/services

`/api/stats`보다 상세한 백엔드별 헬스. 같은 캐시(5초 TTL) 공유.

### Response `200 OK`

```json
{
  "chandra": {"status": "ok", "http_status": 200},
  "llm":     {"status": "ok", "http_status": 200},
  "ocr_backends": {
    "alive": 1,
    "total": 2,
    "per_backend_concurrency": 6,
    "recommended_concurrency": 6,
    "recommended_concurrency_new_client": 3,
    "client_id": null,
    "active_clients": 2,
    "per_backend": {
      "chandra-a": {"status": "ok", "http_status": 200},
      "chandra-b": {"status": "down", "error": "..."}
    }
  },
  "scheduler": {
    "total": 12,
    "active_clients": 2,
    "per_client_limit": 6,
    "inflight": 12,
    "waiting": 30,
    "per_client": {
      "papermeister": {"inflight": 6, "waiting": 12},
      "labnotes":     {"inflight": 6, "waiting": 18}
    }
  },
  "_meta": {
    "chandra_url": "http://nginx:80/health",
    "llm_url":     "http://nginx:80/llm/health",
    "concurrency": 6,
    "mode": "llm+ocr",
    "probe_age_s": 0.3,
    "uptime_s": 32,
    "images": {
      "nginx": "nginx:alpine",
      "wrapper": "honestjung/ocrwrapper:0.2.3",
      "llmwrapper": "honestjung/ocrwrapper:0.2.3",
      "chandra-a": "honestjung/ocrserver:0.1.1",
      "chandra-b": "honestjung/ocrserver:0.1.1",
      "llm": "vllm/vllm-openai:latest"
    },
    "llm_model": "Qwen/Qwen3-32B-AWQ",
    "llm_gpus": 1
  }
}
```

| `scheduler` 필드 | 설명 |
|---|---|
| `total` | wrapper 전체 in-flight 페이지 상한 (`OCR_CONCURRENCY`) |
| `active_clients` | 지금 페이지가 돌고 있거나 대기 중인 `client_id` 수 |
| `per_client_limit` | 지금 활성인 클라이언트 하나가 가져갈 수 있는 슬롯 수 = `ceil(total / active_clients)`. 호출자 관점의 값은 `ocr_backends.recommended_concurrency` |
| `inflight` / `waiting` | 전체 처리 중 / 슬롯 대기 중 페이지 수 |
| `per_client` | 활성 클라이언트별 처리 중·대기 페이지 수. `client_id` 없는 요청은 `"(none)"` |

| `_meta` 필드 | 설명 |
|---|---|
| `mode` | 살아 있는 서비스로 추론한 운영 모드. `2ocr` (chandra ×2), `llm+ocr` (chandra + LLM), `1ocr` (chandra 하나만, LLM 없음), `llm` (LLM만), `llmx2` (LLM이 GPU 2장 점유), `down` (전부 죽음). 전환 중에는 `1ocr` 같은 중간값이 잠깐 보인다 |
| `probe_age_s` | 헬스 캐시 나이 (0~5). freshness 확인용 |
| `images` | 배포 compose의 서비스별 이미지 태그. 클라이언트가 붙어 있는 wrapper/chandra 버전을 여기서 확인 |
| `llm_model` | compose의 LLM 서비스가 띄우는 HF 모델 id. LLM 미구성 시 `null` |
| `llm_gpus` | LLM 서비스에 배정된 GPU 수 |

---

## 클라이언트 간 공평 분배 (wrapper 0.2.4+)

wrapper는 전체 in-flight 페이지를 `OCR_CONCURRENCY`(OCR×2 모드 12, OCR+LLM 모드 6)로 제한하는데, 이 슬롯을 **활성 클라이언트 수로 나눠** 배분한다.

- 활성 클라이언트 = 지금 페이지가 처리 중이거나 슬롯을 기다리는 `client_id`. 없는 요청은 전부 하나(`None`)로 묶인다.
- 클라이언트당 상한 = `ceil(OCR_CONCURRENCY / 활성 클라이언트 수)`. 혼자면 12 전부, 둘이면 6씩, 셋이면 4씩.
- 상한은 페이지 하나를 시작할 때마다 다시 계산된다. 한쪽이 끝나면 남은 쪽은 페이지 한 장 처리 시간 안에 12로 되돌아간다.
- 0.2.3까지는 단순 FIFO 세마포어여서, A가 500쪽 PDF를 먼저 넣으면 뒤에 온 B는 A가 거의 끝날 때까지 시작하지 못했다. 이제는 B가 오는 즉시 절반을 받는다.

그래서 **여러 프로젝트가 같은 서버를 쓰려면 `client_id`를 서로 다르게** 주는 것이 중요하다. 같은 `client_id`(또는 둘 다 없음)면 한 클라이언트로 묶여 분배가 일어나지 않는다. 현재 분배 상태는 `/api/services`의 `scheduler`, 요약은 `/api/stats`의 `active_clients`·`recommended_concurrency`에서 본다.

`/api/stats`·`/api/services`는 권장값을 두 개 돌려준다:

| 상황 | 쓸 값 |
|---|---|
| `client_id` 없이 호출, 아직 제출 전 | `recommended_concurrency_new_client` |
| `?client_id=myapp`(또는 `X-Client-ID`)으로 호출 | `recommended_concurrency` (제출 전이든 진행 중이든 정확한 자기 몫) |
| `client_id` 없이 호출, 이미 진행 중 | 자신이 활성 수에 포함돼 있으므로 `recommended_concurrency` — 단 다른 익명 클라이언트와 구분이 안 되니 id를 주는 편이 낫다 |

id를 주면 두 값이 같아진다. 다르게 나온다면 id가 전달되지 않은 것이다. 응답의 `client_id` 필드로 서버가 무엇을 받았는지 확인할 수 있다.

---

## 폴링 패턴

```python
import time, requests

# 1. 제출 (client_id 선택)
res = requests.post(
    "http://localhost:8080/ocr",
    files={"file": open("paper.pdf", "rb")},
    data={"client_id": "papermeister"},   # 또는 headers={"X-Client-ID": "..."}
)
job_id = res.json()["job_id"]

# 2. 완료 대기
while True:
    job = requests.get(f"http://localhost:8080/ocr/{job_id}").json()
    print(f"{job['done_pages']}/{job['total_pages']} pages done")
    if job["status"] in ("done", "done_with_errors", "failed"):
        break
    time.sleep(10)

# 3. 결과 사용
for page in job["pages"]:
    if page and page["status"] == "ok":
        print(page["markdown"])
```

---

## 도판 분할 잡 API (wrapper 0.3.0+, P02)

OCR 과 별개의 잡 종류. 실행은 **호스트 워커**(`scripts/figures_worker.py`, Codex CLI / gpt-6-astra)가 하고
wrapper 는 접수·큐·결과 저장만 한다. 프롬프트와 결과 JSON 스키마는 **요청에 실려 온다** — 서버는 도메인을
모르고 구조만 검증한다. 쪽 번호는 모두 **0-based**. 설계: `devlog/20260916_P02_figure_split_service_design.md`,
클라이언트 계약: PaperMeister `docs/figure_pipeline_client_plan.md`.

| 메서드 | 경로 | 역할 |
|---|---|---|
| `HEAD/GET` | `/pdfs/{file_hash}` | 서버에 PDF 가 있는가 (200 / 404) |
| `POST` | `/pdfs` | multipart `file` (+`client_id`) → `{file_hash, existed, size}`. OCR 없이 보관만. 201 |
| `POST` | `/figures/workspace` | `{client_id, file_hash, ocr_digest, pages:[{page, markdown}]}` — 논문 쪽별 OCR 텍스트. PDF 가 없으면 404 `pdf_missing`. 201 |
| `HEAD/GET` | `/figures/workspace/{file_hash}/{ocr_digest}` | 작업 폴더 텍스트가 있는가 |
| `POST` | `/figures/{kind}` | `kind` ∈ `detect` \| `link` \| `panels`. 202 `{job_id, total, cached, queued}` |
| `GET` | `/figures/{kind}/{job_id}` | 잡 + 항목별 결과 |
| `GET` | `/figures/jobs?client_id=&kind=&status=` | 목록 (결과 본문 없음) |
| `POST` | `/figures/{kind}/{job_id}/resume?retry_errors=` | 실패·예산 소진 항목 재큐. `retry_errors=true` 면 시도 횟수 초기화 |
| `POST` | `/figures/worker/resume` | 치명 정지(로그인 만료·한도) 해제. 호스트에서 원인을 고친 뒤 호출 |
| `GET` | `/api/figures` | 대시보드 요약 (워커 상태·큐·24h 호출·마지막 오류) |
| `GET` | `/api/figures/items?status=&kind=&client_id=&limit=` | 항목 목록 — 진행·대기 먼저, 그다음 최근 완료 순. 항목마다 쪽수·시도·경과·토큰·결과 요약(link: 도판/항목/skipped, panels: 패널 수, detect: 도판/dismiss)·오류 |
| `GET` | `/figures` | **큐 페이지** (HTML, 30초 자동 갱신) — 워커 상태, 진행/대기/완료 수, 편당 평균과 남은 시간 추정, 항목 표 |
| `POST` | `/internal/figures/*` | 워커 전용 (claim · heartbeat · result · **release**(종료 시 즉시 재큐, 시도 환불) · worker/status · workspace). `X-Worker-Token` + nginx 에서 loopback·docker 브리지만 허용 |

### POST /figures/{kind}

```json
{ "client_id": "papermeister-…",
  "file_hash": "<sha256>",
  "ocr_digest": "<캐시 JSON 해시>",          // detect·link 필수 (작업 폴더 키). panels 는 선택
  "items": [ { "key": "f12@…", … } ],       // kind 별 필드는 아래. key 는 응답에 그대로 돌아온다
  "prompt": { "version": "detect-v1-…", "instructions": "…", "schema": { … } },
  "options": { "model": "gpt-6-astra", "effort": "high", "dpi": 216 },
  "force": false }
```

| kind | item 필수 필드 | 선행 조건 |
|---|---|---|
| `detect` | `page` (쪽 단위 항목, PaperMeister 099 §4), `hint_boxes` (bbox 목록, 0개 가능 — 쪽 의심 자리표시), `figure_keys` (선택, 상자와 같은 순서) | PDF + 작업 폴더 |
| `link` | `figures: [{figure_id, page, bbox_page_1000, …}]` | PDF + 작업 폴더 |
| `panels` | `page`, `bbox_page_1000`, `caption`, `entries` | PDF |

- **dedup**: `(kind, file_hash, ocr_digest, item 내용, prompt, options)` 해시가 같은 항목이 같은 `client_id` 로
  `done` 이면 재호출 없이 그 결과를 복사한다(`cached`). `force: true` 로 우회.
- 항목 상태: `queued` → `processing` → `done` | `failed`(시도 3회 소진) | `budget_exhausted`(세션 상한).
  잡 상태: `queued` | `processing` | `done` | `done_with_errors` | `failed`.
- 워커의 치명 오류(로그인 만료·CLI 없음·사용량 한도)는 **시도로 세지 않고** 항목을 큐로 되돌리며 워커를
  `paused` 로 둔다. `GET` 응답의 `worker.state`·`paused_reason` 으로 보인다. 해제는 `/figures/worker/resume`.
- 호출 간격 `FIGURES_MIN_INTERVAL`(기본 300 s) 은 워커가 지키고 서버는 알려만 준다.
- 결과 TTL 30일, 작업 폴더 TTL 7일 (기동 시 정리). 진실의 원천은 클라이언트 DB.

### GET /figures/{kind}/{job_id}

```json
{ "job_id": "…", "kind": "panels", "status": "processing", "total": 12, "done": 5, "failed": 0, "cached": 3,
  "items": [ { "key": "f12@…", "status": "done", "attempts": 1, "result": { … 클라이언트 스키마 그대로 … },
               "elapsed_s": 97.3, "usage": { … }, "model": "gpt-6-astra", "completed_at": 1789… },
             { "key": "f13@…", "status": "queued", "attempts": 0 } ],
  "worker": { "state": "sleeping", "alive": true, "paused_reason": null, "next_call_at": 1789…, "min_interval_s": 300 } }
```

### 워커가 모델에 주는 입력 (프롬프트 작성자용)

워커(`scripts/figures_worker.py`)는 요청의 `prompt.instructions` 뒤에 `=== INPUT (JSON) ===` 구분선과 JSON 하나를 붙여
`codex exec` 의 stdin 으로 보낸다. `prompt.schema` 는 `--output-schema` 로 강제된다. JSON 모양:

| kind | 작업 디렉터리(`-C`) | `--image` | INPUT JSON |
|---|---|---|---|
| `detect` | 논문 작업 폴더 | `items/<id>/target.png` (대상 쪽 150dpi, 힌트 상자들 빨강 + 순서 번호, 쪽 전체 상자는 안 그림, 쪽 번호 라벨) | `{kind, item: <요청 항목 그대로>, workspace: {pdf_pages, page_numbering:"0-based", text_dir, all_text, pages_dir, page_dpi}, target_image: {path, width, height, dpi, hint_box_drawn}}` |
| `link` | 논문 작업 폴더 | 없음 | `{kind, item, workspace: {…}}` |
| `panels` | 항목 폴더 (`figure.png` 만) | `figure.png` (bbox 크롭, `options.dpi` 기본 216, 긴 변 4000px 상한) | `{kind, item, image: {path, width, height, dpi, pdf_clip_xyxy_points}, image_width, image_height, original_caption, existing_subfigures}` — 뒤 넷은 fsis `astra_panels.py` 프롬프트 호환 |

작업 폴더(`/srv/ocrserver/figure_ws/{file_hash}/{ocr_digest}/`, 논문당 한 번 생성):
`README.txt` · `text/pNNN.txt`(쪽별 OCR 텍스트, 블록마다 `[Label x0 y0 x1 y1] text`, 그림 블록은 `[image: alt]`) ·
`text/all.txt`(`=== page N ===` 구분) · `pages/pNNN.png`(100dpi 전 쪽) · `items/<id>/{figure.json, target.png, run/aN/}`.
detect·link 의 지시문은 "필요하면 앞뒤 쪽을 열고 `text/all.txt` 를 grep 하라" 를 담아야 한다 — 폴더는 `--sandbox read-only`
로 열려 있고 Codex 는 이미지도 스스로 연다(2026-09-16 실측). 실행 산출물(`prompt.txt`, `schema.json`, `response.json`,
`events.jsonl`, `stderr.log`, `run.json`)은 `run/a<시도>/` 에 남는다.

### 워커 운영

- 유닛: `scripts/systemd/ocrserver-figures-worker.service` (User=jikhanjung — codex 로그인이 그 홈에 있다). 설치는 파일 머리말.
- 한 번에 한 항목, 호출 사이 `min_interval_s`(서버가 claim 응답으로 알려줌) 대기. 빈 큐면 30 s 마다 claim.
- 세션 상한: detect 600 s · link 1200 s · panels 600 s (`FIGURES_SESSION_TIMEOUT_*`; 라이브 `.env` 는 link 3600). 넘으면 프로세스 그룹 kill →
  `budget_exhausted`. 첫 실제 link(46쪽)가 968 s 였다(devlog 047).
- **stall 감시**: codex stdout 이 `FIGURES_IDLE_TIMEOUT`(기본 900 s) 동안 늘지 않으면 kill → `failed`(재시도). 스트림의
  `Reconnecting…` 알림은 실패가 아니다(턴이 완료되면 `done`).
- 치명(`login required`·`Codex CLI not found`·`usage limit`·`rate limit`) 은 호출 전 `codex login status` 와 호출 후
  stdout+stderr 에서 찾는다. stderr 의 `failed to refresh available models`·`backend-api/ps/mcp` 는 이 망의 상시 노이즈라 제외.
- 결과 검증: 워커가 `prompt.schema` 로 type/required/properties/items/enum 만 검사(호스트에 jsonschema 없음). 위반이면 `failed`.
  도메인 검증은 클라이언트 몫.
- 로그: `journalctl -u ocrserver-figures-worker -f`. 상태는 `/status` 카드와 `GET /api/figures`.

### 환경변수 (0.3.0 추가)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `FIGURES_WORKER_TOKEN` | (없음) | 비어 있으면 `/internal/figures/*` 가 503 — 워커가 붙을 수 없다 |
| `FIGURES_MIN_INTERVAL` | `300` | 워커 호출 최소 간격(초). 서버는 claim 응답으로 알려준다 |
| `FIGURES_MAX_ATTEMPTS` | `3` | 항목당 시도 |
| `FIGURES_HEARTBEAT_TIMEOUT` | `1800` (라이브 `.env`: 4200) | 이 시간 동안 heartbeat 없는 `processing` 항목은 큐로 복귀. **워커 세션 상한보다 길어야 한다** |
| `FIGURES_RESULT_TTL_DAYS` / `FIGURES_WORKSPACE_TTL_DAYS` | `30` / `7` | 기동 시 정리 |

## 환경변수 (wrapper 컨테이너)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `VLLM_URL` | `http://nginx:80` | vLLM 엔드포인트 |
| `VLLM_MODEL` | `chandra` | 모델명 |
| `OCR_CONCURRENCY` | `12` | wrapper의 in-flight semaphore. vLLM에 동시 전송할 최대 페이지 수 |
| `OCR_DPI` | `150` | PDF 렌더링 해상도 |
| `OCR_MAX_PAGE_PX` | `2200` | 페이지 longest side 픽셀 상한. 초과 시 비례 축소 (vLLM `max_model_len` 보호) |
| `OCR_RESUME_MAX_ATTEMPTS` | `3` | 기동 시 `processing` job 을 재개(resume)하는 최대 횟수. 초과하면 `failed` (`resume aborted: ...`) — 렌더 중 프로세스가 죽는 PDF 가 무한 재시작 루프를 만들지 않게 (0.2.7, devlog 044) |
| `OCR_BACKENDS` | `chandra-a,chandra-b` | health 프로브 대상 backend 컨테이너명(쉼표 구분) |
| `OCR_BACKEND_PORT` | `8000` | 각 backend의 health 포트 |
| `OCR_PER_BACKEND_CONCURRENCY` | `6` | backend 1개당 동시성. 사용 가능 슬롯 = `min(OCR_CONCURRENCY, alive × 이 값)`, 이를 활성 클라이언트 수로 나눈 것이 `recommended_concurrency` |
| `DB_PATH` | `/data/ocrserver.db` | SQLite 파일 경로 |
| `PDF_DIR` | `/data/pdfs` | 업로드 PDF 보관 디렉토리 |

## 제약 사항

- Job 메타데이터·페이지 결과는 SQLite(`DB_PATH`)에 영속 저장됨. wrapper 컨테이너 재시작 후에도 조회 가능
- wrapper 재시작 시 DB에 `processing`으로 남은 job은 시작 시 자동 재개된다 (`ok`인 페이지는 건너뛰고 실패/미완 페이지만 다시 렌더)
- **인증 없음** — 내부망 전용. 외부 노출 시 별도 인증 레이어 필요. `client_id`는 단순 식별자이며 검증되지 않음
- 502/503 오류 시 자동 재시도 (5s → 15s → 30s → 60s, 최대 4회)
