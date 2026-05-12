# OCR 서버 작동 메커니즘

## 전체 구조

```
클라이언트 (batch_ocr.py / PaperMeister)
        │
        ▼
   nginx :8080  (least_conn 로드밸런서)
    │         │
    ▼         ▼
chandra-a  chandra-b
(GPU 0)    (GPU 1)
  vLLM       vLLM
  :8000      :8000
```

nginx가 없는 단일 GPU 구성에서는 클라이언트가 `:8000`에 직접 접속.

---

## 컴포넌트별 역할

### vLLM 서버 (`entrypoint.sh` → `vllm.entrypoints.openai.api_server`)

컨테이너 기동 시 자동으로 뜨는 **OpenAI API 호환 HTTP 서버**. 주요 엔드포인트:

| 엔드포인트 | 설명 |
|---|---|
| `GET /health` | 서버 준비 여부 확인 |
| `GET /v1/models` | 로드된 모델 목록 |
| `POST /v1/chat/completions` | OCR 요청 처리 (GPT-4V와 동일한 형식) |

모델은 `datalab-to/chandra-ocr-2` — 문서 OCR에 특화된 vision-language 모델. 이미지 입력을 받아 마크다운 텍스트로 출력한다.

### `batch_ocr.py`

서버가 아니라 **배치 처리 클라이언트**. `pdfs/` 디렉토리의 PDF를 스캔해서:
1. 페이지를 JPEG로 렌더링 (PyMuPDF)
2. base64 인코딩 후 `/v1/chat/completions`에 POST
3. 결과를 `ocr_json/{sha256}.json`으로 저장

### nginx

두 컨테이너 앞에서 `least_conn` 방식으로 요청 분배. 한쪽 GPU가 바쁘면 반대쪽으로 자동 라우팅.

---

## vLLM Continuous Batching

### 요청이 다른 시간에 도착해도 묶이는 원리

LLM의 토큰 생성은 **전체 네트워크를 1회 통과(forward pass) = 토큰 1개 생성** 단위로 이루어진다.

vLLM 스케줄러는 매 step마다 큐에 있는 모든 요청을 묶어서 하나의 forward pass로 처리한다:

```
step 1: [req1_tok1, req2_tok1, req3_tok1, req4_tok1, req5_tok1, req6_tok1] → GPU
step 2: [req1_tok2, req2_tok2, ..., req6_tok2, req7_tok1(새로 도착)] → GPU
step 3: [req1_tok3, req2_tok3, ..., req7_tok2] → GPU
...
```

요청이 다른 시간에 도착해도 "현재 큐에 있는 것들"을 묶어서 다음 step에 넣기 때문에, 클라이언트가 동시에 보낼 필요가 없다.

### GPU 효율

GPU는 행렬 연산을 배치가 클수록 효율적으로 처리한다. 요청 1개만 처리하면 연산 코어 대부분이 놀지만, 6개를 묶으면 더 많은 코어를 동시에 사용한다.

- concurrency 1: ~60 tokens/s
- concurrency 6: ~200+ tokens/s (GPU 연산 포화 지점)

---

## KV 캐시

각 요청은 지금까지 생성한 토큰들의 Key-Value를 VRAM에 유지한다 (KV 캐시). 동시 요청이 많을수록 VRAM 사용량이 늘어난다.

RTX 8000 (48GB) 기준:
```
VRAM 48GB × 0.90 = 43.2GB 할당
모델 가중치: 8.61GB
KV 캐시 풀: ~34GB
요청당 KV: ~140MB (0.4%)
최대 동시 처리 가능: ~240개
```

현재 구성에서 KV 캐시는 병목이 아니다. **병목은 GPU 연산(TFLOPS)** 이며, concurrency 6~8이 이 GPU의 연산 포화 지점이다.

---

## Concurrency 튜닝

`batch_ocr.py --concurrency N`은 클라이언트가 동시에 유지하는 in-flight 요청 수. `ThreadPoolExecutor`로 sliding window 방식으로 동작한다:

```
worker 1: page1 완료 → page7 → page13 → ...
worker 2: page2 완료 → page8 → page14 → ...
...
worker 6: page6 완료 → page12 → page15 → ...
```

하나가 완료되면 즉시 다음 페이지를 채워 항상 N개의 요청이 in-flight 상태를 유지한다.

### RTX 8000 벤치마크 (kruskal1964.pdf, 15페이지)

| concurrency | 총 시간 | throughput | 비고 |
|---|---|---|---|
| 1 | 312.8초 | 0.05 pages/s | 순차 처리 |
| 4 | 103.8초 | 0.14 pages/s | |
| 6 | 78.2초 | 0.19 pages/s | **스위트스폿** |
| 8 | 79.0초 | 0.19 pages/s | page 10에서 55초 튐 |

- 페이지당 평균 응답: **27.3초** (concurrency 6)
- 실효 throughput: **5.2초/페이지** (병렬 효과 포함)

---

## 2-GPU 구성 예상 성능

nginx + 컨테이너 2개로 구성 시:

```
batch_ocr.py --concurrency 12  →  nginx  →  chandra-a (6개) + chandra-b (6개)
```

- 이론 throughput: ~2.6초/페이지
- A40 serverless 단일 카드와 동등하거나 우위
- 비용: RunPod 대비 거의 0

---

## dtype 자동 선택 (`entrypoint.sh`)

GPU Compute Capability(CC)를 읽어 dtype을 자동 결정한다:

| CC | 아키텍처 | dtype |
|---|---|---|
| < 8.0 (예: RTX 8000 CC 7.5) | Turing | float16 |
| ≥ 8.0 (예: A40 CC 8.6) | Ampere+ | bfloat16 |

동일 이미지를 RunPod(A40)과 로컬(RTX 8000) 양쪽에서 그대로 사용할 수 있다.
