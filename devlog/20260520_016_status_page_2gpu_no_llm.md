# 20260520_016 — 상태 페이지 재구성: OCR 2 GPU + LLM 부재 모드 대응 (wrapper 0.1.2)

## 문제

`/status` 가 단일 GPU + LLM 상시 가용 가정으로 작성돼 있어 운영 현실과 안 맞음:

1. "Chandra OCR" 카드가 `GPU 0` 만 언급. OCR 모드는 chandra-a (GPU 0) + chandra-b
   (GPU 1) 두 백엔드를 nginx upstream 으로 묶어 쓰는데 ([[20260515_011_nginx_split_for_real_ocr_x2]])
   상태 페이지엔 보이지 않음.
2. "Qwen3-14B LLM" 카드가 모드 무관하게 항상 표시 → OCR 모드에서는 컨테이너
   자체가 안 떠있어서 영구 "오프라인". 잘못된 알람 같은 인상.
3. "빠른 테스트" 의 `/llm/health` `/llm/v1/models` 버튼이 OCR 모드에서 의미 없음
   (nginx.ocr.conf 엔 `/llm/` location 자체가 없음).
4. "Wrapper /health" 버튼은 실제로 nginx 가 `/health` 를 chandra 업스트림으로
   라우트해서 wrapper 가 아니라 chandra 를 찌르고 있었음.

## 변경 (`wrapper/status.html` 만 손댐, API 는 그대로)

`/api/services` 가 이미 다 주는 데이터로 재구성:
- `_meta.mode` — 운영 모드 (`down` / `llm` / `ocr` / `2ocr` / `llm+ocr` / `llm+2ocr`)
- `ocr_backends.{alive,total,per_backend,per_backend_concurrency,recommended_concurrency}`
- `llm.{status,http_status}` — LLM 컨테이너 health probe 결과

### 레이아웃

- **상단 모드 배지**: `OCR 2 GPU`, `LLM + OCR 2 GPU` 등으로 한눈에 표시.
  운영 메트릭(가동시간/동시성/처리중/총 Job)도 같이.
- **OCR 백엔드 카드**: chandra-a, chandra-b 를 각각 행으로 나열, 옆에 GPU 매핑
  (`chandra-a → GPU 0`, `chandra-b → GPU 1`). 활성/전체, 권장 동시성, 백엔드당
  동시성 같이 표시. `alive < total` 이면 "부분 가용" 배지.
- **LLM 카드 conditional**: `mode.includes('llm')` 이면 정상 probe 표시, 아니면
  카드 회색 처리 + "현재 LLM 컨테이너가 떠 있지 않습니다. mode-llm.sh 또는
  mode-llm+ocr.sh 로 전환하면 활성화됩니다." 안내.
- **빠른 테스트도 모드별**: LLM 활성일 때만 `/llm/health`, `/llm/v1/models`
  버튼 노출. "Wrapper /health" 버튼은 외부에서 도달 불가라 제거.

### GPU 매핑 출처

chandra-a / chandra-b → GPU 0 / 1 매핑은 `docker-compose.yml` 의 `device_ids`
약속. API 가 GPU 정보를 안 주므로 status.html 에 `GPU_FOR_BACKEND` 상수로
하드코딩. 백엔드 이름이 바뀌면 같이 갱신 필요.

## 배포

이미지: `honestjung/ocrwrapper:0.1.2` (id `ae365f7ab413`) — Docker Hub 에
`:0.1.2` + `:latest` 동시 push 완료.
`docker-compose.yml` 의 `image:` 가 `:0.1.2` 로 갱신돼 있음. OCR 작업 끝나면:

```bash
cd /srv/ocrserver && docker compose up -d wrapper
```
