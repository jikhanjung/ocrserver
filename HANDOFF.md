# HANDOFF — 2026-05-21 (chandra 0.1.1 + 웹 모드 전환 + NVML 인시던트 복구)

이 파일은 작업 인수인계용. 작업 단위로 갱신.

## 방금 한 작업 (2026-05-21)

오늘은 chandra 0.1.1 swap → 웹 모드 전환 UI → NVML mismatch 인시던트 발생
→ reboot 복구 → 모드 전환 실측까지 한 호흡으로 진행. 세부는 `devlog/20260521_021_*.md`
~ `_024_*.md` 참고 (특히 #6~#9 는 024 한 묶음으로 정리).

- **세션 #2 (외부 빌드 호스트)**: chandra (`honestjung/ocrserver`) **`:0.1.1`
  빌드 + Docker Hub push**. Manifest digest
  `sha256:f6117fbbb2caa866d8349b72bc0d7d11ecc176ff1e4790562f083774fa3d380c`,
  17 GB. `:0.1.1` 과 `:latest` 둘 다 같은 digest. 020 에서 deferred 됐던
  항목. KOPRI 12% stuck 함정은 베이스 fresh pull 영향인지 이번엔 재현 안 됨.
  상세 `devlog/20260521_021_*.md`.
- **세션 #3 (운영 호스트)**: chandra-a + chandra-b 를 `:0.1.0` → `:0.1.1` swap.
  `docker pull` → compose bump → `docker compose up -d chandra-a chandra-b`
  → 두 GPU cold start 통과 후 healthy. `/api/services._meta.images` 와
  `/status` 뱃지에 0.1.1 반영 확인.
- **세션 #4**: 웹에서 모드 전환 + wrapper 재시작 동안 nginx 의 "재시작 중"
  자동 새로고침 페이지 (`/status` 의 `→ OCR` / `→ LLM` 버튼). 새 wrapper
  이미지 `:0.1.7` (digest `600a9d7213f1...`) build + Hub push + swap.
  `nginx-errors/` 디렉터리 + nginx RO 마운트, `proxy_intercept_errors`
  + `error_page → /__restarting`. 호스트 `/etc/systemd/system/ocrserver-mode-switch.{path,service}`
  활성, `/srv/ocrserver/data/mode_request` 가 생기면 `mode_switcher.sh` 가
  `mode-{ocr,llm}.sh` 실행 (docker socket 노출 없음). `.env` 에 `MODE_TOKEN`
  추가, wrapper env 로 전달. 상세 `devlog/20260521_022_*.md`.
- **세션 #5**: 모드 버튼 다듬기 → wrapper `0.1.8`. 현재 모드 버튼은
  disabled + filled 색으로 클릭 차단. 라벨 `OCR×2` / `OCR+LLM` 명료화.
  mode-{ocr,llm}.sh 의 `.env` overwrite 버그 수정 (sed in-place 로
  `OCR_CONCURRENCY` 만 갱신, `MODE_TOKEN` 등 보존). 상세 `devlog/20260521_023_*.md`.
- **세션 #6 (인시던트)**: 첫 웹 모드 전환 테스트 (OCR×2 → OCR+LLM) 시도 중
  llm 컨테이너가 `Failed to initialize NVML: Driver/library version mismatch
  — NVML library version: 595.71` 으로 기동 실패. 호스트 unattended-upgrades
  가 NVIDIA 드라이버 userland 만 업그레이드, 커널 모듈 reload 안 된 상태.
  이미 떠있던 chandra-a 는 GPU 0 reserve 중이라 영향 없지만 새 GPU 컨테이너
  못 띄움. 호스트 reboot 결정.
- **세션 #7 (reboot 복구)**: reboot 후 `ocrserver_default` 네트워크가 새 ID
  로 재생성되어 stopped chandra-b / llm 컨테이너의 옛 network 참조가 깨짐.
  `docker compose up -d chandra-b` 가 `failed to set up container networking:
  network ... not found` 로 실패. 우회: `docker compose rm -fsv chandra-b llm`
  로 stale 컨테이너 제거 후 mode-ocr.sh 재실행 → 정상 기동. cold start
  대기 동안 nginx 가 `upstream "llm"` 참조 채로 crashloop 중이라 8080
  죽어있어서, nginx.ocr.conf 를 nginx.conf 로 미리 swap + `--force-recreate
  nginx` 해서 8080 즉시 부활 (mode-ocr.sh 후속 reload 와 idempotent).
  메모리: `project_compose_network_after_reboot.md`.
- **세션 #8 (모드 전환 OCR×2 → OCR+LLM)**: 웹 버튼 → mode-llm.sh 자동 실행
  → chandra-b stop → llm (Qwen3-14B) cold start ~1분 30초 → healthy.
  mode probe `llm+ocr`. 세션 #6 에서 NVML 로 막혔던 시나리오가 처음부터
  끝까지 동작.
- **세션 #9 (모드 전환 OCR+LLM → OCR×2)**: 웹 버튼 → mode-ocr.sh → llm
  stop → chandra-b 재기동 (vLLM cold start ~4분 30초) → healthy 07:33:07Z.
  mode probe `2ocr` 확정. 양방향 모드 전환 시나리오 둘 다 실측 통과.
- **세션 #10 (GPU 전력 한도 영구화)**: 두 RTX 8000 의 power limit 230W 가
  reboot 마다 default 260W 로 휘발하던 것을 systemd oneshot unit
  (`ocrserver-gpu-power-limit.service`) 으로 boot-time 자동 적용.
  `/etc/systemd/system/` 에 설치 + `enable --now` 완료, unit `active (exited)`.
  reboot 후 자동 재적용 검증은 다음 reboot 기회에. 상세
  `devlog/20260521_025_*.md`.

## 현재 상태 (snapshot)

### 운영 모드
- nginx 모드: **OCR 2 GPU** (`nginx.ocr.conf` 활성, mode chip 상 `2ocr`)
  — chandra-b cold start 동안 일시적으로 `1ocr` 표시 가능
- 운영 디렉터리: `/srv/ocrserver/` (jikhanjung 소유, sudo 없이 docker compose 가능)
- 운영 컴포즈는 prebuilt image 만 참조 (build 는 dev tree 에서)

### 컨테이너 / 이미지 (운영서버)
```
SERVICE     IMAGE                         STATUS
chandra-a   honestjung/ocrserver:0.1.1    Up (healthy, GPU 0)
chandra-b   honestjung/ocrserver:0.1.1    Up (healthy, GPU 1)
nginx       nginx:alpine                  Up (nginx.ocr.conf, errors/ 마운트)
wrapper     honestjung/ocrwrapper:0.1.8   Up (OCR_CONCURRENCY=12)
```
OCR+LLM 모드로 전환하려면 `/status` 의 `→ LLM` 버튼 또는
`/srv/ocrserver/mode-llm.sh`.

### 이미지 hub 상태
- Docker Hub `honestjung/ocrserver`: `:0.1.0`, `:0.1.1`, `:latest` 셋 다 존재.
  `:0.1.1` 과 `:latest` 같은 digest (`f6117fbbb2ca...`).
- Docker Hub `honestjung/ocrwrapper`: `:0.1.7`, `:0.1.8` 존재. 운영은 `:0.1.8`.
- 운영서버 로컬에도 위 태그들 보존됨.

### 호스트 메트릭
- `ocrserver-metrics.timer` 활성, 1분 주기 → `/srv/ocrserver/data/metrics.db`
- wrapper 컨테이너는 read-only 로 `/data/metrics.db` mount
- wrapper 가 `/srv/ocrserver/docker-compose.yml` 도 RO 마운트
  (`/etc/ocrserver-compose.yml`) 해서 `/api/services._meta.images` 로 노출 (60s TTL)

## 곧 해야 할 작업

1. **unattended-upgrades 정책 재검토** — 세션 #6 의 NVML mismatch 가
   userland-만-업그레이드 + 커널 모듈 stale 조합으로 발생. `nvidia-*`
   패키지를 unattended-upgrades 에서 제외하거나 (`/etc/apt/apt.conf.d/50unattended-upgrades`
   의 `Package-Blacklist`), 업그레이드 직후 reboot 알림을 받는 방식 고려.
   메모리: `project_unattended_upgrades_docker.md`,
   `project_compose_network_after_reboot.md`.

## 참고 위치

- 데브로그: `devlog/20260520_013_*.md` ~ `20260521_025_*.md`
- 메모리(자동 컨텍스트): `~/.claude/projects/-home-jikhanjung-projects-ocrserver/memory/`
  - `feedback_dev_vs_ops_host.md` — 이 host 는 dev/빌드 트리이자 운영 호스트
    (현재는 같은 머신)
  - `project_compose_network_after_reboot.md` — reboot 후 stale 컨테이너
    network ID 깨짐 / 복구 절차
- 메트릭 스크립트: `scripts/metrics_collector.py`, `scripts/systemd/`
- 운영 명령:
  ```bash
  # 모드 전환 (호스트 직접)
  /srv/ocrserver/mode-ocr.sh   # 2 GPU OCR
  /srv/ocrserver/mode-llm.sh   # 1 GPU OCR + 1 GPU LLM
  # 모드 전환 (웹)
  # /status 의 → OCR / → LLM 버튼 (현재 모드 버튼은 disabled)
  # 로그
  docker compose -f /srv/ocrserver/docker-compose.yml logs -f wrapper
  # DB 인스펙트 (sudo 없이)
  docker exec ocrserver-wrapper-1 python3 -c "import sqlite3; ..." # /data/*.db
  ```
