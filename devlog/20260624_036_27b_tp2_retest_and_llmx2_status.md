# devlog 036 — 27B dense TP=2 재실측(NVLink 복구 후) + /status LLMx2 모드 추가

날짜: 2026-06-24
태그: 벤치(배포 변경 없음) + wrapper 0.2.2 → 0.2.3 (status 모드 추가)

## 배경

035 에서 NVLink 가 복구(`topo -m` = NV2)되면서, 034 에서 "PCIe 폴백이라
느려서 보류" 했던 **Qwen3.5-27B-GPTQ-Int4 (dense) 를 TP=2 로** 다시 돌려볼
조건이 생김. NVLink all-reduce 면 TP=2 통신 페널티가 줄어드니 재실측.

## 실측 방법

- 일회성 `docker run` (compose 미변경). OCR 큐 빈 것 확인 후 chandra-a + llm
  내려 두 GPU 확보 → `vllm/vllm-openai:latest` 로 `Qwen/Qwen3.5-27B-GPTQ-Int4
  --tensor-parallel-size 2 --dtype float16 --gpu-memory-utilization 0.90
  --max-model-len 32768 --trust-remote-code --enforce-eager`, 포트 18000.
- 하네스: `llmserver.db` 의 실제 메타추출 프롬프트 6건(prompt ~5.5–6k tok)
  비스트리밍 replay, `temperature=0`, `max_tokens`=원래 completion 길이.
  (033/HANDOFF 의 32B-AWQ 측정과 동일 성격 하네스)

## 결과

| 구성 | aggregate tok/s | GPU | 콜드스타트 | 성공 |
|---|---|---|---|---|
| **27B dense TP=2 (NVLink)** | **12.85** | 2장 (OCR 중단) | ~13분 | 6/6 |
| Qwen3-32B-AWQ (현 배포, 단일) | 20.2 | 1장 (OCR 유지) | ~74초 | 6/6 |
| Qwen3-14B fp16 (구) | 18.5 | 1장 | — | — |

per-req 9.9–13.4 tok/s, 평균 12.1. 생성 중 GPU util 0% 관측.

## 판정 — 배포 안 함 (034 보류 유지)

- **OOM 은 해결됨**: TP=2 로 각 GPU 25.68 GiB 로드 → 단일 48GB OOM 이던
  034 증상 사라짐. NVLink 복구의 효과는 "로드가 됨" 수준에서 확인.
- **그러나 속도는 현 32B-AWQ 보다 ~37% 느림** (12.85 vs 20.2 tok/s). 원인
  추정: TP=2 동기화 오버헤드 + 이 모델이 **GDN/mamba 하이브리드 + 멀티모달**
  (로그에 `mamba page size`, `Encoder cache ... image items`) 이라
  enforce-eager 에서 무거움. 콜드스타트 ~13분도 033 의 MoE 35B(~11분)와 동급.
- **운영 비용**: GPU 2장 점유 → OCR 전면 중단. 단일 GPU 로 더 빠른 32B-AWQ
  가 모든 면에서 우위.
- 결론: NVLink 가 살아도 27B dense 는 채택 안 함. 034 의 판단이 실측으로
  재확인됨. (NVLink 의 실익은 여전히 "나중에 TP=2 가 분명히 이득인 더 큰
  표준 트랜스포머 모델" 때.)

## 부수 작업 — /status 에 LLMx2(2 GPU LLM) 모드 추가

이번처럼 LLM 이 두 GPU(TP=2)를 점유하는 형상을 `/status` 가 구분 못 했음
(프로브는 llm health 하나만 봐서 1 GPU/2 GPU 구분 불가). compose 파싱으로
LLM GPU 수를 읽어 모드에 반영.

- `wrapper/main.py`:
  - `_parse_llm_gpus(llm_svc, toks)` 신설 — llm 서비스 command 의
    `--tensor-parallel-size`(`-tp`, `=N` 형 포함) 우선, 없으면 deploy 의
    `device_ids` 개수, 기본 1. `_refresh_compose_cache` 가 `llm_gpus` 캐시.
  - `_mode_from_probes(cache, llm_gpus)`: n==0 & llm_ok 일 때 `llm_gpus>=2`
    면 **`llmx2`**, 아니면 기존 `llm`. 나머지 모드 불변.
  - `/api/services._meta.llm_gpus` 노출, `/api/stats` 도 같은 시그니처.
- `wrapper/status.html`: `modeLabel` 에 `llmx2 → "LLM×2 (2 GPU)"` 추가.
  `mode.includes('llm')` 기반 LLM 카드/퀵테스트는 'llmx2' 도 자동 매칭.
- 검증: 실 compose(32B-AWQ, device_ids `['1']`) → `llm_gpus=1` → 모드
  `llm`/`llm+ocr` 그대로. 합성 TP=2(플래그 3형 + device_ids×2) → 전부 2 →
  `llmx2`. 배포 후 `/api/services._meta.llm_gpus=1`, 운영 모드 `llm+ocr` 정상.

## 배포

- wrapper `0.2.2` → `0.2.3` (호스트 빌드, llmwrapper 와 공유 이미지 → 둘 다
  `--force-recreate`). chandra/llm 무변경. OCR 큐 빈 상태에서 recreate.
- 주의: 현재 compose 엔 LLMx2 형상이 없음(32B-AWQ 단일 GPU). `llmx2` 칩은
  **향후 TP=2 LLM 을 compose 로 띄울 때** 자동 표시되도록 준비만 된 상태.

## 참고
- 선행: 035(NVLink 복구), 034(NVLink 진단 + 27B 1차 OOM), 033(32B-AWQ 채택)
