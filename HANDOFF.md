# HANDOFF — 2026-07-23 (NVIDIA 드라이버 스큐 인시던트 → 리부팅 복구)

이 파일은 작업 인수인계용. 작업 단위로 갱신.

## 방금 한 작업 (2026-07-23 — 드라이버 버전 불일치 복구 + 재발 방지)

호스트 리부팅(~09:24) 직후 `unattended-upgrades` 가 NVIDIA 드라이버를
`595.71.05 → 595.84` 로 자동 업그레이드 → 로드된 커널 모듈(구)과 userspace(신)
**Driver/library version mismatch** → GPU 컨테이너(chandra-a, llm) 전면 다운.
`unattended_upgrades_docker` gotcha 의 드라이버 변종. 세부
`devlog/20260723_037_nvidia_driver_mismatch_unattended_upgrade.md`.

- **복구**: `sudo reboot` 로 로드 모듈을 595.84 로 정렬 → compose restart 정책
  으로 전 컨테이너 복귀. **검증 완료** (리부팅 후 uptime 몇 분 시점):
  - `nvidia-smi` 595.84, NVML 595.84 일치. 두 GPU 정상, 두 `VLLM::EngineCore`
    프로세스(chandra-a GPU0, llm GPU1) 기동.
  - 컨테이너 전부 Up. cold start: llm ~1.5분, chandra-a ~6분 후 둘 다 healthy.
  - 운영 경로(nginx:8080) `/health` 200, `/llm/health` 200,
    `/api/services._meta` mode=`llm+ocr`, llm_model=`Qwen/Qwen3-32B-AWQ`.
- **재발 방지 (적용 완료)**: `/etc/apt/apt.conf.d/52-nvidia-blacklist` 신설 —
  `unattended-upgrades` 가 `nvidia-` / `libnvidia` / `linux-firmware-nvidia`
  자동 업그레이드 못 하도록 blacklist. `apt-config dump` 로 병합 확인.
  단 blacklist 는 **자동** 업그레이드만 막음 — 수동 `apt upgrade` 는 여전히
  드라이버를 올리므로 드라이버 갱신은 계획된 리부팅과 함께만.
  주의: 패턴 `nvidia-` 가 `nvidia-container-toolkit` 도 함께 고정.
- **devlog 037 untracked** — 아직 commit 안 됨 (사용자 요청 시 커밋).

## 이전 작업 (2026-06-24 오후 — NVLink/27B/LLMx2)

NVLink 복구(브리지 재안착) 확인 → 034 에서 보류했던 Qwen3.5-27B-dense 를
TP=2 로 재실측 → 그래도 미채택 → 부산물로 /status 에 LLMx2 모드 추가.
세부 `devlog/20260624_035_*` (NVLink 복구), `036_*` (27B 재실측 + LLMx2).

- **NVLink 복구**: 전원 off → 브리지 재장착 → on. `topo -m` NODE→**NV2**,
  4서브링크 25.781 GB/s, `nvlink -e`=0, dmesg sublink Error 소멸. 근본 원인
  브리지 접촉 불량 확정. 정상값 기준: `topo -m`=NV2, `nvlink -e`=0.
- **27B dense TP=2 재실측**: OOM 은 풀림(각 GPU 25.68 GiB). 하지만 aggregate
  **12.85 tok/s** (현 32B-AWQ 20.2 보다 ~37% 느림) + GPU 2장 점유(OCR 중단)
  + 콜드스타트 ~13분. GDN/mamba 하이브리드+멀티모달이라 무거움. **미채택**,
  034 판단 실측 재확인. → 32B-AWQ 단일 GPU 가 여전히 최적.
- **/status LLMx2 모드** (wrapper **0.2.2 → 0.2.3**): LLM 이 두 GPU(TP=2)를
  점유하는 형상을 표시. compose llm 의 `--tensor-parallel-size`/`device_ids`
  개수로 GPU 수 파싱(`_parse_llm_gpus`) → `_mode_from_probes` 가 n==0 &
  llm_ok & gpus>=2 면 `llmx2`. status.html 칩 "LLM×2 (2 GPU)".
  `_meta.llm_gpus` 노출. 현 compose 는 단일 GPU 라 칩 표시는 향후 TP=2 LLM
  배포 시 자동. wrapper+llmwrapper 둘 다 0.2.3 recreate.

## 이전 작업 (2026-06-24 오전 — LLM 모델 업그레이드)

`llm` 서비스 모델을 **Qwen3-14B(fp16) → Qwen3-32B-AWQ(int4)** 로 교체. OCR
은 안 건드림 (듀얼 분할 그대로). 이미지/wrapper 변경 없음 — compose 의 `llm`
command 모델 인자만 변경.

- **선정 근거 (실측 A/B)**: RTX 8000(Turing sm_75) 단일 GPU 에서 3개 후보를
  vLLM 0.20.2 같은 이미지로 실측:
  - **Qwen3.5-35B-A3B-GPTQ-Int4 (MoE)**: ✅ 기동되나 콜드스타트 ~11분
    (멀티모달 비전타워 warmup + GDN/FlashInfer JIT), A/B 에서 6건 중 1건
    HTTP 500, 처리량 ~15 tok/s. 탈락.
  - **Qwen3.5-27B-GPTQ-Int4 (dense)**: ❌ 로드 중 CUDA OOM. int4 로 안 덮인
    부분(비전타워+GDN)이 fp16 으로 올라가 ~47GB 초과, 단일 48GB 에 안 들어감.
    `--max-num-seqs 2` + expandable_segments 도 동일 OOM. TP=2 면 들어가지만
    두 GPU 점유 → OCR 전면 중단이라 부적합.
  - **Qwen3-32B-AWQ (표준 트랜스포머)**: ✅ 채택. 로드 ~74초, 6/6 성공,
    한국어 요약/메타추출 품질 우수.
- **속도 핵심 발견**: `llmserver.db` 실제 메타추출 프롬프트 6건으로 동일
  하네스 측정 → **14B-fp16 18.5 tok/s vs 32B-AWQ 20.2 tok/s**. 32B 가 오히려
  ~9% 빠름. 디코딩은 메모리 대역폭 병목이라 int4(토큰당 ~18GB 읽기) 가
  fp16 14B(~28GB) 보다 적게 읽어서. 즉 **속도 손해 없는 업그레이드**.
  (DB 과거 14B 실측 18.5 tok/s 와 통제 벤치 18.5 일치 → 방법론 검증됨.)
- **품질**: 14B/32B/35B 모두 메타추출은 정확(한국어 문서 포함). 32B 는 DOI
  접두사 정리 등 미세 개선. 실제 워크로드(서지 메타추출) 는 14B 로도 이미
  충분했고, 32B 의 진짜 이점은 어려운 케이스/요약/챗 헤드룸.
- **배포 절차**: dev tree `docker-compose.yml` 의 llm command 모델 인자만
  수정 → `/srv/ocrserver/` 로 cp → `docker compose --project-directory
  /srv/ocrserver --profile llm up -d --force-recreate llm`. served-model-name
  은 `qwen` 유지 → 클라이언트(PaperMeister 등) 코드 무변경.
- **검증**: `/llm/health` 200(46초), 운영 경로(nginx→llmwrapper→vllm) 로
  `model:"qwen"` 샘플 추론 정상, llmwrapper 가 DB 기록 정상(qwen/ok/2111ms).
- **/status 페이지 동적화 (wrapper 0.2.1 → 0.2.2)**: status.html 에
  `Qwen3-14B` 가 하드코딩돼 있어 stale. 매번 안 고치도록 동적화 — main.py
  의 compose 파서(`_refresh_compose_cache`)가 `llm` 서비스 command 의 첫
  non-flag 토큰을 모델명으로 뽑아 `/api/services._meta.llm_model` 로 노출,
  status.html 이 그걸로 `#llmModelShort/#llmModelFull` 채움. 앞으로 모델
  교체 시 compose 만 바꾸면 페이지가 자동 반영. wrapper + llmwrapper 둘 다
  0.2.2 로 recreate (같은 이미지 공유, 드리프트 방지).
- **모델 캐시**: `Qwen3-32B-AWQ` 는 `/srv/ocrserver/hf_cache` 에 받아둠.
  테스트로 받은 `Qwen3.5-35B-A3B-GPTQ-Int4`(~18GB) / 일부 27B 메타데이터도
  캐시에 남아있음 — 디스크 정리 시 후보.
- **✅ 해결 — NVLink 복구** (devlog 034 진단 → 035 복구): 전원 off →
  NVLink 브리지 재장착 → 전원 on (034 의 1순위 권고). `topo -m` 이
  NODE(PCIe) → **NV2**(bonded 2×NVLink) 로 복구, 4서브링크 모두
  25.781 GB/s, `nvlink -e` 에러 카운터 0, dmesg sublink Error 소멸. 근본
  원인 = 브리지 접촉 불량 확정. 이제 **TP=2 큰 모델 재시도 가능** (034 에서
  PCIe 폴백 때문에 보류했던 27B dense / MoE 등). 정상값 기준: `topo -m`=NV2,
  `nvlink -e`=0.

## 이전 작업 (2026-06-09)

LLM (`/llm/*`) 트래픽을 기록할 수 있게 wrapper 코드베이스에 LLM proxy
모드를 합치고, OCR 결과에 빈 페이지 있을 때 client 가 강제로 다시 OCR
시킬 수 있게 `POST /ocr` 에 `force` 옵션 추가. 세부
`devlog/20260609_032_llm_proxy_and_force_flag.md`.

- **구조**: 같은 이미지 (`honestjung/ocrwrapper:0.2.1`) 를 두 컨테이너로
  띄움. `wrapper` (`WRAPPER_ROLE=ocr`, 기본) 는 기존 OCR + 대시보드 +
  `/api/llm/*` (조회용, LLM DB RO). `llmwrapper` (`WRAPPER_ROLE=llm`,
  compose profile `[llm]`) 는 `/v1/*` proxy + 기록 전담. event loop /
  DB 분리로 OCR 큐가 LLM streaming proxy 의 어떤 stall 에도 영향받지 않게.
- **DB**: `data/llmserver.db` (WAL) 신규. llmwrapper RW, wrapper RO.
  `llm_requests` 테이블 — submitted/completed/model/endpoint/client_ip/
  request_json/response_text/prompt_tokens/completion_tokens/total_tokens/
  latency_ms/http_status/status/error/streamed. 본문은 65KB 로 truncate.
- **라우팅**: 외부 URL 그대로 (`/llm/v1/chat/completions`). nginx `/llm/`
  upstream 만 `llm:8000` → `llmwrapper:8000`. SSE 위해 `proxy_buffering
  off` + `proxy_request_buffering off`. `/llm/health` 만 별도 location
  으로 vllm 직결 (`/status` 가 실제 백엔드 health 보려면 필요).
- **SSE streaming**: chunk 통과 + 누적 패턴. `aiter_lines()` 로 라인 단위
  forward, `data: {...}` 의 `delta.content` 누적, vllm 마지막-1 chunk 의
  `usage` 객체 캡처. 종료 시 `asyncio.create_task(db_llm_insert(...))` —
  finally 에서 직접 await 하면 client_abort 시 GeneratorExit race.
  `client_abort` 도 status 로 기록.
- **force flag**: `POST /ocr` 의 `force: bool = Form(False)`. True 면
  `db_find_existing_by_hash` + `db_find_done_by_filename` 둘 다 skip,
  새 `job_id` 로 전체 OCR. 기존 row 보존, 다음 dedup 에선 새 잡이
  최신이라 우선. 응답에 `forced: true` 포함. 빈 페이지 판정 (whitespace-
  only 포함) 은 client 책임.
- **검증**:
  - non-streaming chat: status=ok, 15/50/65 토큰, 2813ms
  - streaming 정상: status=ok, 9/10/19 토큰, usage chunk 캡처
  - streaming 중단: status=client_abort
  - `/api/llm/stats?range=24h` 통계 정상, `/status` 의 LLM 카드 24h row +
    최근 요청 리스트 노출
  - POST /ocr 같은 파일 → cached:true / force=true → 새 job_id

- **배포**: wrapper image `0.1.14` → `0.2.1`. 큐 비어 있는 상태에서
  recreate, in-flight 잡 영향 0. nginx 는 single-file bind mount inode
  함정 회피 위해 `--force-recreate`.

## 이전 작업 (2026-05-28 밤)

큰 PDF (Stewart 1773p) 제출 시 대시보드가 4분 이상 먹통이던 문제 해결.
세부 `devlog/20260528_031_per_page_render_in_worker.md`.

- **원인**: `_run()` 이 PDF 전체 페이지를 upfront 로 PyMuPDF render +
  base64 list 에 쌓아두고 OCR 시작. 1773p = 단일 thread to_thread 에서 ~9분
  GIL convoy → 이벤트 루프 starve.
- **수정**: `_render_pdf` 제거, `_pdf_page_count` (페이지 수만) +
  `_render_one_page` (단일 페이지) 로 분리. `_ocr_page` 가 자기 페이지를
  `_sem` 슬롯 안에서 직접 render → chandra POST. render 동시성이 OCR
  세마포어 (12) 와 자연 일치.
- **왜 GIL convoy 안 일어나나**: chandra 25s/page, render 0.3s/page → 어느
  순간이든 12 슬롯 중 0-1개만 render 중, 나머지는 chandra await. 동시 render
  스레드 ≈ 1.
- **부가 fix**: `_render_sem` 삭제 (불필요), POST 핸들러의 sha256 +
  fitz.open + 디스크 write 도 `asyncio.to_thread` 화 (POST 동안에도 대시
  보드 응답).
- **배포**: `0.1.13` → `0.1.14`. Stewart 진행 중이었는데 lifespan resume
  으로 45/1773 부터 이어 처리. 다운타임 중 chandra 응답 4개 잡혀서 49
  로 올라옴.

## 이전 작업 (2026-05-28 저녁)

`db_find_done_by_hash` 가 `status='done'` 만 매칭해서, 같은 파일을 처리
중인 동안 재제출하면 **중복 잡** 이 만들어지던 문제. 오늘 Stewart 가
processing 중일 때 PaperMeister 가 재제출 → 두 번째 Stewart 잡이 생겨서
서버 hang 트리거된 사건의 latent 원인.

- 함수 rename: `db_find_done_by_hash` → `db_find_existing_by_hash`
- SQL: `status IN ('done','processing','queued')`, ORDER `(status='done') DESC, submitted_at DESC` (done 우선)
- 응답에 `in_progress: bool` 필드 추가 — 클라이언트가 "그냥 polling 하자" 결정 가능
- failed 잡은 여전히 dedup 안 됨 (재시도 가능 유지)
- client_id scoping 그대로 (다른 client 끼리는 dedup 안 일어남)
- 배포: `0.1.12` → `0.1.13`. 신규 잡 0건 상태에서 무손실 재기동.

## 이전 작업 (2026-05-28 오후)

PaperMeister client 가 큐 깊이 과소계상 (큰 책을 1페이지로 카운트) 으로
12개 큰 PDF 를 동시 제출 → 서버 wrapper recreate 시 lifespan resume 가
12개 모두 동시 render → GIL convoy → wrapper unresponsive 인시던트.
세부 `devlog/20260528_029_total_pages_hint_and_render_sem.md`.

- **변경 (a)**: POST `/ocr` 에 `total_pages: int | None = Form(None)` hint
  파라미터 추가. 주어지면 sync 파싱 스킵, `_jobs`/`db` 에 초기값으로 박음.
  `_run()` 이 `_render_pdf` 결과로 어차피 덮어쓰니까 거짓 hint 자동 보정.
  응답에도 `total_pages` 포함 (클라이언트 첫 poll 불필요).
- **변경 (b, 긴급 추가)**: `_render_sem = asyncio.Semaphore(2)` 도입.
  `_run()` 의 `await asyncio.to_thread(_render_pdf, ...)` 를 감쌈. PyMuPDF
  를 N 스레드 동시 호출 시 GIL convoy 로 이벤트 루프 starve 됨 — 12잡 동시
  resume 이 직접 hang 일으킴. semaphore=2 로 막아도 12잡 동시 resume 자체는
  trigger 가능, 후속 작업 필요 (per-page render).
- **데이터 복구**: Stewart Antarctica (1343/1773 done) 만 'processing'
  유지, 나머지 11잡 (모두 0/N) DB 에서 'failed' 마킹. host python 은
  root-owned DB write 불가 → 1회용 컨테이너로 SQL UPDATE.
- **배포**: `0.1.10` → `0.1.11` (hint) → `0.1.12` (render_sem 긴급 추가).
- **PaperMeister 쪽 fix**: 별도 (다른 리포). `wrapper_submit()` 이 로컬에서
  PyMuPDF 로 페이지 수 미리 계산해서 큐 깊이 계산에 사용 + 서버에 hint
  로 전송. 12잡 동시 제출 재발 방지.

## 이전 작업 (2026-05-28 오전)

PaperMeister 가 60s 타임아웃으로 POST /ocr 이 5건 연속 실패한 인시던트
진단 + 수정. 세부 `devlog/20260528_028_render_in_thread_event_loop_freeze.md`.

- **원인**: `wrapper/main.py:_run()` 이 `async def` 인데 안에서 PDF 전체
  페이지를 동기 루프로 래스터화 (`fitz.get_pixmap` + `tobytes("jpeg")` +
  `base64.encode`). 220p + 518p 큰 잡 두 개가 같은 시간 접수되면서 200+초
  동안 이벤트 루프 freeze → 그 윈도우에 들어온 POST 들이 응답 못 받음,
  nginx 는 client (PaperMeister 60s timeout) 끊긴 후 **499** 로 기록.
- **수정**: 헬퍼 `_render_pdf()` 로 sync 부분 분리, `asyncio.to_thread()`
  로 호출. PyMuPDF C 코드는 GIL 풀어주므로 진짜 병렬 render 가능.
- **부수 발견**: 어제 (devlog 027) 의 nginx 변경 (`/api/` 의 `error_page`
  제거) 이 **실제 nginx 컨테이너에는 적용 안 돼 있었음**. Docker
  single-file bind mount 는 컨테이너 시작 시점의 호스트 inode 를 잡고,
  `cp` 로 호스트 파일 교체 시 새 inode 가 되면 컨테이너는 옛 파일을 계속
  참조. `nginx -s reload` 로도 안 됨. `docker compose up -d --no-deps
  --force-recreate nginx` 로 컨테이너 재기동해야 새 inode 잡힘.
  → `mode-*.sh` 가 reload 로 끝나고 있는 것도 같은 버그에 취약. 별도 fix
  필요 (아래 곧 해야 할 작업 #8).
- **배포**: wrapper `0.1.9` → `0.1.10`, nginx 컨테이너 재기동, lifespan
  resume 으로 in-flight 2 잡 (canadiannaturali 498p + Valent 9p) 이어
  처리.
- **검증**: 배포 후 throughput 30 ppm, /api/stats 5 trials 35-104ms 안정.

## 이전 작업 (2026-05-27)

`/metrics` 페이지의 **6h 탭만** 그래프가 안 나오는 버그 진단 + 수정.
세부 `devlog/20260527_027_api_no_store_and_error_passthrough.md`.

- **증상**: Edge/Chrome 둘 다 6h 탭에서 `SyntaxError: Unexpected token '<'`.
  fetch 가 `<!doctype html>...` 를 200 으로 받음. 1h/24h/7d 는 정상.
- **원인**: ① `nginx.ocr.conf` `/api/` 의 `error_page 502 504 = /__restarting;`
  가 wrapper 잠깐 죽었을 때 HTML 200 응답으로 덮어쓰고, ② 그 응답에
  `Cache-Control` 이 없어서 브라우저가 heuristic 으로 캐싱. 6h 가 metrics
  페이지 기본 active 탭 (`metrics.html:48`) 이라 wrapper 재시작 윈도우에서
  가장 자주 hit → 6h 만 stale HTML 이 캐시 박힘. 다른 탭은 클릭해야 fetch
  되니 그 윈도우를 피해감.
- **수정**:
  - `wrapper/main.py` 에 middleware 추가 — `/api/*` 응답에
    `Cache-Control: no-store` 박음. 페이지 (`/`, `/metrics`, `/static/`) 는
    그대로.
  - `nginx.ocr.conf` / `nginx.llm.conf` `/api/` location 에서
    `proxy_intercept_errors on;` + `error_page` 줄 제거. 이제 wrapper 다운
    중에는 nginx 기본 502 가 그대로 나오고 `fetch().json()` 이 깨끗하게 reject.
    페이지 라우트는 친절 페이지 유지.
- **배포**: 이미지 `honestjung/ocrwrapper:0.1.8` → `0.1.9`. nginx config +
  compose 파일 `/srv/ocrserver/` 에 sync, nginx reload, wrapper recreate.
- **사용자 즉시 우회 (사고 직후)**: Ctrl+F5 / DevTools "Disable cache" 로
  stale 응답 강제 무효화. 사용자 측에서 확인 완료.

## 이전 작업 (2026-05-26)

오늘 세션은 사용자가 화요일 출근해서 "지난주 금요일 OCR 작업 돌려놓고
갔는데 중간에 서버 문제 생긴 것 같다" 고 보고한 인시던트 진단 +
재발 대비 watchdog 활성화. 세부 `devlog/20260526_026_friday_freeze_incident.md`.

- **인시던트 진단**: 금요일 2026-05-22 **10:30:37 ~ 10:30:39 UTC** (KST
  19:30) 윈도우에서 호스트가 hard freeze. ext4 orphan cleanup + systemd
  shutdown 로그 부재로 비정상 종료 확정. 4일간 다운 상태로 있다가 사용자가
  화요일 01:55 UTC 에 파워버튼 하드리셋으로 복구.
- **원인 미상**: NVRM Xid, OOM, MCE, kernel panic 모두 흔적 없음. AC 정전
  아님 (옆 PC 들 멀쩡, 같은 콘센트). GPU 평형 (78/81°C, 100% util, 30분
  ±1°C), load/mem 평탄. 가장 일관된 가설은 NVIDIA 드라이버 deadlock /
  PCIe stall (마지막 2분 page duration p95 130k→184k ms 점프 = 선행 신호
  후보).
- **잡 영향**: in-flight 3개 PDF (Bulat human pose, Neuralangelo, 3D
  Gaussian Splatting) 가 `done_with_errors` 로 마킹. 재업로드 필요
  (dedup hit 안 됨).
- **forensic 패턴 정립**: 4개 로그 소스 (`metrics.db` timer, journald
  timer, chandra-b stdout, `pages.completed_at` 이벤트) cross-check 로
  freeze 를 2초 윈도우까지 좁힘. **이벤트 기반(pages) 이 timer 기반보다
  훨씬 정밀.** 다음 freeze 진단 시 같은 방법 적용.
- **/metrics 그래프 quirk 확인**: AVG 시리즈(GPU/load/mem)는 빈 bucket =
  None → gap 표시, COUNT 시리즈(pages/jobs)는 빈 bucket = 0 → 0 라인
  표시. 호스트 다운 기간을 시각적으로 헷갈리게 보이는 부수효과. 코드는
  `wrapper/main.py:285-289`. 일단 인지만, 수정은 후순위.
- **watchdog 활성화 (재발 대비)**: `wdat_wdt` (ACPI WDAT, INTEL SKL
  레퍼런스) → `/dev/watchdog0` 활성. systemd `RuntimeWatchdogSec=10s`,
  PID 1 이 ping 중. 다음 freeze 시 ~30초 후 자동 reset (4일 다운 →
  30~60초 다운). 영구 설정 박힘 (`/etc/modules-load.d/watchdog.conf`,
  `/etc/systemd/system.conf.d/watchdog.conf`).
  - 함정: X299 PCH 라 처음엔 `iTCO_wdt` 시도했지만, ACPI WDAT 가 PCH
    TCO 리소스를 claim 해서 platform driver 가 silently 무동작. **WDAT
    있는 시스템은 `wdat_wdt` 가 정답.**

## 현재 상태 (snapshot)

### 운영 모드
- nginx 모드: **OCR 1 GPU + LLM** (`nginx.llm.conf` 활성, mode chip `llm+ocr`)
- 운영 디렉터리: `/srv/ocrserver/` (jikhanjung 소유, sudo 없이 docker
  compose 가능)
- 운영 컴포즈는 prebuilt image 만 참조

### 컨테이너 / 이미지 (운영서버)
```
SERVICE      IMAGE                         STATUS
chandra-a    honestjung/ocrserver:0.1.1    Up (healthy, GPU 0)
chandra-b    honestjung/ocrserver:0.1.1    (profile=ocr, 비활성)
nginx        nginx:alpine                  Up (nginx.llm.conf)
wrapper      honestjung/ocrwrapper:0.2.3   Up (WRAPPER_ROLE=ocr, OCR_CONCURRENCY=6)
llmwrapper   honestjung/ocrwrapper:0.2.3   Up (WRAPPER_ROLE=llm)
llm          vllm/vllm-openai:latest       Up (healthy, GPU 1, Qwen3-32B-AWQ)  ← 14B에서 교체
```

### DB
- `data/ocrserver.db` — OCR 잡/페이지. wrapper RW. ~1.5GB (jobs 7700+).
- `data/metrics.db` — host metrics. metrics_collector 가 RW, wrapper RO.
- `data/llmserver.db` — **신규**. llmwrapper RW, wrapper RO. WAL.

### 호스트 보호 / 메트릭
- **하드웨어 watchdog: 활성** — `wdat_wdt`, PCH timeout 30s,
  systemd ping 10s. PID 1 (`systemd`) 가 `/dev/watchdog0` 잡고 ping 중.
  `WatchdogLastPingTimestamp` 확인 가능. 다음 reboot 시
  `/sys/class/watchdog/watchdog0/bootstatus` 가 0 이 아니면 자동
  재부팅 트립 흔적.
- `ocrserver-metrics.timer` 활성, 1분 주기 → `/srv/ocrserver/data/metrics.db`
- `ocrserver-gpu-power-limit.service` 활성 — 두 RTX 8000 power limit
  230W 를 boot 마다 자동 적용 (025 에서 설치, 이번 reboot 에 첫 자동
  재적용 — 검증 별도 필요)
- wrapper 가 `/srv/ocrserver/docker-compose.yml` RO 마운트해서
  `/api/services._meta.images` 로 노출 (60s TTL)

### Friday freeze 의 잡 잔재
- DB: jobs 4020 (done 4005, done_with_errors 10, failed 5), pages
  113831 (ok 113417, failed 414). processing 0건.
- in-flight 3건 (`4711d32a` Bulat, `6947ccbe` Neuralangelo, `40c6d2a4`
  3DGS) 는 `done_with_errors` 로 reconcile 됨. 사용자가 재업로드 필요.

## 곧 해야 할 작업

백로그는 [TODOs.md](TODOs.md) 로 이동. 우선순위 + 분류 + 오늘 새로 늘어난
항목 (mode 스크립트의 nginx reload, lifespan resume sync read, 잡 fair
스케줄링, fitz.open 중복) 포함.

## 참고 위치

- 데브로그: `devlog/20260520_013_*.md` ~ `20260526_026_friday_freeze_incident.md`
- 메모리(자동 컨텍스트): `~/.claude/projects/-home-jikhanjung-projects-ocrserver/memory/`
  - 신규 추가: `reference_watchdog_setup.md` (watchdog 위치 + 검증 명령)
- 메트릭 스크립트: `scripts/metrics_collector.py`, `scripts/systemd/`
- 운영 명령:
  ```bash
  # 모드 전환 (호스트 직접)
  /srv/ocrserver/mode-ocr.sh   # 2 GPU OCR
  /srv/ocrserver/mode-llm.sh   # 1 GPU OCR + 1 GPU LLM
  # 모드 전환 (웹): /status 의 → OCR / → LLM 버튼
  # 로그
  docker compose -f /srv/ocrserver/docker-compose.yml logs -f wrapper
  # DB 인스펙트 (sudo 없이)
  docker exec ocrserver-wrapper-1 python3 -c "import sqlite3; ..." # /data/*.db
  # watchdog 상태 (sudo 없이)
  systemctl show | grep -i watchdog
  cat /sys/class/watchdog/watchdog0/state
  cat /sys/class/watchdog/watchdog0/bootstatus   # 0 = 정상, 그 외 = 트립 흔적
  ```

---

_세션 종료: 2026-07-23 — NVIDIA 드라이버 스큐(595.71.05→595.84 unattended-upgrade) 인시던트, 리부팅으로 전 스택 복구·검증. 재발 방지 blacklist(`52-nvidia-blacklist`) 적용. devlog 037 작성(untracked). 운영 `llm+ocr` 정상._
