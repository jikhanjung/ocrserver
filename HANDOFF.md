# HANDOFF — 2026-05-27 (/api/ no-store + nginx error passthrough, wrapper 0.1.9)

이 파일은 작업 인수인계용. 작업 단위로 갱신.

## 방금 한 작업 (2026-05-27)

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
- nginx 모드: **OCR 2 GPU** (`nginx.ocr.conf` 활성, mode chip `2ocr`)
- 운영 디렉터리: `/srv/ocrserver/` (jikhanjung 소유, sudo 없이 docker
  compose 가능)
- 운영 컴포즈는 prebuilt image 만 참조

### 컨테이너 / 이미지 (운영서버, 화요일 01:55 부팅 후)
```
SERVICE     IMAGE                         STATUS
chandra-a   honestjung/ocrserver:0.1.1    Up (healthy, GPU 0)
chandra-b   honestjung/ocrserver:0.1.1    Up (healthy, GPU 1)
nginx       nginx:alpine                  Up (nginx.ocr.conf)
wrapper     honestjung/ocrwrapper:0.1.9   Up (OCR_CONCURRENCY=12)
llm         vllm/vllm-openai:latest       Exited (0)   ← 의도대로
```

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

우선순위 순. devlog 026 의 "곧 해야 할 작업" 에서 watchdog (#1) 만 빠짐.

1. **nvidia-* unattended-upgrades 블랙리스트** (이전 HANDOFF 에서 이월).
   024 의 NVML mismatch 인시던트 + 이번 freeze 가 둘 다 NVIDIA 드라이버
   계열과 연관 가능성 있음. `/etc/apt/apt.conf.d/50unattended-upgrades`
   의 `Package-Blacklist` 에 `nvidia-*` 추가. 메모리:
   `project_unattended_upgrades_docker.md`.
2. **부팅 알림 hook** — boot 0 직후 어딘가 (Slack/이메일) 로 ping. watchdog
   이 자동 재부팅한 경우 즉시 인지. systemd `multi-user.target` 의 oneshot
   이면 충분.
3. **`metrics_collector` 확장** — CPU 온도/throttle, GPU power draw, ECC
   카운터, PCIe link state. 다음 freeze 의 진단 단서 강화. metrics.db 의
   스키마 변경 + 그래프 측 컬럼 추가.
4. **page duration p95 알림** — 분 단위 p95 가 평소의 3배 초과 시 webhook.
   이번 인시던트의 선행 신호 패턴을 실시간 감지.
5. **NVIDIA 드라이버 변경 검토** — Open Kernel Module 595.71 →
   proprietary. Turing (RTX 8000) 은 proprietary 가 더 안정적이라는 보고.
   다만 트리거 확정 못한 상태에서 큰 변경은 risk. 후순위.
6. **/api/metrics gap visualization** — 호스트 다운 시 `pages_per_step` 도
   gap 으로 표시. `wrapper/main.py:285-289` 의 default fill 정책 수정.
   미니 작업.
7. **냉장고 별도 확인** — 같은 콘센트군이라는데 다른 두 PC 멀쩡한 게
   모순. 우연일 가능성 ↑ but 직접 확인 권장.
8. **GPU power limit 230W 자동 재적용 검증** — 025 에서 설치한
   `ocrserver-gpu-power-limit.service` 가 이번 reboot 에 정상 작동했는지
   `nvidia-smi -q -d POWER | grep "Power Limit"` 로 확인.

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
