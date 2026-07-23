# devlog 037 — NVIDIA 드라이버 버전 불일치로 GPU 백엔드 전면 다운 (unattended-upgrade)

날짜: 2026-07-23
태그: 인시던트 진단 + 재발 방지 설정 (서비스 복구는 리부팅 필요, 미완)

## 요약

호스트 리부팅(~09:24) 직후 `unattended-upgrades` 가 09:36 에 NVIDIA 드라이버를
`595.71.05 → 595.84` 로 자동 업그레이드. 실행 중 커널은 아직 구 모듈(595.71.05)을
로드한 채인데 userspace 라이브러리만 595.84 로 갈리면서 **Driver/library version
mismatch** 발생. GPU 필요한 컨테이너(chandra-a, llm)가 전부 기동 실패, OCR/LLM
백엔드 전면 다운. GPU 불필요한 `llmwrapper` 만 살아있었음.

## 증상

```
$ nvidia-smi
Failed to initialize NVML: Driver/library version mismatch
NVML library version: 595.84

$ docker ps -a --format '{{.Names}}\t{{.Status}}'
ocrserver-nginx-1        Created
ocrserver-wrapper-1      Created
ocrserver-chandra-a-1    Created
ocrserver-llmwrapper-1   Up 4 minutes          ← GPU 불필요, 유일하게 생존
ocrserver-llm-1          Exited (127) 4 minutes ago
ocrserver-chandra-b-1    Exited (0) 6 weeks ago ← 원래 profile 비활성, 정상
```

## 진단 — 로드 모듈 vs 디스크 모듈 vs 패키지 로그

버전 불일치의 3-way 대조로 원인 확정:

```
# 현재 메모리에 로드된 커널 모듈 (구버전)
$ cat /proc/driver/nvidia/version
NVRM version: ... 595.71.05 ...

# 디스크의 DKMS 모듈 + userspace (신버전)
$ modinfo nvidia | grep ^version
version:        595.84

# NVML userspace 라이브러리
NVML library version: 595.84

# dpkg 로그: 오늘 09:36 업그레이드 (부팅 ~09:24, 12분 후)
$ grep -iE "nvidia-driver|libnvidia" /var/log/dpkg.log | tail
2026-07-23 09:36:11 status installed nvidia-driver-595-open:amd64 595.84-0ubuntu0.26.04.1
2026-07-23 09:36:00 status installed libnvidia-gl-595:amd64 595.84-0ubuntu0.26.04.1
...
```

로드된 커널 모듈(595.71.05)과 userspace(595.84) 불일치가 근본 원인.
`unattended_upgrades_docker` gotcha 의 드라이버 변종 — 이전엔 docker-ce 재시작
이었고, 이번엔 드라이버 버전 스큐라 훨씬 파괴적(모든 GPU 컨테이너 정지).

## 복구 방법 (미완 — 사용자 리부팅 대기)

로드 모듈과 userspace 를 맞추려면 리부팅이 정답. 리부팅하면 595.84 모듈이
로드되고 compose restart 정책으로 컨테이너 복귀.

```bash
sudo reboot
# 복귀 후
nvidia-smi                                    # 595.84 정상 표시
docker compose -f /srv/ocrserver/docker-compose.yml --project-directory /srv/ocrserver ps
```

(모듈 reload — `modprobe -r nvidia_uvm nvidia_drm nvidia_modeset nvidia && modprobe nvidia`
— 로도 이론상 가능하나, 부팅 12분 만의 업그레이드로 상태가 어중간해 리부팅 권장.)

## 재발 방지 (적용 완료)

`unattended-upgrades` 가 NVIDIA 드라이버/라이브러리를 자동 업그레이드하지
못하도록 로컬 override blacklist 신설. `50unattended-upgrades` 직접 수정은
패키지 업데이트 시 덮어써질 수 있어 별도 파일로 분리 (apt 가 리스트 블록 병합).

`/etc/apt/apt.conf.d/52-nvidia-blacklist`:
```
Unattended-Upgrade::Package-Blacklist {
    "nvidia-";
    "libnvidia";
    "linux-firmware-nvidia";
};
```

패턴은 패키지명 앞부터 정규식 매칭(`re.match`, `-32768` 핀):
- `nvidia-` → `nvidia-driver-595-open`, `nvidia-dkms-595-open`, `nvidia-utils-595`,
  `nvidia-firmware-*`, `nvidia-container-toolkit` 등
- `libnvidia` → `libnvidia-compute-595`, `libnvidia-gl-595`, `libnvidia-decode-595` 등
- `linux-firmware-nvidia` → `linux-firmware-nvidia-graphics`

검증:
```
$ apt-config dump 'Unattended-Upgrade::Package-Blacklist'
Unattended-Upgrade::Package-Blacklist:: "nvidia-";
Unattended-Upgrade::Package-Blacklist:: "libnvidia";
Unattended-Upgrade::Package-Blacklist:: "linux-firmware-nvidia";

$ sudo unattended-upgrade --dry-run --debug 2>&1 | grep -iE "nvidia|blacklist"
Initial blacklist: nvidia- libnvidia linux-firmware-nvidia
Applying pinning: PkgPin(pkg='/^nvidia-/', priority=-32768)
Applying pinning: PkgPin(pkg='/^libnvidia/', priority=-32768)
Applying pinning: PkgPin(pkg='/^linux-firmware-nvidia/', priority=-32768)
```

## 주의점

- 이 blacklist 는 `nvidia-container-toolkit` 도 함께 고정(패턴 `nvidia-` 매칭).
  안전 우선이라 그대로 두지만, 툴킷만 자동 업데이트가 필요하면 패턴을
  개별 나열(`nvidia-driver`, `nvidia-dkms`, `nvidia-utils`, `nvidia-compute`,
  `nvidia-firmware`, `nvidia-kernel`)로 좁혀야 함.
- blacklist 는 **자동** 업그레이드만 막음. 수동 `apt upgrade` 는 여전히 드라이버를
  올리므로, 드라이버 갱신은 반드시 계획된 리부팅과 함께 의도적으로 수행.
