# 20260521_024 — 인시던트: NVML mismatch + reboot 복구, 모드 전환 양방향 실측

023 다음에 곧장 첫 웹 모드 전환 실측을 시도하다 인시던트 → reboot → 복구
→ 양방향 검증 통과까지 한 흐름.

## 증상 (세션 #6)

023 직후 `/status` 의 `→ LLM` 버튼을 눌러 OCR×2 → OCR+LLM 전환을 처음으로
실측. mode-llm.sh 가 chandra-b stop 까지는 정상 진행, 그러나 llm 컨테이너
기동 단계에서 즉시 실패:

```
Failed to initialize NVML: Driver/library version mismatch
NVML library version: 595.71
```

### 원인 — NVIDIA 드라이버 userland-only 업그레이드

호스트 unattended-upgrades 가 NVIDIA 드라이버 userland 패키지만 업그레이드
하면서 커널 모듈은 이전 버전으로 로드된 채. `nvidia-smi` (커널 모듈 ←→
userland 통신) 가 미스매치 거부. `journalctl` 에서 06:04 UTC 에
`nvidia-persistenced` 가 SIGTERM 으로 stop 된 흔적도 확인.

이미 GPU 0 를 reserve 하고 있던 chandra-a 는 영향 없음 (이미 열린 device
handle 은 살아있음). 새로 GPU 컨테이너를 띄우는 시점에 NVML 초기화 실패.

전 사례: 007 의 docker-ce auto-restart 와 동일 메커니즘 (unattended-upgrades),
다만 이번엔 docker 가 아니라 nvidia-* 패키지.

### 임시 상태 (reboot 직전)

- chandra-a: Up healthy (영향 없음)
- chandra-b: stopped (mode-llm.sh 가 정상적으로 멈춤)
- llm: 시작 실패 (NVML mismatch)
- nginx: Up — 다만 `nginx.conf` 가 `nginx.llm.conf` 사본 상태, `/llm/`
  upstream 이 down → 502
- wrapper 0.1.8: Up, `_mode_switching=True` 갇혀 새 OCR POST 503 거부
- mode probe: `1ocr`

호스트 reboot 필요. 호스트 재부팅으로 결정.

## reboot 후 복구 (세션 #7)

### 새 함정 — compose 네트워크 ID drift

reboot 직후:

```
NAME                    STATUS
ocrserver-chandra-a-1   Up (healthy)         ← unless-stopped 로 auto-restart
ocrserver-chandra-b-1   Exited               ← stopped 였으니 그대로
ocrserver-llm-1         Exited
ocrserver-nginx-1       Restarting (1)       ← upstream "llm" 못 찾아 crashloop
ocrserver-wrapper-1     Up                   ← fresh start
```

`/srv/ocrserver/mode-ocr.sh` 실행 → `docker compose up -d chandra-b` 단계에서:

```
Error response from daemon: failed to set up container networking:
network b2d091783701bf2ade7d233b721af8511ba87cc33a98f9db9bcf7810ca83173f not found
```

진단:

```
$ docker network ls
NETWORK ID     NAME                DRIVER    SCOPE
2d3ccb090fe8   ocrserver_default   bridge    local       ← 새 ID
```

reboot 으로 `ocrserver_default` 네트워크가 새 ID (`2d3ccb090fe8...`) 로
재생성됐는데, **stopped 상태였던** chandra-b / llm 컨테이너의 spec 에는
옛 network ID (`b2d091783701...`) 가 그대로 박혀있음. `docker compose up`
이 기존 컨테이너 start 를 시도해서 즉시 실패.

(이미 Up 상태로 부팅 시점에 attach 됐던 chandra-a 는 새 네트워크에 정상
붙어있어서 영향 없음 — 이 비대칭이 진단을 늦췄다.)

### 우회

```bash
docker compose rm -fsv chandra-b llm   # stale stopped 컨테이너 제거
/srv/ocrserver/mode-ocr.sh             # 재실행
```

vLLM 서빙 컨테이너는 stateless (모델은 이미지에 baked) 라 안전. mode-ocr.sh
가 `up -d chandra-b` 단계에서 컨테이너를 fresh 로 생성, 새 네트워크에 attach.

### 8080 즉시 부활

mode-ocr.sh 의 chandra-b health 대기 (~4-5분) 동안 nginx 가 여전히
`upstream "llm"` 참조 채로 crashloop. `/api/services` / 대시보드 접근 불가.
스크립트가 chandra-b 기다리느라 nginx 단계까지 못 가는데, nginx 처리는
chandra-b 와 무관하므로 미리 처리:

```bash
cp /srv/ocrserver/nginx.ocr.conf /srv/ocrserver/nginx.conf
docker compose -f /srv/ocrserver/docker-compose.yml up -d --no-deps --force-recreate nginx
```

mode-ocr.sh 의 후속 nginx reload 와 idempotent. 8080 → HTTP 200 즉시 복귀
(chandra-b cold start 동안 mode probe 는 `1ocr` 로 표시, OCR 업로드는
chandra-a 한 GPU 로만 처리되지만 대시보드/상태 페이지는 정상).

### 복구 완료

chandra-b healthy 후 mode-ocr.sh 가 nginx reload + wrapper recreate 마무리.
mode probe `2ocr`, OCR_CONCURRENCY=12 확인.

## 모드 전환 양방향 실측 (세션 #8, #9)

### #8: OCR×2 → OCR+LLM

`/status` 의 `→ LLM` 버튼 → systemd path unit (`ocrserver-mode-switch.path`)
이 `/srv/ocrserver/data/mode_request` 를 잡고 `mode_switcher.sh` 가
mode-llm.sh 실행 → chandra-b stop → llm (Qwen3-14B) cold start **~1분 30초**
→ healthy at 07:25:26Z. mode probe `llm+ocr`.

NVML 정상이라 이번엔 llm 컨테이너가 처음부터 끝까지 정상 기동. 세션 #6
에서 막혔던 시나리오 통과.

### #9: OCR+LLM → OCR×2

`→ OCR` 버튼 → mode-ocr.sh → llm stop → chandra-b 재기동 → vLLM cold start
**~4분 30초** → healthy at 07:33:07Z. mode probe `2ocr` 확정.

(이번엔 #7 과 달리 chandra-b 가 같은 lifecycle 안에서 stop 됐다 다시
start 되는 경로라 stale network 문제는 발생하지 않음.)

양방향 전환 + 도중 nginx "재시작 중" 페이지(`/__restarting`) + 완료 후
대시보드 자동 새로고침 전부 의도대로 동작.

## 학습

- **nvidia-* 도 unattended-upgrades 블랙리스트 필요.** docker-ce 는 007
  이후 이미 블랙리스트에 있지만 nvidia-* 는 빠져있었음.
  `/etc/apt/apt.conf.d/50unattended-upgrades` 의 `Package-Blacklist` 갱신
  필요 (다음 작업).
- **호스트 reboot 후 stopped compose 컨테이너는 stale.** 새 메모리:
  `project_compose_network_after_reboot.md`. `rm -fsv` 후 `up -d` 로 복구.
  호스트 reboot 빈도가 낮아서 그동안 만나지 못했던 함정.
- **mode-*.sh 의 chandra cold start 대기 중에 nginx 만 먼저 처리하는
  우회로** 가 8080 가시성 회복에 효과적. 스크립트 자체를 고치진 않음
  (정상 흐름에선 nginx 가 살아있는 상태에서 reload 만 하면 되므로 현재
  순서가 맞음). 인시던트 복구 한정 기법.

## 후속

- HANDOFF.md 정리: 오늘 9개 세션 chronologic 으로 재정렬, NVML 인시던트
  블록 제거, 곧 해야 할 작업에 `nvidia-*` 블랙리스트 단일 항목.
- 메모리: `project_compose_network_after_reboot.md` 추가.
