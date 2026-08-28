# OCR Server — 서비스 엔드포인트 가이드

모든 서비스는 nginx 리버스 프록시를 통해 단일 포트(`8080`)로 노출된다.

**Base URL**: `http://<host>:8080`

---

## 다른 컴퓨터에서 접속하기

이 문서의 예시는 모두 `http://localhost:8080`으로 적혀 있다. 서버가 아닌 다른
컴퓨터에서 호출할 때는 `localhost`를 서버 주소로 바꾸면 되고, 나머지는 동일하다.

| 항목 | 값 |
|---|---|
| 서버 주소 | `http://172.16.112.150:8080` (호스트명 `jikhanserver`) |
| 접근 범위 | KOPRI 내부망에서만 접근 가능. 외부 인터넷·VPN 없이 외부에서는 접속 불가 |
| 인증 | 없음. 내부망 신뢰 기반이므로 API 키/로그인 절차가 없다 |
| 프로토콜 | HTTP (TLS 없음) |
| 업로드 상한 | PDF 1개당 **500MB** (nginx `client_max_body_size`) |
| 식별자 | `client_id`(form) 또는 `X-Client-ID` 헤더 — 호출 주체별로 고정값을 쓰길 권장 (dedup 단위) |

```bash
# 연결 확인 — 200 + JSON이 오면 도달한 것
curl -s http://172.16.112.150:8080/api/stats

# 예시 스크립트를 그대로 쓰려면 BASE 하나만 바꾸면 된다
BASE=http://172.16.112.150:8080
curl -s -X POST $BASE/ocr -F "file=@doc.pdf" -F "client_id=myapp"
```

주의할 점:

- 서버는 실험 장비(RTX 8000 ×2)라서 재부팅·모드 전환(`OCR 2 GPU` ↔ `OCR + LLM`)이
  가끔 있다. 연결이 거부되면 잠시 후 재시도하고, 상태는 `/api/services`나 브라우저로
  `http://172.16.112.150:8080/status`를 열어 확인한다.
- `/llm/*` 엔드포인트는 `OCR + LLM` 모드일 때만 살아 있다 (`/api/stats`의 `mode` 참조).
- IP는 고정 할당이다. 바뀌면 이 표와 `WRAPPER_API.md` 상단 주소를 함께 갱신할 것.

---

## 서비스 구성도

```
클라이언트
    │
    ▼ :8080
┌─────────┐
│  nginx  │  경로 기반 라우팅
└────┬────┘
     │
     ├─ /ocr, /api/, /          → wrapper (FastAPI, Job 관리)
     ├─ /llm/                   → llm (vLLM, Qwen3-14B)
     └─ /health, /v1/*          → chandra-a (vLLM, Chandra OCR)
```

---

## 1. OCR — PDF 처리 (Wrapper API)

PDF를 통째로 보내면 비동기로 처리해 결과를 반환한다.  
자세한 명세는 [`WRAPPER_API.md`](./WRAPPER_API.md) 참조.

### 주요 엔드포인트

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/ocr` | PDF 제출, job_id 즉시 반환 |
| `GET` | `/ocr/{job_id}` | Job 상태 및 전체 결과 조회 |
| `GET` | `/ocr` | 전체 Job 목록 조회 (`?client_id=...` 필터) |
| `GET` | `/api/stats` | Job 카운트 + OCR backend capacity (`mode`, `recommended_concurrency`) |
| `GET` | `/api/services` | 백엔드별 헬스 + capacity 상세 |
| `GET` | `/` | 웹 대시보드 |

### 빠른 사용법

```bash
# PDF 제출 (client_id는 form 또는 X-Client-ID 헤더로 전달 가능, 둘 다 선택사항)
JOB=$(curl -s -X POST http://localhost:8080/ocr \
  -F "file=@paper.pdf" \
  -F "client_id=papermeister" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "job_id: $JOB"

# 완료 대기 (폴링)
while true; do
  STATUS=$(curl -s http://localhost:8080/ocr/$JOB \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'], d['done_pages'], '/', d['total_pages'])")
  echo "$STATUS"
  [[ "$STATUS" == done* || "$STATUS" == failed* ]] && break
  sleep 10
done

# 결과 텍스트 추출
curl -s http://localhost:8080/ocr/$JOB \
  | python3 -c "
import sys, json
pages = json.load(sys.stdin)['pages']
for p in pages:
    if p and p['status'] == 'ok':
        print(f'--- p.{p[\"page\"]+1} ---')
        print(p['markdown'])
"
```

### Python 클라이언트 예시

```python
import time, requests

BASE = "http://localhost:8080"

def ocr_pdf(path: str) -> list[dict]:
    with open(path, "rb") as f:
        job_id = requests.post(f"{BASE}/ocr", files={"file": f}).json()["job_id"]

    while True:
        job = requests.get(f"{BASE}/ocr/{job_id}").json()
        print(f"[{job['status']}] {job['done_pages']}/{job['total_pages']}")
        if job["status"] in ("done", "done_with_errors", "failed"):
            return job["pages"]
        time.sleep(10)

pages = ocr_pdf("paper.pdf")
text = "\n".join(p["markdown"] for p in pages if p and p["status"] == "ok")
```

### 큐 깊이 자동 조정 (mode-aware)

`/api/stats`의 `mode`/`recommended_concurrency`를 보면 서버가 OCR 전용(`2ocr`, 권장 12)인지 LLM 공유(`llm+ocr`, 권장 6)인지 알 수 있다. 클라이언트가 in-flight 페이지 수를 이 값에 맞추면 모드 전환 시 별도 설정 변경 없이 자동으로 throughput 추종.

```python
stats = requests.get("http://localhost:8080/api/stats").json()
# {"mode": "2ocr", "recommended_concurrency": 12, "ocr_backends_alive": 2, ...}
target_inflight = stats["recommended_concurrency"]
# 큐에 target_inflight 미만 남으면 추가 PDF 제출
```

자세한 mode 라벨 종류와 각 필드 의미는 [`WRAPPER_API.md`](./WRAPPER_API.md#get-apistats) 참조.

---

## 2. LLM — Qwen3-14B 범용 언어 모델

OpenAI 호환 API. 경로 `/llm/` 이하가 vLLM(`llm:8000/`)으로 라우팅된다.

- **모델명**: `qwen`
- **컨텍스트**: 최대 32,768 토큰
- **GPU**: GPU 1 (LLM 모드 시)

### 주요 엔드포인트

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/llm/health` | 헬스체크 |
| `GET` | `/llm/v1/models` | 사용 가능한 모델 목록 |
| `POST` | `/llm/v1/chat/completions` | 채팅 완성 |
| `POST` | `/llm/v1/completions` | 텍스트 완성 |

### 채팅 완성 (chat/completions)

```bash
curl -X POST http://localhost:8080/llm/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen",
    "messages": [
      {"role": "system", "content": "당신은 논문을 분석하는 전문가입니다."},
      {"role": "user", "content": "다음 텍스트를 요약해 주세요: ..."}
    ],
    "max_tokens": 512,
    "temperature": 0.7
  }'
```

응답:
```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "model": "qwen",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "..."},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 50, "completion_tokens": 120, "total_tokens": 170}
}
```

### Thinking 모드 비활성화

Qwen3는 기본적으로 Chain-of-Thought(`<think>` 태그)를 출력한다.  
빠른 응답이 필요하면 비활성화:

```bash
curl -X POST http://localhost:8080/llm/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen",
    "messages": [{"role": "user", "content": "안녕하세요"}],
    "max_tokens": 200,
    "chat_template_kwargs": {"enable_thinking": false}
  }'
```

### 스트리밍

```bash
curl -X POST http://localhost:8080/llm/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen",
    "messages": [{"role": "user", "content": "파이썬으로 Hello World 작성해줘"}],
    "max_tokens": 300,
    "stream": true
  }'
```

### Python 클라이언트 예시

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/llm/v1",
    api_key="none",  # 인증 불필요
)

resp = client.chat.completions.create(
    model="qwen",
    messages=[{"role": "user", "content": "논문 초록을 한국어로 번역해줘: ..."}],
    max_tokens=1024,
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)
print(resp.choices[0].message.content)
```

### OCR 결과와 결합 예시

```python
import time, requests
from openai import OpenAI

BASE = "http://localhost:8080"
llm = OpenAI(base_url=f"{BASE}/llm/v1", api_key="none")

# 1. OCR
with open("paper.pdf", "rb") as f:
    job_id = requests.post(f"{BASE}/ocr", files={"file": f}).json()["job_id"]
while True:
    job = requests.get(f"{BASE}/ocr/{job_id}").json()
    if job["status"] in ("done", "done_with_errors", "failed"):
        break
    time.sleep(10)

ocr_text = "\n\n".join(
    p["markdown"] for p in job["pages"] if p and p["status"] == "ok"
)

# 2. LLM 분석
resp = llm.chat.completions.create(
    model="qwen",
    messages=[
        {"role": "system", "content": "논문 OCR 결과를 분석해 핵심 내용을 정리하세요."},
        {"role": "user", "content": ocr_text[:8000]},
    ],
    max_tokens=1024,
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)
print(resp.choices[0].message.content)
```

---

## 3. Chandra OCR — vLLM 직접 접근 (고급)

Wrapper를 거치지 않고 vLLM API에 직접 접근하는 방법.  
일반적으로는 Wrapper API를 사용하는 것을 권장한다.

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/health` | Chandra 헬스체크 |
| `GET` | `/v1/models` | 모델 목록 |
| `POST` | `/v1/chat/completions` | 이미지→Markdown OCR |

```bash
# 이미지 OCR (base64 인코딩 필요)
IMG_B64=$(base64 -w0 page.jpg)
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"chandra\",
    \"messages\": [{
      \"role\": \"user\",
      \"content\": [{
        \"type\": \"image_url\",
        \"image_url\": {\"url\": \"data:image/jpeg;base64,${IMG_B64}\"}
      }]
    }],
    \"max_tokens\": 4096
  }"
```

---

## 4. GPU 모드 전환

GPU 1을 OCR 또는 LLM 용도로 전환한다.

### LLM 모드 (기본)
GPU 0: OCR, GPU 1: Qwen3-14B

```bash
sudo /srv/ocrserver/mode-llm.sh
```

### OCR 모드
GPU 0 + GPU 1: OCR × 2 (처리량 2배)

```bash
sudo /srv/ocrserver/mode-ocr.sh
```

### 현재 모드 확인

```bash
docker ps --format "{{.Names}}: {{.Status}}" \
  | grep -E "llm|chandra"
```

---

## 5. 모니터링

### 웹 대시보드

`http://localhost:8080/` 접속 — Job 목록, 처리 상태, 페이지별 OCR 결과 확인.

### 서비스 상태 요약

```bash
echo "=== chandra-a ===" && curl -s http://localhost:8080/health
echo "=== llm ===" && curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/llm/health
echo "=== wrapper ===" && curl -s http://localhost:8080/api/stats
echo "=== containers ===" && docker ps --format "{{.Names}}: {{.Status}}"
```
