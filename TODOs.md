# Backlog

작업 완료된 항목은 devlog 에 기록 후 여기서 제거. 우선순위는 대략 위에서
아래. 세션 인수인계 (방금 한 작업 / 현재 상태) 는 [HANDOFF.md](HANDOFF.md).

## 운영 안정성

- **nvidia-* unattended-upgrades 블랙리스트** — 024 NVML mismatch + 026
  freeze 둘 다 NVIDIA 드라이버 계열 의심. `/etc/apt/apt.conf.d/50unattended-upgrades`
  의 `Package-Blacklist` 에 `nvidia-*` 추가. 메모리:
  `project_unattended_upgrades_docker.md`.
- **부팅 알림 hook** — boot 0 직후 Slack/이메일 ping. watchdog 자동
  재부팅한 경우 즉시 인지. systemd `multi-user.target` oneshot 이면 충분.
- **GPU power limit 230W 자동 재적용 검증** — 025 의
  `ocrserver-gpu-power-limit.service` 가 정상 작동하는지 매 reboot 후
  `nvidia-smi -q -d POWER | grep "Power Limit"` 확인. 자동화 가능.
- **mode-*.sh 의 nginx reload → force-recreate** (devlog 028 발견) —
  현재 `cp nginx.X.conf nginx.conf` + `nginx -s reload` 패턴이 Docker
  single-file bind mount 의 inode 고정 때문에 무효. mode 전환 시 새
  config 가 컨테이너에 반영 안 됨. `docker compose up -d --no-deps
  --force-recreate nginx` 로 교체. 메모리: `project_docker_bind_mount_inode.md`.

## Wrapper 코드

- **lifespan resume 의 sync `f.read()` to_thread화** (devlog 031 발견) —
  큰 잡 여러 개 in-flight 인 채로 wrapper 재기동 시 PDF 들을 sync 로 읽어
  startup 50s+ 지연. 모두 to_thread 화 + 병렬 read 하면 ~10s 이하.
- **잡 fair 스케줄링** (devlog 029/031 발견) — 한 잡이 `_sem` 12 슬롯
  FIFO 큐를 다 채우면 후속 잡 starve. round-robin 또는 잡당 슬롯 cap
  (예: max 6) 필요. per-page 디자인 (0.1.14) 으로 starvation 영향은
  좀 줄었지만 (큰 잡 _ocr_page 들이 chandra 대기 동안 다른 잡 슬롯
  사이에 끼어들 여지 생김) 근본 해결은 별건.
- **per-page render 의 fitz.open 중복** (devlog 031 trade-off) — 페이지마다
  `fitz.open()` 새로 호출하면 큰 PDF 의 헤더 파싱이 ~10ms/페이지 누적
  (1773p = ~18s). 실 throughput 영향 미미하지만, per-job fitz doc 캐시
  하면 더 깔끔. thread safety 확인 필요.
- **/api/metrics gap visualization** — 호스트 다운 시 `pages_per_step` 도
  gap 으로 표시. `wrapper/main.py:285-289` 의 default fill 정책 수정.
  미니 작업.

## 관측성

- **`metrics_collector` 확장** — CPU 온도/throttle, GPU power draw, ECC
  카운터, PCIe link state. 다음 freeze 의 진단 단서 강화. metrics.db
  스키마 + 그래프 컬럼 추가.
- **page duration p95 알림** — 분 단위 p95 가 평소의 3배 초과 시 webhook.
  freeze 선행 신호 패턴 실시간 감지.

## 하드웨어 / 외부

- **NVIDIA 드라이버 변경 검토** — Open Kernel Module 595.71 →
  proprietary. Turing (RTX 8000) 은 proprietary 가 더 안정적이라는 보고.
  트리거 확정 못한 상태에서 큰 변경이라 후순위.
- **냉장고 별도 확인** — 026 freeze 시 같은 콘센트군의 다른 두 PC 멀쩡
  했던 게 모순. 우연일 가능성 높지만 직접 확인 권장.
