# 045 — GPU 1 예산제: LLM 선점 0.90→0.60, OCR×1 + LLM 모드 전환

**날짜**: 2026-09-14
**상태**: 배포·검증 완료. 현재 모드 `llm+ocr`.

## 왜

GPU 를 쓰는 작업이 늘었다: chandra(OCR, vLLM), Qwen3-32B-AWQ(LLM, vLLM),
FigEx(fsis2026 도판 분할 실험), dolfinserver2(SAM2.1 / YOLO11-seg / re-ID 학습).
GPU 0 은 OCR 전용으로 두고 **GPU 1 은 여러 작업이 공유**하기로 했다.

걸림돌은 vLLM 이 `--gpu-memory-utilization` 만큼을 기동 시 **통째로 선점**한다는
것. 0.90 이면 GPU 1 에 5GB 만 남아 다른 무엇도 못 올라가고, 반대로 다른
프로세스가 먼저 잡고 있으면 vLLM 이 free-memory 검사에서 기동 실패한다.

## 무엇을

- `docker-compose.yml` `llm`:
  `--gpu-memory-utilization ${LLM_GPU_UTIL:-0.60}`,
  `--max-model-len ${LLM_MAX_MODEL_LEN:-16384}` (전: 0.90 / 32768).
- `/srv/ocrserver/.env` 에 `LLM_GPU_UTIL=0.60`, `LLM_MAX_MODEL_LEN=16384`.
- `mode-llm.sh`: `.env` 를 읽어 기동 로그에 값 표시, `llmwrapper` 도 같이
  `up -d`, 낡은 "Qwen3-14B" 주석 정정.
- 기본 운영 모드를 **OCR×1 + LLM** 으로. `mode-ocr.sh`(OCR×2) 는 OCR 이 밀릴 때
  쓰는 예외 경로로 남긴다.

버리지 않은 것: MIG(Turing 미지원), 필요할 때 컨테이너 바꿔 띄우기(32B 74초·
chandra 3분 콜드스타트, 상태 기계 하나 더 생김), MPS(지금은 불필요 — 기본
compute mode 의 time-slicing 으로 공존 확인, 아래).

## GPU 1 예산 (실측)

| 항목 | 메모리 |
|---|---|
| Qwen3-32B-AWQ 가중치 | 18.1 GiB |
| KV 캐시 (util 0.60) | 9.6 GiB = 39,216 tokens, 16k 요청 2.4개 동시 |
| vLLM 총 점유 | **29.9 GB** |
| **공용 여유** | **~19 GB** |

세입자 예상 요구: FigEx 수 GB · re-ID 학습 8.8GB · re-ID 앙상블 5.2GB ·
seg 학습 ~5GB · detect 학습(imgsz 1280, batch 8, 17h) 10~15GB 추정.
detect 학습날은 `LLM_GPU_UTIL=0.45` 로 내리고 `docker compose --profile llm up -d llm`.

LLM 트래픽은 거의 전부 OCR 첫 페이지 서지 메타 추출(짧음)이라 KV 축소의
체감 손실은 없다고 본다.

## 검증 (09:30~09:40 UTC)

1. `./mode-llm.sh` — chandra-b 정지, nginx.llm.conf 교체, llm 재생성, wrapper
   `--no-deps --force-recreate`. llm healthy 까지 ~70초. 기동 로그의
   `CERTIFICATE_VERIFY_FAILED` 는 KOPRI MITM 이 HF 메타 조회를 막은 것 — 재시도
   후 캐시로 로드, 무해(기존과 동일).
2. **`/llm/` resolver rewrite 경로 첫 실검증** (HANDOFF 에 미뤄 뒀던 항목):
   `POST /llm/v1/chat/completions` → nginx → llmwrapper → llm, 1.7초, 정상 응답.
   `/llm/health` 200. `/api/services` `_meta.mode = llm+ocr`, llm ok.
3. OCR: `kruskal1964.pdf` 15쪽 제출 → 15/15 done, chandra-a 단독.
4. **공존**: `/mnt/disk1/fsis2026/venv/figex` 의 torch 로 GPU 1 에 15GB 할당 +
   8192² matmul 20회 돌리는 동안 LLM 요청 → 1.2초 정상 응답. GPU 1 사용
   44.4GB 피크, 프로세스 종료 후 29.9GB 로 복귀. vLLM 은 영향 없음.

## 규칙

- **기동 순서: llm 먼저, 배치 작업은 그 뒤.** compose 에서 llm 은 상주하므로
  보통 자동으로 지켜진다. llm 을 재기동해야 하면 GPU 1 의 배치 작업을 먼저 끝낼 것
  (아니면 free-memory 검사 실패).
- 배치 작업은 `CUDA_VISIBLE_DEVICES=1` 로, 예산 ~19GB 안에서.
- 긴 학습이 도는 동안 LLM 응답은 time-slicing 으로 느려질 수 있다. 거슬리면
  그때 MPS 검토, 또는 긴 학습은 밤에.
