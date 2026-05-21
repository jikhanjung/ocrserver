# 20260521_025 — GPU 전력 제한 230W 영구화 (systemd oneshot)

## 배경

이 호스트의 Quadro RTX 8000 ×2 는 default power limit 260W. 운영 중 실
소비는 보통 120-200W 대지만 burst 시 260W 까지 튀고, 환기/팬 노이즈/
전력 비용 측면에서 약간의 헤드룸을 깎아 230W 로 운용해 왔음. 이는
`nvidia-smi -pl 230` 으로 수동 설정해온 값.

```
$ nvidia-smi --query-gpu=index,power.limit,power.default_limit,power.max_limit,power.min_limit --format=csv
index, power.limit [W], power.default_limit [W], power.max_limit [W], power.min_limit [W]
0, 230.00 W, 260.00 W, 260.00 W, 100.00 W
1, 230.00 W, 260.00 W, 260.00 W, 100.00 W
```

문제: power limit 은 reboot 시 휘발 → default (260W) 로 복귀. 024 의 NVML
mismatch 인시던트에서 호스트 reboot 한 직후에도 이미 default 로 돌아간
상태였을 것 (당시엔 확인 안 함).

## 해결 — systemd oneshot unit

`scripts/systemd/ocrserver-gpu-power-limit.service`:

```ini
[Unit]
Description=Cap NVIDIA GPU power limit to 230W (Quadro RTX 8000 ×2)
After=nvidia-persistenced.service
Wants=nvidia-persistenced.service
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/nvidia-smi -pl 230

[Install]
WantedBy=multi-user.target
```

설계 의도:

- **`Before=docker.service`** — chandra-a/b 가 GPU 를 reserve 하기 전에
  power limit 이 먼저 박히도록. 사실 limit 은 동적으로 바뀌어도 무방하지만
  순서를 명확히 해 둠.
- **`After=nvidia-persistenced.service` + `Wants=...`** — nvidia 커널 모듈
  이 로드된 상태에서만 `nvidia-smi -pl` 이 동작. nvidia-persistenced 가
  static unit 이라 직접 enable 은 안 되지만, `Wants` 로 우리 unit 이 활성화
  되면 같이 끌려옴 → 커널 모듈 로드 보장.
- **`Type=oneshot` + `RemainAfterExit=yes`** — 한 번 실행되고 끝나는
  설정성 작업. systemctl status 에서 "active (exited)" 로 남게 함.

`-pl 230` 은 `-i 0,1` 인덱스 안 주면 모든 GPU 에 적용. 이 호스트엔 GPU 2장
뿐이라 그대로 OK.

## 설치 절차

```bash
sudo cp /home/jikhanjung/projects/ocrserver/scripts/systemd/ocrserver-gpu-power-limit.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ocrserver-gpu-power-limit.service
```

검증:

```bash
systemctl status ocrserver-gpu-power-limit.service
# → active (exited)

nvidia-smi --query-gpu=index,power.limit --format=csv
# → 230.00 W, 230.00 W (지금도 이미 230 이라 변화 없음)
```

reboot 검증은 다음 reboot 기회에 (예: nvidia-* 블랙리스트 작업 또는
다음 정전 시점). 아직 안 함.

## 변경 한도

power limit 외에 persistence mode (`nvidia-smi -pm 1`) 는 일부러 안 건드림.
이 unit 의 책임은 전력 한도 한 가지로 한정. persistence mode 가 필요해지면
별도 unit 또는 nvidia-persistenced 활성화로 처리.

(참고: 024 NVML mismatch 의 근본 원인은 persistence mode 가 아니라
unattended-upgrades 가 userland 만 올린 것. persistence 켰어도 같은 결과
였을 것. nvidia-* apt 블랙리스트가 진짜 처방이고 그건 별도 작업.)

## 후속

- HANDOFF.md 의 "곧 해야 할 작업" 에 unit 설치 절차 단계 추가.
- nvidia-* unattended-upgrades 블랙리스트 작업은 별도 항목으로 유지.
