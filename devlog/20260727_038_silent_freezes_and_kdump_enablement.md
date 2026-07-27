# devlog 038 — 무증상 호스트 프리즈 4회 (watchdog 이 조용히 복구) + kdump 활성화

날짜: 2026-07-27
태그: 인시던트 발견 + forensic 인프라 구축 (근본 원인 미상, 다음 프리즈에서 vmcore 확보 목표)

## 요약

세션 시작 시 상태 점검 중 **2026-07-26 하루에 호스트가 4번 freeze** 했고
매번 하드웨어 watchdog 이 자동 리셋했다는 사실을 발견. devlog 026 의 금요일
프리즈와 **동일한 시그니처** (커널 메시지 한 줄 없이 뚝 끊김).

사용자는 인지하지 못한 상태였음 — watchdog 이 70~100초 만에 복구했고
compose restart 정책으로 컨테이너가 전부 돌아와서, 밖에서 보면 아무 일도
없었던 것처럼 보였기 때문. **026/037 에서 만든 watchdog 이 설계대로 동작한
첫 실전 사례**이자, 동시에 "복구가 너무 매끄러워서 장애가 은폐된다"는
부작용의 첫 사례.

원인 규명이 계속 막히는 근본 이유는 **로그가 아무것도 안 남기 때문**.
그래서 이번 세션의 산출물은 원인 규명이 아니라 **다음 프리즈에서 vmcore 를
확보할 수 있게 만드는 것**.

## 발견 — 프리즈 4회

`journalctl --list-boots` 로 부팅 경계 확인:

```
 -4  Thu 2026-07-23 18:37:46 UTC — Sun 2026-07-26 04:17:29 UTC   (2일 9.7시간 안정)
 -3  Sun 2026-07-26 04:19:09 UTC — Sun 2026-07-26 04:42:27 UTC
 -2  Sun 2026-07-26 04:43:37 UTC — Sun 2026-07-26 06:26:53 UTC
 -1  Sun 2026-07-26 06:28:19 UTC — Sun 2026-07-26 19:26:30 UTC
  0  Sun 2026-07-26 19:27:49 UTC — (현재)
```

부팅 간격 = **100s / 70s / 86s / 79s**. watchdog 트립(10초) + POST/부팅
시간과 일치하는 전형적인 자동 리셋 패턴.

## 죽을 때 로그 — 4번 모두 완전히 동일

```
Jul 26 19:26:30.634884 systemd[1]: Starting ocrserver-metrics.service...
Jul 26 19:26:30.805134 systemd[1]: ocrserver-metrics.service: Deactivated successfully.
Jul 26 19:26:30.805803 systemd[1]: Finished ocrserver-metrics.service.
                                    ← 여기서 끝. 다음 줄이 없음
```

1분 주기 메트릭 타이머의 "Finished" 한 줄 뒤로 **아무것도 없음**:

- shutdown / reboot 타깃 없음 (정상 종료 아님)
- kernel panic / oops / BUG 없음
- **Xid / NVRM 없음** (devlog 034 NVLink, 037 드라이버 스큐와 구별됨)
- MCE / Hardware Error 없음
- OOM 없음
- 마지막 수 분간 커널 메시지 자체가 0건

`metrics.db` 의 1분 샘플도 마지막 샘플까지 완전 평탄 — 선행 신호 전무:

```
load1 1.9~2.0 | mem 10.2GB | gpu0 0% 41°C | gpu1 100% 78°C
```

devlog 026 때 관찰됐던 "마지막 2분 page duration p95 130k→184k ms 점프"
같은 선행 신호가 이번엔 **없음**. 단 이번엔 OCR 이 유휴라 page 이벤트
자체가 없어서 그 채널을 못 쓴 것이기도 함.

## 동시간대 2차 현상 — vLLM EngineCore 크래시 9회

`llmserver.db` 기준 07-26 에 `EngineDeadError` (HTTP 500) 9건:

```
03:15:04  03:43:52  03:51:16  04:13:05  04:28:58  05:00:52  05:55:26
19:46:28  20:05:22
```

각 500 직후 `All connection attempts failed` 3건씩 — vllm 컨테이너가 죽고
재시작되는 동안 llmwrapper 가 붙지 못한 것. 컨테이너 `RestartCount=2`,
restart 정책으로 자력 복구, 20:05 이후 현재까지 8시간 안정.

**프리즈와의 관계는 미확정.** 같은 시간대에 몰려 있으나 선후가 일정하지 않음:

| 크래시 | 프리즈 | 관계 |
|---|---|---|
| 04:13 | 04:17 (+4분) | 크래시 → 프리즈 |
| 04:28 | 04:42 (+14분) | 크래시 → 프리즈 |
| 05:00, 05:55 | 06:26 (+31분) | 크래시 → 프리즈 |
| — | 19:26 | 선행 크래시 없음 |
| 19:46, 20:05 | — | 프리즈 **이후** 크래시 |

03:15 / 03:43 / 03:51 크래시는 프리즈로 이어지지 않았고, 19:26 프리즈는
선행 크래시가 없었음. 공통 원인(GPU/드라이버 레벨)의 서로 다른 발현일
가능성은 있으나 현 증거로는 인과 주장 불가.

크래시 시점의 vllm 로그는 **이미 유실**됨 (`daemon.json` 에 로깅 설정이
없어 시스템 logrotate 가 돌면서 07-26 03:00~06:30 / 19:40~20:10 구간이
`docker logs` 에서 0줄). 06-25 에 남아있던 동종 크래시의 흔적은
faulthandler 의 C 스택 덤프 — EngineCore 프로세스가 CUDA 작업 중
시그널로 죽었다는 뜻이지만 시그널 종류는 확인 불가.

## 핵심 문제 — 진단 인프라가 통째로 죽어 있었다

프리즈 원인을 못 잡는 이유를 파고들었더니, **vmcore 를 뜰 수 있는 체인이
세 군데 모두 끊겨 있었음**.

### 고리 1 — kdump 가 애초에 꺼져 있었음

```
$ cat /sys/kernel/kexec_crash_loaded
0                          # 크래시 커널 미로드
$ ls /var/crash/
(비어 있음)
```

crashkernel 메모리는 2.3GB 예약돼 있고(`/proc/cmdline` 에
`crashkernel=...64G-128G:2048M`) `kdump-tools.service` 도
`active (exited)` 로 멀쩡히 떠 있었는데 실제로는 아무것도 안 하고 있었음.

원인은 Ubuntu 기본값 `/etc/default/kdump-tools` 의 **`USE_KDUMP=0`**.
`/etc/init.d/kdump-tools` 첫 줄이:

```sh
[ "$USE_KDUMP" -ne 0 ] || exit 0;
```

**→ 서비스 status 만 보면 속는다.** `active (exited)` + `status=0/SUCCESS`
로 정상처럼 보이지만 exit 0 로 빠져나간 것. 반드시
`cat /sys/kernel/kexec_crash_loaded` 로 확인해야 함.

### 고리 2 — 조용한 프리즈는 panic 으로 전환되지 않음

```
kernel.hardlockup_panic = 0
kernel.panic_on_oops    = 0
kernel.softlockup_panic = 0
kernel.nmi_watchdog     = 1     # 감지는 하지만 경고만 찍고 끝
```

NMI watchdog 은 켜져 있어서 하드락업을 **감지**는 하지만, 기본값에서는
경고만 출력하고 panic 시키지 않음. kdump 는 panic 을 트리거로 동작하므로
**panic 이 안 나면 kdump 는 영원히 안 돈다.** 우리 증상(무증상 정지)에
가장 결정적인 고리.

### 고리 3 — 하드웨어 watchdog 이 덤프보다 먼저 리셋 (가장 놓치기 쉬움)

```
$ cat /sys/class/watchdog/watchdog0/timeout
10
$ sysctl kernel.watchdog_thresh
kernel.watchdog_thresh = 10
```

NMI 하드락업 판정은 `watchdog_thresh` 의 약 2배인 **~20초** 뒤에 남.
그런데 하드웨어 watchdog 은 **10초**에 리셋. 즉 고리 1·2 를 고쳐도
**판정이 나기 전에 박스가 죽어서 구조적으로 영원히 못 잡는 상태**였음.

필요 시간 산정:

```
락업 판정        ~20s   (watchdog_thresh 10 × 2)
kexec + 캡처커널 부팅  ~15s
makedumpfile -c -d 31  ~30-60s   (호스트 사용 RAM 10GB 기준)
────────────────────────────────
합계             ~65-95s
```

다행히 `max_timeout=613` 이라 상향 여지가 충분했음.

> **부수 정정**: 기존 메모리에 "watchdog timeout 은 30초이고 WDAT spec 상
> set-timeout 액션이 없어 변경 불가" 라고 기록돼 있었으나 **둘 다 오류**.
> 실제 값은 10초였고(systemd `RuntimeWatchdogSec` 이 하드웨어 타임아웃을
> 그대로 프로그램함), `max_timeout=613` / `min_timeout=2` 로 변경 가능.

## 적용 (완료)

```bash
# ── 1. kdump 활성화 ──────────────────────────────────────
sudo sed -i 's/^USE_KDUMP=0/USE_KDUMP=1/' /etc/default/kdump-tools
echo 'KDUMP_NUM_DUMPS=5' | sudo tee -a /etc/default/kdump-tools   # 루트 83% 사용중
sudo kdump-config symlinks
sudo kdump-config load

# ── 2. 하드락업을 panic 으로 전환 ────────────────────────
sudo tee /etc/sysctl.d/60-lockup-panic.conf >/dev/null <<'EOF'
kernel.hardlockup_panic = 1
kernel.panic_on_oops = 1
kernel.panic = 60
EOF
sudo sysctl --system

# ── 3. HW watchdog 타임아웃 확대 (덤프 시간 확보) ────────
sudo tee /etc/systemd/system.conf.d/watchdog.conf >/dev/null <<'EOF'
[Manager]
RuntimeWatchdogSec=180s
RebootWatchdogSec=10min
EOF
sudo systemctl daemon-reexec
```

`kdump-config symlinks` 가 `Invalid argument : missing kernel version` 을
출력하지만 무해 — 인자 없이 호출한 탓이고, 이어서 `vmlinuz` 링크 생성 +
`-28` initrd 재생성 + `initrd.img` 링크 생성을 모두 수행함.

### `softlockup_panic` 은 의도적으로 제외

GPU1 이 PaperMeister 레퍼런스 추출로 24/7 100% 물려 있는 상태라,
소프트락업 오탐으로 **멀쩡한 박스가 재부팅될 위험**이 실재. `hardlockup`
만 panic 으로 전환.

### 검증

```
$ cat /sys/kernel/kexec_crash_loaded          → 1
$ cat /sys/class/watchdog/watchdog0/timeout   → 180
$ sysctl kernel.hardlockup_panic kernel.panic_on_oops
kernel.hardlockup_panic = 1
kernel.panic_on_oops = 1
$ kdump-config status
current state   : ready to kdump
```

컨테이너 5개 전부 무영향 (`daemon-reexec` 은 PID 1 재실행만 하고 서비스는
건드리지 않음).

## 커널 범프 내성 확인 — 037 함정은 여기 해당 없음

devlog 037 에서 커널 범프가 per-kernel denylist 로 wdat_wdt 를 죽인 전례가
있어 kdump 도 같은 취약성이 있는지 점검했음. **결론: 자가 치유됨.**

- `kdump-tools.service` → `/etc/init.d/kdump-tools start` → `kdump-config load`
- `load()` 가 매 부팅마다 `manage_symlinks` 호출 → `KVER=$(uname -r)` 기준으로
  `/var/lib/kdump/{vmlinuz,initrd.img}` 링크를 검사하고 어긋나면 재생성
- 새 커널의 kdump initrd 는 `/etc/kernel/postinst.d/kdump-tools` 훅이
  패키지 설치 시점에 미리 생성 (현재 `-27`, `-28` 둘 다 준비됨)

즉 커널이 `-29` 로 올라가도 심볼릭 링크가 자동으로 따라감.

## 한계 (기대치 관리)

프리즈가 **CPU 하드락업**이면 이제 잡히지만, PCIe fatal / CPU 완전 정지
같은 펌웨어 레벨 정지면 **NMI 자체가 안 떠서 vmcore 없이 그냥 리셋**됨.
07-26 프리즈들은 커널 메시지가 0줄이라 둘 중 어느 쪽인지 **아직 미상** —
양쪽 다 같은 로그(=무로그)를 남기기 때문.

**다음 프리즈 때 `/var/crash/` 가 비어 있으면 후자로 판정**하고
netconsole 또는 serial console 로깅으로 넘어갈 것.

## 비용

- freeze 자동복구 시간이 10초 → 최대 180초로 늘어남. 실측 복구가
  70~100초(대부분 POST/부팅)였으니 체감은 ~1.5분 → 최대 4.5분.
  OCR 이 유휴고 LLM 은 배치라 감수 가능하다고 판단.
- `/var/crash` 는 루트 파티션(83% 사용, 59GB 여유). 압축 덤프 수백 MB~2GB
  예상, `KDUMP_NUM_DUMPS=5` 로 상한.

## 미검증 항목

**end-to-end 실동작 테스트 미수행.** `echo c | sudo tee /proc/sysrq-trigger`
로 의도적 panic 을 내면 vmcore 가 실제로 떨어지는지, 180초 안에 덤프가
끝나는지 확인 가능하나, 박스가 재부팅되고 PaperMeister 배치가 끊기므로
한가한 시점으로 보류. 참고로 현재 `kernel.sysrq = 176` 이라 crash 비트
(0x40)가 꺼져 있어 테스트 전 `sudo sysctl -w kernel.sysrq=1` 필요.

## 그 외 발견 (미처리)

- **bootstatus 는 이 보드에서 신뢰 불가**: 07-26 에 watchdog 리셋이 4번
  있었는데 `/sys/class/watchdog/watchdog0/bootstatus` 는 계속 **0**.
  devlog 026 에 "≠0 이면 트립 흔적" 이라고 적어뒀으나 이 하드웨어에선
  성립하지 않음. **`0` 을 보고 "트립 없었다"고 결론내면 정반대로 틀림.**
  대신 `journalctl --list-boots` + 각 부팅 마지막 줄 확인을 쓸 것.
- **docker 로그 로테이션 미설정**: `/etc/docker/daemon.json` 에 로깅 설정이
  없어 크래시 구간 로그가 유실됨. `max-size`/`max-file` 명시 필요.
- 부팅마다 `systemd-modules-load: Module 'wdat_wdt' is deny-listed (by kmod)`
  경고 — 037 에서 대체된 옛 `modules-load.d` 경로의 잔재. oneshot 이 뒤에서
  로드하므로 무해하나 혼동 유발.
- `wdat-watchdog-load.service:3` 의 `Documentation=` 에 URL 아닌 텍스트가
  들어가 부팅마다 `Invalid URL` 경고 3줄. 순수 미관 문제.

## 참고 — OCR 은 7주째 유휴

이번 조사 중 확인: 마지막 OCR 잡이 **2026-06-09 07:00** (누적 7,767건,
done 7,720 / done_with_errors 12 / failed 35). GPU0 은 chandra-a 가 메모리만
점유한 채 util 0%. 현재 호스트 부하는 전부 LLM(GPU1) 쪽 —
PaperMeister 레퍼런스 추출이 하루 ~2,200건 / ~400만 토큰으로 24/7 가동 중.
