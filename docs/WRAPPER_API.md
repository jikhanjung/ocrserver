# Wrapper API 명세

PDF 파일을 받아 페이지별 OCR을 수행하고 결과를 반환하는 비동기 Job API.  
내부적으로 vLLM(`datalab-to/chandra-ocr-2`)에 페이지 단위로 요청을 분산하며, 클라이언트는 서버 내부 구조를 알 필요 없다.

**Base URL**: `http://<host>:8080`

---

## 엔드포인트 목록

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/ocr` | PDF 제출, job_id 즉시 반환 |
| `GET` | `/ocr/{job_id}` | Job 상태 및 결과 조회 |
| `GET` | `/ocr` | 전체 Job 목록 조회 (pages 제외) |

---

## POST /ocr

PDF 파일을 제출하고 job_id를 즉시 반환한다. 처리는 백그라운드에서 비동기로 진행된다.

### Request

```
Content-Type: multipart/form-data
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `file` | binary | ✓ | PDF 파일 (최대 200MB) |

### Response `200 OK`

```json
{
  "job_id": "b41c324a-941c-41f6-bae3-efba4f9c44a4"
}
```

### 예시

```bash
curl -X POST http://localhost:8080/ocr \
  -F "file=@paper.pdf"
```

---

## GET /ocr/{job_id}

Job 상태와 전체 결과(pages 포함)를 반환한다.

### Response `200 OK`

```json
{
  "job_id": "b41c324a-941c-41f6-bae3-efba4f9c44a4",
  "filename": "paper.pdf",
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

### Response `200 OK`

```json
[
  {
    "job_id": "b41c324a-...",
    "filename": "paper.pdf",
    "status": "done",
    "submitted_at": 1778650895.506,
    "total_pages": 15,
    "done_pages": 15,
    "failed_pages": 0
  }
]
```

---

## 폴링 패턴

```python
import time, requests

# 1. 제출
res = requests.post("http://localhost:8080/ocr", files={"file": open("paper.pdf", "rb")})
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
| `OCR_CONCURRENCY` | `12` | vLLM에 동시 전송할 최대 페이지 수 |
| `OCR_DPI` | `150` | PDF 렌더링 해상도 |

## 제약 사항

- Job 결과는 **메모리에만 저장**됨 — wrapper 컨테이너 재시작 시 초기화
- **인증 없음** — 내부망 전용. 외부 노출 시 별도 인증 레이어 필요
- 502/503 오류 시 자동 재시도 (5s → 15s → 30s → 60s, 최대 4회)
