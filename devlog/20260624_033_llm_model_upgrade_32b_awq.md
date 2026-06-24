# devlog 033 — LLM 모델 업그레이드 Qwen3-14B → Qwen3-32B-AWQ + /status 동적화

날짜: 2026-06-24
태그: `honestjung/ocrwrapper:0.2.2` (wrapper), `llm` 서비스 모델 교체

## 배경

`llm` 서비스가 Qwen3-14B(fp16) 로 돌고 있었는데, "더 똑똑한 모델로 바꿀 수
있나" 라는 요구. 단, 호스트는 RTX 8000 ×2 인데 `llm` 모드에서 LLM 은 **GPU
1장(48GB, Turing sm_75)** 만 쓴다 (다른 GPU 는 chandra OCR). 즉 단일 48GB +
bf16/FP8 텐서코어 없음이 후보를 크게 제약.

용도 확인: `llmserver.db` 실제 트래픽은 거의 전부 **"OCR 첫 페이지에서 서지
메타데이터 추출"** 단일 작업 (PaperMeister). 목표는 "더 똑똑하게".

## 후보 실측 (vLLM 0.20.2 동일 이미지, GPU1 단독)

먼저 싸게: 각 모델 `config.json` 만 받아 arch 확인 → vLLM 0.20.2 가 셋 다
등록함 (`Qwen3_5MoeForConditionalGeneration`, `Qwen3_5ForConditionalGeneration`,
`Qwen3ForCausalLM`). "vLLM main 필요" 블로커는 없음.

| 후보 | 결과 |
|---|---|
| **Qwen3.5-35B-A3B-GPTQ-Int4** (MoE, GDN+멀티모달) | ✅ 기동(21GB) 하나 콜드스타트 ~11분 (비전타워 warmup + GDN/FlashInfer JIT). A/B 6건 중 1건 HTTP 500. ~15 tok/s. |
| **Qwen3.5-27B-GPTQ-Int4** (dense, GDN+멀티모달) | ❌ 로드 중 CUDA OOM. int4 로 안 덮인 비전타워+GDN 이 fp16 으로 올라가 ~47GB 초과 → 단일 48GB 불가. `--max-num-seqs 2`+expandable_segments 도 동일. TP=2 면 가능하나 두 GPU 점유=OCR 중단. |
| **Qwen3-32B-AWQ** (표준 트랜스포머, int4) | ✅ 채택. 로드 ~74초, 6/6 성공, 한국어 요약·메타추출 품질 우수. |

### Turing 호환성 메모
- FA2 는 sm_75 거부되지만 vLLM 이 graceful fallback (TORCH_SDPA / Triton-GDN /
  FLASHINFER). GDN 커널은 **Triton/FLA 폴백**이라 sm_75 에서 돈다.
- 즉 Qwen3.5 도 "아키텍처 비호환"은 아니다. dense 27B 는 순수 **메모리 용량**
  문제, MoE 35B-A3B 는 **콜드스타트/안정성** 문제.

## 속도 핵심 발견 — int4 큰 모델이 fp16 작은 모델보다 빠를 수 있다

`llmserver.db` 실제 메타추출 프롬프트 6건을 동일 하네스(단독, thinking off,
temp 0)로 재생:

| | 14B-fp16 | 32B-AWQ-int4 |
|---|---|---|
| 평균 처리량 | 18.5 tok/s | **20.2 tok/s** |

32B 가 오히려 ~9% 빠름. 디코딩은 메모리 대역폭 병목이라 토큰당 읽는 가중치가
14B-fp16(~28GB) > 32B-int4(~18GB) 이기 때문. DB 과거 14B 실측(18.5)과 통제
벤치(18.5)가 일치 → 방법론 검증됨. **결론: 14B→32B-AWQ 는 속도 손해 없는
업그레이드.** (세션 중 "32B 가 2배 느릴 것"이라 했던 추정은 fp16 가정 오류로
정정.)

## 품질 A/B (14B vs 32B vs 35B-A3B, 같은 6문서)

세 모델 모두 year·DOI·title·authors 정확 (한국어 문서 포함). 차이는 미용
수준 — 32B/35B 는 DOI 의 `http://dx.doi.org/` 접두사 정리, 35B 는 대소문자
임의 정규화. **실제 워크로드(메타추출)는 14B 로도 이미 충분**했고, 32B 의
실이득은 어려운 케이스/요약/챗 헤드룸 + 속도 동급.

## 배포

### (1) 모델 교체
- dev tree `docker-compose.yml` 의 `llm` command 모델 인자만
  `Qwen/Qwen3-14B` → `Qwen/Qwen3-32B-AWQ` (나머지 동일: `--dtype float16
  --gpu-memory-utilization 0.90 --max-model-len 32768 --enforce-eager
  --trust-remote-code`). **`--served-model-name qwen` 유지 → 클라이언트
  무변경.**
- `cp` → `/srv/ocrserver/` → `docker compose --project-directory
  /srv/ocrserver --profile llm up -d --force-recreate llm`.
- 검증: `/llm/health` 200(46초), 운영 경로(nginx→llmwrapper→vllm) 로
  `model:"qwen"` 샘플 추론 정상, llmwrapper 가 DB 기록 정상.

### (2) /status 페이지 동적화 (wrapper 0.2.1 → 0.2.2)
- `status.html` 에 `Qwen3-14B` 가 하드코딩돼 있어 stale 표시됨. 매번 손대지
  않도록 동적화:
  - `main.py` 의 compose 파서를 `_refresh_compose_cache()` 로 묶고, `llm`
    서비스 command 의 **첫 non-flag 토큰**을 모델명으로 추출 →
    `/api/services._meta.llm_model` 로 노출.
  - `status.html` 의 LLM 카드 제목을 placeholder(`#llmModelShort`,
    `#llmModelFull`)로 바꾸고 JS 가 `_meta.llm_model` 로 채움
    (`Qwen3-32B-AWQ LLM — Qwen/Qwen3-32B-AWQ`).
  - 앞으로 모델 교체 시 **compose 만 바꾸면 페이지 자동 반영**.
- wrapper + llmwrapper 둘 다 같은 이미지라 함께 `0.2.2` 로 recreate
  (드리프트 방지). LLM 유휴 시점이라 무손실.

## 미해결

- `nvidia-smi topo -m` 이 GPU0↔GPU1 을 `NODE`(PCIe) 로 보고, NVLink 링크
  inactive + capabilities 빈 응답. 물리 브리지 있다는데 드라이버가 NVLink 를
  못 잡는 중 → TP 성능에 직결. 브리지 재안착 / dmesg / persistence mode 점검
  필요 (별도 세션).
- hf_cache 에 테스트로 받은 `Qwen3.5-35B-A3B-GPTQ-Int4`(~18GB) 등 잔여 →
  디스크 정리 시 후보.

## 참고
- 메모리: `reference_llm_model_fit_rtx8000.md` (속도/적합성 발견 요약)
- HANDOFF 2026-06-24 갱신
