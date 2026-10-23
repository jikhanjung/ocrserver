# 053 — 모드 스크립트가 GPU 0 사용 여부를 본다 (2026-10-23)

## 문제

`mode-ocr.sh`·`mode-llm.sh` 는 "chandra-a 가 GPU 0 에서 늘 돈다"를 전제로 했다.
10-23 지금은 GPU 0 = figsplit 학습, GPU 1 = chandra-b 다. chandra-a 는 꺼져 있다.

- `mode-llm.sh` 는 chandra-b 를 끄고 OCR 을 chandra-a 로 보낸다. chandra-a 가 꺼져 있으면 **OCR 백엔드가 0** 이 된다.
- `mode-ocr.sh` 는 chandra-a 를 살피지 않고 동시 처리를 12(GPU 둘 전제)로 둔다.
- 두 스크립트는 `/status` 의 모드 토글(`POST /api/mode` → `ocrserver-mode-switch.path` → `mode_switcher.sh`)로도 실행된다. 그래서 스스로 막아야 한다.
- 스크립트가 중간에 멈추면 wrapper 의 `_mode_switching`(메모리 플래그)이 남아 `/ocr` 가 계속 503 이다. 이 플래그는 wrapper 를 재생성해야 풀린다.

## 고침

- `mode-lib.sh`(새로 만듦, 두 스크립트가 source):
  - `is_running`.
  - `gpu0_free_for_chandra_a`: chandra-a 가 안 돌 때 GPU 0 사용량이 `GPU0_BUSY_MIB`(기본 1024) 미만이면 비었다고 본다.
  - `wait_healthy`: 기본 900 s 타임아웃. 전에는 무한 대기였다.
  - `set_ocr_concurrency`, `reload_nginx`, `recreate_wrapper`.
- `mode-ocr.sh`:
  - llm 을 멈춘다.
  - chandra-a 는 이미 돌면 두고, GPU 0 이 비었으면 띄우고, 다른 작업이 쓰고 있으면 띄우지 않는다.
  - chandra-b 를 띄우고, healthy 인 백엔드 수 n 을 센다. 띄웠는데 healthy 가 아닌 chandra-a 는 다시 멈춘다(nginx `resolve` 가 계속 보내므로).
  - **동시 처리 = 6 × n**, `nginx.ocr.conf` 를 적용하고 wrapper 를 재생성한다. n = 0 이면 nginx 는 그대로 두고 wrapper 만 재생성한 뒤 실패로 끝낸다.
- `mode-llm.sh`:
  - chandra-a 가 꺼져 있고 GPU 0 이 바쁘면 **거절**한다. 아무것도 바꾸지 않고 wrapper 만 재생성해 switching 플래그를 푼 뒤 exit 1.
  - chandra-a 가 꺼져 있고 GPU 0 이 비었으면, chandra-a 를 띄워 healthy 까지 기다린 뒤에 chandra-b 를 끈다. OCR 이 끊기지 않는다.
  - 그 뒤는 예전과 같다.
- `mode_switcher.sh`(root 소유)는 바꾸지 않았다. 같은 스크립트를 부른다.

## 검증 (10-23, GPU 0 = figsplit 24.9 GB, chandra-b 만 가동)

- `bash -n` 세 파일 통과. 도우미 함수가 현재 상태를 맞게 읽는다(GPU 0 바쁨, chandra-a 꺼짐, chandra-b 가동).
- 라이브(`/srv/ocrserver`)에 복사한 뒤 실제로 실행했다:
  - `mode-llm.sh` → 거절, exit 1. chandra-b·nginx·동시 6 그대로, mode `1ocr`, `/api/mode` switching false.
  - `mode-ocr.sh` → "GPU 0 사용 중 — chandra-a 는 띄우지 않는다", OCR × 1, 동시 6, `/health` 200.
- **아직 못 본 경로**: GPU 0 이 빈 상태에서 chandra-a 를 띄우는 쪽(`mode-ocr.sh` → OCR × 2·동시 12, `mode-llm.sh` 의 a 먼저 기동).
  figsplit 학습이 끝난 뒤 처음 쓸 때 확인한다.

## 추가 (10-23 05:02 UTC) — `mode-llm.sh --allow-no-ocr`

사용자가 OCR 이 멈추는 것을 감수하고 GPU 1 을 LLM 으로 바꾸기로 했다(GPU 0 = figsplit). 거절 동작은 그대로 두고, 직접 실행할 때만 쓰는 옵션을 넣었다.

- 옵션은 chandra-a 가 꺼져 있고 GPU 0 이 바쁠 때만 거절을 건너뛴다. `/api/mode` 토글은 옵션 없이 부르므로 계속 거절한다.
- 실행 결과: chandra-b 중지, `nginx.llm.conf`, llm·llmwrapper 기동.
  - `/llm/v1/models` 200 까지 232 s 걸렸다. HDD 의 `hf_cache` 에서 Qwen3-32B-AWQ 19 GB 를 읽었다.
  - GPU 1 29.9 GB, 짧은 생성 "LLM OK", mode `llm`, chandra `/health` 502(의도대로).
  - **`honestjung/vllm-openai:0.20.2-p1`(devlog 052)과 HDD 로 옮긴 hf_cache(devlog 054)를 이것으로 확인했다.**
- 되돌리기: GPU 0 이 비면 `./mode-llm.sh`(옵션 없이)가 chandra-a 를 띄워 OCR + LLM 이 된다. 그 경로는 아직 검증하지 않았다.
