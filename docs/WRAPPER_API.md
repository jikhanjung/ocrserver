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
| `recommended_concurrency` | 클라이언트가 채워둘 in-flight 페이지 권장값 = `alive × OCR_PER_BACKEND_CONCURRENCY` (기본 6) |
| `mode` | 운영 모드 라벨 (아래 표) |
| `concurrency` | wrapper 자신의 in-flight semaphore (백엔드 수와 무관, 별도 env로 설정) |

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
target_inflight = stats["recommended_concurrency"]   # 모드 따라 6 또는 12
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
    "per_backend": {
      "chandra-a": {"status": "ok", "http_status": 200},
      "chandra-b": {"status": "down", "error": "..."}
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

| `_meta` 필드 | 설명 |
|---|---|
| `mode` | 살아 있는 서비스로 추론한 운영 모드. `2ocr` (chandra ×2), `llm+ocr` (chandra + LLM), `1ocr` (chandra 하나만, LLM 없음), `llm` (LLM만), `llmx2` (LLM이 GPU 2장 점유), `down` (전부 죽음). 전환 중에는 `1ocr` 같은 중간값이 잠깐 보인다 |
| `probe_age_s` | 헬스 캐시 나이 (0~5). freshness 확인용 |
| `images` | 배포 compose의 서비스별 이미지 태그. 클라이언트가 붙어 있는 wrapper/chandra 버전을 여기서 확인 |
| `llm_model` | compose의 LLM 서비스가 띄우는 HF 모델 id. LLM 미구성 시 `null` |
| `llm_gpus` | LLM 서비스에 배정된 GPU 수 |

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

## 환경변수 (wrapper 컨테이너)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `VLLM_URL` | `http://nginx:80` | vLLM 엔드포인트 |
| `VLLM_MODEL` | `chandra` | 모델명 |
| `OCR_CONCURRENCY` | `12` | wrapper의 in-flight semaphore. vLLM에 동시 전송할 최대 페이지 수 |
| `OCR_DPI` | `150` | PDF 렌더링 해상도 |
| `OCR_MAX_PAGE_PX` | `2200` | 페이지 longest side 픽셀 상한. 초과 시 비례 축소 (vLLM `max_model_len` 보호) |
| `OCR_BACKENDS` | `chandra-a,chandra-b` | health 프로브 대상 backend 컨테이너명(쉼표 구분) |
| `OCR_BACKEND_PORT` | `8000` | 각 backend의 health 포트 |
| `OCR_PER_BACKEND_CONCURRENCY` | `6` | backend 1개당 권장 동시성. `recommended_concurrency = alive × 이 값` |
| `DB_PATH` | `/data/ocrserver.db` | SQLite 파일 경로 |
| `PDF_DIR` | `/data/pdfs` | 업로드 PDF 보관 디렉토리 |

## 제약 사항

- Job 메타데이터·페이지 결과는 SQLite(`DB_PATH`)에 영속 저장됨. wrapper 컨테이너 재시작 후에도 조회 가능
- wrapper 재시작 시 DB에 `processing`으로 남은 job은 시작 시 자동 재개된다 (`ok`인 페이지는 건너뛰고 실패/미완 페이지만 다시 렌더)
- **인증 없음** — 내부망 전용. 외부 노출 시 별도 인증 레이어 필요. `client_id`는 단순 식별자이며 검증되지 않음
- 502/503 오류 시 자동 재시도 (5s → 15s → 30s → 60s, 최대 4회)
