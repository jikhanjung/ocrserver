# Chandra2-vLLM 새 repo 마이그레이션 계획

PaperMeister의 `deploy/chandra2-vllm-pod/`를 **독립 repo**로 분리하면서, **단일 Docker 이미지가 RunPod(A40, Ampere)와 로컬(RTX 8000, Turing) 양쪽에서 동작**하도록 리팩토링하는 작업 계획서.

새 repo 디렉토리에서 Claude를 다시 띄울 때 이 문서를 먼저 읽어 컨텍스트를 복원하세요.

---

## 0. 컨텍스트 (왜 이 작업을 하는가)

- 기존: `deploy/chandra2-vllm-pod/`는 RunPod A40 전용으로 만들어져 있음. `ENTRYPOINT`에 vLLM 옵션이 hardcode (dtype 미지정 = bfloat16 기본 가정)
- 변경 동기: 사용자가 로컬에 **Quadro RTX 8000 × 2 (48GB × 2, Turing CC 7.5)** 서버를 확보. RunPod serverless 비용 대비 압도적으로 저렴해서 대량 OCR을 로컬에서 돌리고 싶음
- 단일 이미지로 묶을 수 있는 근거:
  - 두 환경 모두 단일 카드 48GB로 충분 → tensor parallel 불필요
  - 차이는 **dtype(BF16 vs FP16)** 하나로 수렴
  - FlashAttention 2 등 일부 최적화는 vLLM이 Turing에서 자동 fallback
- PaperMeister 본체는 별도 repo로 유지. 이 repo의 vLLM 엔드포인트를 호출하는 클라이언트.

### 결정된 설계 포인트 (논의 결과)

| 항목 | 결정 | 근거 |
|------|------|------|
| 이미지 분리 vs 통합 | **단일 이미지** | dtype 외 차이 없음, entrypoint에서 CC 자동 감지 |
| dtype 결정 | **런타임에 CC 감지 → FP16/BF16 자동** | `nvidia-smi --query-gpu=compute_cap`로 첫 카드 CC 읽고 8.0 미만이면 FP16 |
| 모델 weight | **이미지에 baked 유지** (현 방식) | RunPod 콜드 스타트 짧게 유지 우선. 로컬도 한 번만 받으면 됨 |
| 2-GPU 운용 | **컨테이너 2개에 1장씩 핀** | tensor parallel 안 씀, 페이지 단위 병렬이 자연스러움 |
| Override | **모든 vLLM 옵션 env var로 노출** | `VLLM_DTYPE`, `VLLM_MAX_MODEL_LEN`, `VLLM_GPU_UTIL`, `VLLM_EXTRA_ARGS` |

---

## 1. 새 repo 목표 구조

```
chandra-vllm/                      # 새 repo 루트 (이름은 사용자 결정 — 잠정)
├── README.md                      # 통합 사용법 (RunPod + 로컬 양쪽 흐름)
├── Dockerfile                     # 환경변수 driven + entrypoint.sh 사용
├── entrypoint.sh                  # CC 감지 → dtype 결정 → vLLM 기동
├── batch_ocr.py                   # PaperMeister에서 그대로 이동 (수정 없음 예정)
├── docker-compose.local.yml       # 로컬 2-GPU 운용 표준화
├── .dockerignore                  # 이미지에 들어가면 안 되는 것 (docs/, .git, *.md 등)
├── .gitignore
├── docs/
│   ├── INSTALL_LOCAL.md           # PaperMeister에서 이동
│   └── RUNPOD.md                  # RunPod Pod 세팅 가이드 (README에서 분리)
└── (선택) .github/workflows/build.yml   # GHCR 자동 빌드/푸시
```

> repo 이름, 위치, GitHub remote 생성 여부는 사용자에게 받아야 함. 잠정 이름 `chandra-vllm` 사용.

---

## 2. 마이그레이션 작업 (순서대로)

### 2.1 사용자가 먼저 해야 할 것 (Claude 외부)

- [ ] 새 디렉토리 생성: `mkdir ~/projects/chandra-vllm && cd ~/projects/chandra-vllm`
- [ ] 기존 PaperMeister에서 4개 파일 복사:
  - `deploy/chandra2-vllm-pod/Dockerfile`
  - `deploy/chandra2-vllm-pod/batch_ocr.py`
  - `deploy/chandra2-vllm-pod/README.md`
  - `deploy/chandra2-vllm-pod/INSTALL_LOCAL.md`
  - `deploy/chandra2-vllm-pod/NEW_REPO_PLAN.md` (이 문서)
- [ ] `git init`
- [ ] 새 위치에서 `claude` 실행, 이 문서(`NEW_REPO_PLAN.md`)를 가장 먼저 읽도록 지시

### 2.2 Claude가 해야 할 것 (새 세션에서)

다음을 순서대로 진행. **각 단계 후 사용자 확인**을 받는 것이 안전.

#### Step 1. 파일 재배치
- [ ] `INSTALL_LOCAL.md` → `docs/INSTALL_LOCAL.md`
- [ ] `README.md`에서 RunPod 관련 섹션을 추출 → `docs/RUNPOD.md` 분리
- [ ] `NEW_REPO_PLAN.md`는 그대로 루트 유지 (역사 기록 + 다음 세션 참조용)

#### Step 2. `entrypoint.sh` 신규 작성

```bash
#!/bin/bash
# Runtime dispatcher: GPU CC 감지 → dtype 자동 선택
set -e

CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d '.')

if [ -z "$VLLM_DTYPE" ]; then
  if [ -z "$CC" ]; then
    echo "[entrypoint] WARN: GPU CC 감지 실패 → dtype=auto"
    VLLM_DTYPE=auto
  elif [ "$CC" -lt 80 ]; then
    VLLM_DTYPE=float16
  else
    VLLM_DTYPE=bfloat16
  fi
fi

echo "[entrypoint] GPU CC=${CC:-unknown} → VLLM_DTYPE=$VLLM_DTYPE"

exec python3 -m vllm.entrypoints.openai.api_server \
  --model "${VLLM_MODEL:-datalab-to/chandra-ocr-2}" \
  --served-model-name "${VLLM_SERVED_NAME:-chandra}" \
  --host 0.0.0.0 --port 8000 \
  --gpu-memory-utilization "${VLLM_GPU_UTIL:-0.90}" \
  --max-model-len "${VLLM_MAX_MODEL_LEN:-12384}" \
  --dtype "$VLLM_DTYPE" \
  --trust-remote-code \
  ${VLLM_EXTRA_ARGS}
```

- [ ] `chmod +x entrypoint.sh`

#### Step 3. `Dockerfile` 리팩토링

기존:
```dockerfile
ENTRYPOINT ["python3", "-m", "vllm.entrypoints.openai.api_server", ...hardcoded...]
```

변경:
```dockerfile
FROM vllm/vllm-openai:latest

RUN python3 -c "from huggingface_hub import snapshot_download; snapshot_download('datalab-to/chandra-ocr-2')"
RUN pip3 install --no-cache-dir PyMuPDF Pillow

COPY entrypoint.sh /workspace/entrypoint.sh
COPY batch_ocr.py /workspace/batch_ocr.py
RUN chmod +x /workspace/entrypoint.sh && mkdir -p /workspace/pdfs /workspace/ocr_json

# 기본값 (override 가능)
ENV VLLM_MODEL=datalab-to/chandra-ocr-2 \
    VLLM_SERVED_NAME=chandra \
    VLLM_MAX_MODEL_LEN=12384 \
    VLLM_GPU_UTIL=0.90

EXPOSE 8000
ENTRYPOINT ["/workspace/entrypoint.sh"]
```

- [ ] 환경 변수 default를 ENV에 박아 `docker inspect`로 가시화
- [ ] `VLLM_DTYPE`은 기본 미설정 (entrypoint가 CC로 결정)

#### Step 4. `docker-compose.local.yml`

로컬 2-GPU 표준 운용 패턴을 코드로 박음. RunPod에서는 사용 안 함.

```yaml
services:
  chandra-a:
    image: chandra-vllm:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ['0']
              capabilities: [gpu]
    ports: ["8000:8000"]
    volumes:
      - ${HOME}/.papermeister/ocr_json:/workspace/ocr_json
    restart: unless-stopped

  chandra-b:
    image: chandra-vllm:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ['1']
              capabilities: [gpu]
    ports: ["8001:8000"]
    volumes:
      - ${HOME}/.papermeister/ocr_json:/workspace/ocr_json
    restart: unless-stopped
```

- [ ] PaperMeister OCR 출력 경로를 그대로 마운트 → 결과 즉시 PaperMeister 캐시에 들어감
- [ ] `restart: unless-stopped`로 장기 배치 안정성 확보

#### Step 5. `README.md` 재작성

내용 골격:
1. What is this (한 문단)
2. Quick start — 로컬
3. Quick start — RunPod
4. 환경변수 레퍼런스 표 (`VLLM_DTYPE`, `VLLM_MAX_MODEL_LEN`, `VLLM_GPU_UTIL`, `VLLM_EXTRA_ARGS`, `VLLM_MODEL`, `VLLM_SERVED_NAME`)
5. Batch OCR 사용법 (`batch_ocr.py`)
6. PaperMeister 연동 노트 (endpoint URL 1개 또는 2개)
7. docs/ 안내 (INSTALL_LOCAL.md, RUNPOD.md)

- [ ] 자동 dtype 감지 동작을 환경변수 표에서 명시 (`VLLM_DTYPE` 미설정 시 CC 8.0 기준 자동)

#### Step 6. `docs/RUNPOD.md`

기존 `README.md`의 RunPod 섹션을 그대로 옮기고:
- [ ] reserved pod vs serverless 차이 보강
- [ ] 비용 추정 (`A40 ~$0.39/hr`)
- [ ] **반드시 배치 후 Pod 종료** 경고 (현 README에 있음)

#### Step 7. `docs/INSTALL_LOCAL.md`

PaperMeister에서 이동한 그대로 사용. 단 다음만 수정:
- [ ] 4번 섹션 "Chandra2-vLLM 이미지 빌드 + 첫 실행"에서 `--dtype float16` 추가 작업이 **이제 entrypoint.sh가 자동 처리하므로 불필요**하다는 점 반영
- [ ] 빌드 명령: `cd ~/projects/PaperMeister/deploy/chandra2-vllm-pod` → `cd ~/projects/chandra-vllm` (또는 결정된 이름)
- [ ] `docker-compose -f docker-compose.local.yml up -d` 워크플로우 추가

#### Step 8. `.dockerignore` / `.gitignore`

`.dockerignore`:
```
.git
.github
docs/
*.md
docker-compose.local.yml
NEW_REPO_PLAN.md
```

`.gitignore`:
```
*.pyc
__pycache__/
.venv/
ocr_json/
pdfs/
*.swp
```

#### Step 9. (선택) CI

GHCR로 이미지 자동 빌드/푸시. **로컬에서 빌드해서 쓰기 시작한 뒤로 미뤄도 됨**. 빌드가 무거워서(10-20GB 모델 다운로드 포함) free runner에서는 타임아웃 가능성 — self-hosted runner 또는 multi-stage 분리 검토 필요.

---

## 3. 검증 절차 (build 후)

### 로컬 (RTX 8000)

- [ ] `docker build -t chandra-vllm:latest .` 성공
- [ ] `docker run --rm --gpus '"device=0"' -p 8000:8000 chandra-vllm:latest` 기동 시 로그에 `[entrypoint] GPU CC=75 → VLLM_DTYPE=float16` 표시
- [ ] `curl http://localhost:8000/health` → 200
- [ ] `curl http://localhost:8000/v1/models` → `chandra` 모델 노출
- [ ] 1페이지 짜리 PDF로 batch_ocr.py 동작 확인
- [ ] `docker-compose -f docker-compose.local.yml up -d`로 2-GPU 동시 기동, `nvidia-smi`로 두 카드 모두 점유 확인

### RunPod (A40)

- [ ] 같은 이미지(GHCR 또는 RunPod 빌드)로 Pod 기동 시 `[entrypoint] GPU CC=86 → VLLM_DTYPE=bfloat16`
- [ ] health/모델/OCR 동작 동일

### 실패 시 빠른 진단

| 증상 | 의심 |
|------|------|
| `Cannot allocate memory` / OOM | `VLLM_GPU_UTIL` 0.85로 낮추기, `VLLM_MAX_MODEL_LEN` 축소 |
| BF16 강제 시 Turing 에러 | env var override가 실수로 들어갔는지 — `VLLM_DTYPE` unset 확인 |
| CC 감지 0 또는 빈 값 | `nvidia-smi` 자체가 컨테이너에서 안 보임 → toolkit 설치/`--gpus` 옵션 점검 |
| HuggingFace download fail (build) | 빌드 호스트 인터넷, HF rate limit, 모델 ID 확인 |

---

## 4. 사용자에게 받아야 할 결정 사항

새 세션 첫 turn에 다음을 확정:

1. **repo 이름** — `chandra-vllm` / `chandra-ocr-pod` / 기타
2. **위치** — `~/projects/<name>` 기본 가정
3. **GitHub remote** — 같이 만들지 (gh CLI로) / 일단 로컬만
4. **PaperMeister 측 `deploy/chandra2-vllm-pod/`** — 이주 완료 후 삭제 / 당분간 유지
5. **CI (GHCR)** — 이번에 같이 / 나중에

---

## 5. PaperMeister 측 후속 작업 (이 repo 작업 후)

이 repo가 동작하기 시작하면 PaperMeister 본체에서:

- [ ] `papermeister/ocr.py`가 RunPod serverless를 호출하던 부분을 vLLM OpenAI 호환 endpoint로 일반화
- [ ] preferences에 `vllm_endpoints: list[str]` 추가 (로컬에서 `["http://localhost:8000", "http://localhost:8001"]`)
- [ ] idle worker round-robin / health check를 vLLM `/health`로 변경
- [ ] `~/.papermeister/preferences.json`의 RunPod 키는 fallback으로 유지 가능
- [ ] HANDOFF.md에 endpoint 전환 작업 항목 추가

이 항목들은 **새 repo가 안정화된 후 PaperMeister repo에서 별도 세션**으로 진행.

---

## 6. 참고: 현재 알려진 제약과 비제약

### 작동 보장된 것 (이미 검증된 가정)
- Chandra2 = `datalab-to/chandra-ocr-2`, vLLM `vllm/vllm-openai:latest` 베이스
- 단일 48GB 카드로 max-model-len 12384, gpu-mem-util 0.90 동작 (RunPod A40 운영 중)
- batch_ocr.py는 OpenAI 호환 `/v1/chat/completions` 호출만 사용 → 환경 의존성 없음

### 가정이지만 검증 필요한 것
- vLLM이 RTX 8000(Turing)에서 `--dtype float16`으로 안정 동작 — 공식 supported로 알려져 있으나 chandra-ocr-2 모델 자체의 fp16 정확도 확인 필요
- 모델 weight가 fp16에서도 OCR 품질 손실 없음 (보통 vision 모델은 fp16에서 문제 없으나 첫 페이지 출력 spot-check 권장)

### 비제약
- Quadro RTX 8000 PCIe gen3 슬롯이라도 inference엔 충분 (PCIe 대역폭은 prefill 전송에만 영향)

---

## 7. 첫 세션 진입 시 Claude에게 줄 프롬프트 (예시)

> 이 디렉토리는 PaperMeister의 `deploy/chandra2-vllm-pod/`에서 분리한 신규 repo입니다. `NEW_REPO_PLAN.md`를 먼저 읽고, Section 2.2의 Step 1부터 순서대로 진행해주세요. 각 Step 완료 후 한 번씩 확인 받으세요. 먼저 Section 4의 "사용자에게 받아야 할 결정 사항" 5개부터 질문해주세요.
