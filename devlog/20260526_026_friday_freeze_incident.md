# 20260526_026 — 인시던트: 금요일 호스트 freeze, 3.65일 다운, 화요일 하드 리셋 복구

025 (GPU power limit systemd) 다음 첫 세션. 사용자가 화요일 출근해 "지난주
금요일 OCR 작업 돌려놓고 갔는데 중간에 서버에 문제가 생긴 것 같다" 고 보고.
원인 진단을 위해 가용한 로그를 cross-check 한 기록.

원인은 끝내 단정 못함 — 호스트가 평형 상태에서 갑자기 hard freeze 됐고,
디스크에 panic/Xid/MCE 흔적이 하나도 안 남음. 그러나 진단 과정에서 freeze
시점을 ~2초 윈도우까지 좁혔고, **선행 신호로 보이는 page duration slowdown**
패턴을 발견했음. 다음 freeze 가 재현되면 비교할 baseline 으로 남김.

## 사용자 첫 보고와 초기 진단

- 화요일 오전 10:55 KST 출근, 호스트가 꺼져있어서 파워버튼 길게 눌러 하드
  리셋. 즉 사용자가 자동으로 켜진 게 아니라 직접 켰음.
- 호스트 uptime 3분 시점에 조사 시작.

### 부팅 히스토리

```
journalctl --list-boots
 ...
 -2 6076804468... Tue 2026-05-12 01:21:42 UTC → Thu 2026-05-21 07:05:49 UTC
 -1 46d5aed393... Thu 2026-05-21 07:06:11 UTC → Fri 2026-05-22 10:30:01 UTC
  0 9f99b2d553... Tue 2026-05-26 01:55:12 UTC → (현재)
```

부팅 -1 의 LAST ENTRY 가 **Fri 2026-05-22 10:30:01 UTC** (KST 금요일 19:30)
에서 끊김. 그 다음 부팅이 오늘 화요일 01:55 UTC. **약 3.65일간 호스트가
꺼져 있다가 사용자가 켠 것.**

### 비정상 종료의 증거

부팅 -1 의 마지막 systemd 로그가 1분 주기 `sysstat-collect` 의 정상 종료
1줄이고, 그 뒤로 systemd 의 `Stopping`/`shutdown`/`poweroff` 메시지가
**한 줄도 없이 끊김**. 깨끗한 종료라면 수십 줄의 stop 메시지가 나옴.

오늘 부팅(boot 0) 초반에:

```
EXT4-fs (dm-0): orphan cleanup on readonly fs
EXT4-fs (dm-0): mounted filesystem ... ro with ordered data mode.
```

`orphan cleanup` 은 ext4 가 unclean 상태였을 때 마운트 시 자동으로 도는
복구. **fs 가 깨끗하게 unmount 된 적이 없다는 직접 증거.**

부팅 -1 전체에서 `NVRM Xid`, `oom-killer`, `MCE`, `kernel panic`,
`watchdog` 트리거 — **단 한 줄도 없음**. `/var/crash` 비어있고
(`kdump_lock` 0바이트만), pstore 도 비어있음 (권한). kdump 가 작동하려면
시스템이 panic 핸들러를 실행할 수 있어야 하는데, **panic 도 안 적은 채
통째로 frozen** 상태였다는 뜻.

## 가설 좁히기 과정

### 1차 가설: 정전 → 폐기

- 깨끗한 종료가 아니고 panic 도 없으니 외부 전원 차단이 1순위로 보였음.
- 사용자 확인: **옆 두 PC (리눅스 서버 + 윈도우 PC) 는 멀쩡**, **연구실 냉장고도
  같은 날 죽은 것 같다**.
- 처음엔 차단기 트립 (데스크탑 + 냉장고 = 같은 회로 과부하) 으로 좁혔으나,
- 사용자 추가 보고: **셋 다 같은 파워 아울렛, 다른 두 PC 멀쩡, 방금 리부팅
  전까지 전원은 들어와 있었음, 하드 리셋 했음**.

→ AC 정전/차단기 트립 가설 폐기. **호스트만 freeze, AC 는 살아있었음.**
냉장고 죽음은 우연일 가능성 (별도 확인 필요).

### 2차 가설: 하드웨어 freeze / 드라이버 deadlock

소프트웨어 panic 흔적 없음 + AC 살아있음 + 화면도 안 떴음 (사용자가 파워
버튼 길게 눌러야 했다는 사실로 확증) → 통째로 hard freeze.

가능성:
- NVIDIA 드라이버 deadlock (Open Kernel Module 595.71, RTX 8000 Turing)
- PSU/VRM 글리치 (steady-state 에서도 droop 가능)
- CPU 과열 (사무실 에어컨 여부, 5월 말 한국)
- PCIe 링크 장애

## Freeze 시점 좁히기 — cross-source 타임라인

핵심 발견: **다른 로그들이 freeze 시점에 끊긴 시각이 서로 다르게 보이지만,
그건 기록 cadence 차이일 뿐 셋 다 같은 ~2초 윈도우에서 동시 정지.**

| 소스 | 마지막 entry (UTC) | 기록 방식 |
|---|---|---|
| `metrics.db` (ocrserver-metrics.timer) | 10:29:39 | 1분 timer (OnUnitActiveSec) |
| journald (sysstat-collect) | 10:30:01 | 1분 timer (OnCalendar) |
| chandra-b stdout (docker log) | 10:30:35 | vLLM `loggers.py`, 10초 간격 |
| `pages.completed_at` (status=ok) | **10:30:37** | 이벤트 기반 (페이지 완료마다) |

그리고:
- 다음 metrics fire 예정: 10:30:39 → 안 적음
- 다음 sysstat fire 예정: 10:31:01 → 안 적음
- 다음 chandra-b loggers 라인 예정: 10:30:45 → 안 적음

따라서 freeze 시각:

```
10:30:37  확정적으로 살아있음 (page DB write 성공)
10:30:39  이미 죽음 (metrics timer 가 fire 못함)
        ─ 약 2초 윈도우 ─
```

KST 19:30:37 ~ 19:30:39. 사용자가 PC 앞을 떠나기 직전 시간대.

### "OCR 처리량은 freeze 후에도 기록된 것처럼 보임" 의 정체

사용자가 `http://172.16.112.150:8080/metrics` 그래프에서 OCR 처리량
(`pages_per_step`) 시리즈가 3.65일 갭 동안 끊김 없이 0 라인으로 그려진
것을 보고 "OCR 처리량만 계속 기록되고 있었던 것 같다" 고 질문.

`wrapper/main.py:259-345` `/api/metrics` 핸들러를 보면:

```python
for col in _METRIC_COLS:
    series[col] = [None] * len(ts_grid)          # GPU/load/mem
series["jobs_per_step"]  = [0] * len(ts_grid)    # COUNT 계열
series["pages_per_step"] = [0] * len(ts_grid)    # COUNT 계열
```

- AVG 계열 (GPU/load/mem) 은 빈 bucket 을 None 으로 두고 → 그래프 gap
- COUNT 계열 (jobs/pages) 은 빈 bucket 을 0 으로 두고 → 그래프 0 라인

평상시엔 둘 다 자연스러운 default. 다만 **호스트 다운 시엔
"데이터 없음" 과 "0건 처리" 가 시각적으로 구분되지 않는다.** DB 확인:

```sql
SELECT COUNT(*) FROM pages
 WHERE completed_at > strftime('%s','2026-05-22 10:31')
   AND completed_at < strftime('%s','2026-05-26 01:55');
-- → 0
```

페이지도 정확히 freeze 시점에 끊겼고, 화요일 부팅 후 wrapper 가 in-flight
3개 잡의 미완료 페이지 10건을 `status='failed'` + `completed_at = 01:59:12`
로 reconcile 한 것만 4일 후 들어와 있음.

→ **OCR 처리량 시리즈만 더 오래 산 게 아니라, 단지 매 페이지 완료라는
이벤트 기반으로 적혀서 freeze 직전 2초 윈도우까지 가장 fine-grained 한
기록을 남긴 것뿐.** 그래프의 0 라인은 데이터가 아니라 default fill.

이건 진단 측면에서 큰 시사점: **이벤트 기반 메트릭이 timer 기반보다
freeze forensics 에 훨씬 유용.** timer 만 보면 ±30~60초 오차, 이벤트
기반이면 초 단위.

## Freeze 직전 ~30분 시스템 상태

`metrics.db` 에서 (1분 간격):

| 시각 (UTC) | load1 | mem(MB) | GPU0 util/°C | GPU1 util/°C |
|---|---|---|---|---|
| 10:00:22 | 2.18 | 26,436 | 100% / 78 | 100% / 81 |
| 10:10:49 | 2.10 | 26,398 | 100% / 78 | 100% / 81 |
| 10:20:17 | 2.32 | 27,233 | 100% / 78 | 100% / 81 |
| 10:25:29 | 2.25 | 27,257 | 100% / 78 | 100% / 81 |
| 10:28:36 | 1.77 | 27,445 | 100% / 70 ← dip | 100% / 80 |
| **10:29:39** | **1.96** | **27,385** | **100% / 78** | **100% / 80** |

- GPU0 78°C, GPU1 81°C 가 **30분 이상 ±1°C 안에서 평형**. RTX 8000 slowdown
  임계(89°C) 까지 8~10°C 마진.
- load1, mem, GPU mem 모두 평탄. **누적 가열/메모리 누수/리소스 고갈 패턴
  아님.**
- 10:28:36 의 GPU0 70°C dip 은 잡 사이 짧은 idle (load1 도 같이 dip).
  1분 뒤 평형 복귀. freeze 와 직접 인과 없을 가능성.

GPU power 컬럼이 metrics_collector 에 없어서 power draw 변화는 확인 불가.
CPU 온도/VRM 온도도 없음.

## Freeze 직전 ~2분의 선행 신호 — page duration p95

이게 이번 인시던트의 가장 가치 있는 발견. `pages.duration_ms` 의 분 단위
집계:

| 시각 (UTC) | 페이지/분 | avg ms | p95 ms |
|---|---|---|---|
| 09:50~10:27 (평소) | 13~24 (~18) | 30~47k | 45~130k |
| 10:28 | 5 | 29k | 37k |
| **10:29** | **12** | **60k** | **184k** ← p95 폭증 |
| **10:30** | **10** | **62k** | 99k ← avg 평소의 1.7배 |

마지막 페이지 ts 2026-05-22 **10:30:37** (chandra-b 의 docker log 마지막
줄 10:30:35 와 일치).

해석:
- GPU util 은 100% 유지, throughput (분당 페이지수) 도 평소 범위 유지
  (vLLM 이 6 reqs 동시 처리로 cover).
- 그러나 **각 페이지의 wall-clock 처리 시간이 평소의 ~75% 더 길어짐**.
- GPU 작업 자체는 도는데 request 별 round-trip 이 느려지는 패턴 →
  PCIe 트랜잭션 retry, 드라이버 lock contention, GPU↔CPU 통신 stall 등
  **드라이버/PCIe 레이어의 점진적 stall** 신호와 일관.
- 이게 freeze 의 root cause 인지 단순 동반 증상인지는 단정 못함. 다만
  **다음 freeze 가 재현되면 같은 패턴이 보이는지 확인할 baseline**.

이 패턴은 PSU 글리치 가설 (즉각 reset/hang) 보다 드라이버 deadlock 가설
(점진적 누적 후 hard hang) 쪽과 더 일관됨. 다만 확정은 아님.

## 잡 영향

금요일 작업 중단 시점 in-flight 였던 PDF **3건**이 끊겼다가 화요일 부팅 시
wrapper 가 `done_with_errors` 로 마킹:

| submitted (Fri UTC) | 파일 | 진행 |
|---|---|---|
| 10:30:09 | Bulat et al. — human pose estimation | 8p 중 0p |
| 10:29:21 | Li et al. — Neuralangelo | 10p 중 9p |
| 10:28:21 | Kerbl et al. — 3D Gaussian Splatting | 14p 중 1p |

이 잡들의 미완료 페이지 10건이 `pages` 테이블에 `status='failed'`,
`completed_at=2026-05-26 01:59:11~12`, `duration_ms` 약 110초로 적혔음
(chandra timeout 의 잔재). 사용자가 재업로드하면 dedup 안 되고 새 잡으로
들어감 (`status='done_with_errors'` 는 dedup hit 대상 아님).

금요일 9:30~10:30 한 시간 동안 총 1,083 페이지 모두 `ok` 로 정상 처리됨
(실패 0건). freeze 자체와는 무관.

## 복구

사용자가 파워버튼 길게 눌러 하드 리셋. boot 0 의 systemd 가 정상 부팅,
compose 자동 기동. 이번엔 024 의 stale network ID 문제는 발생하지 않음
— 모든 컨테이너가 같은 reboot 윈도우에 새로 생성됐기 때문.

화요일 부팅 후 약 3분 시점:

```
chandra-a   honestjung/ocrserver:0.1.1   Up (health: starting, GPU 0)
chandra-b   honestjung/ocrserver:0.1.1   Up (health: starting, GPU 1)
nginx       nginx:alpine                 Up (nginx.ocr.conf)
wrapper     honestjung/ocrwrapper:0.1.8  Up (OCR_CONCURRENCY=12)
llm         vllm/vllm-openai:latest      Exited (0) ← 의도대로
```

cold start ~4-5분 후 OCR 처리 정상 가능. 모드 `OCR×2` 유지.

025 에서 활성화한 `ocrserver-gpu-power-limit.service` 가 boot 직후
power limit 230W 를 자동 적용했는지 다음 reboot 에서 검증 예정이었는데,
이번 reboot 이 그 검증 기회가 됨 (별도 확인 필요).

## 학습

- **freeze forensics 엔 이벤트 기반 로그가 결정적.** 1분 timer 만 보면
  ±60초 윈도우, 이벤트 기반(page completion) 이면 초 단위까지 좁힘.
  이번엔 4개 소스 (timer 2개 + 이벤트 2개) 를 cross-check 해서 freeze
  를 2초 윈도우로 잡았음.
- **page duration p95 가 freeze 의 선행지표일 가능성.** 마지막 2분에
  avg 36→62k, p95 130→184k 로 점프. throughput (분당 페이지수) 은
  떨어지지 않아서 단순 시스템 모니터링으론 못 잡음. **이벤트별 wall-clock
  분포** 를 봐야 보임.
- **/metrics 그래프의 default fill 차이**: AVG 시리즈는 None → gap,
  COUNT 시리즈는 0 → 평평한 라인. 둘 다 각자 합리적이지만 다운타임은
  시각적으로 헷갈리게 보임. AVG 시리즈의 gap 으로 다운타임 식별 가능.
- **AC 정전과 호스트 freeze 는 외관이 비슷함** (둘 다 갑자기 멈춤, 화면
  꺼짐). 구분 단서: 다른 같은 회로 장비 상태 / 전원 LED / `last reboot`
  타임스탬프. 사용자 관찰이 가설 좁히기에 결정적이었음.

## 곧 해야 할 작업 (이 인시던트 기반)

우선순위 순:

1. ~~하드웨어 watchdog 활성화~~ — **같은 세션에서 활성화 완료. 아래
   "후속" 섹션 참고.**
2. **부팅 알림** — boot 0 직후 어딘가 (Slack/이메일/PaperMeister 호스트)
   로 ping. 의도치 않은 재부팅 즉시 인지.
3. **metrics_collector 확장** — CPU 온도/throttle, GPU power draw, ECC
   카운터, PCIe link state 추가. 다음 freeze 의 진단 단서를 더 모음.
4. **page duration 알림** — 분 단위 p95 가 평소의 3배 초과 시 webhook.
   freeze 의 분 단위 선행 경보 가능성.
5. **NVIDIA 드라이버 변경 검토** — Open Kernel Module 595.71 →
   proprietary 로 되돌리는 옵션. Turing (RTX 8000) 은 proprietary 가 더
   안정적이라는 보고가 일반적. 다만 트리거를 확정 못한 단계에서 큰 변경은
   risk 가 있어 후순위.
6. **/api/metrics gap visualization** — `pages_per_step` 채울 때 같은
   bucket 의 host metric 이 None 이면 0 대신 None 으로 보내거나, 그래프
   측에서 gap 처리. 미니 작업.
7. **냉장고 별도 확인** — 같은 콘센트군이면 (사용자 보고로는 그렇다는데)
   별도 원인 있는지 점검. 다른 두 PC 가 살아있는 것과 모순되니 우연일
   가능성도 있음.

## 후속 — watchdog 활성화 (같은 세션)

위 "곧 해야 할 작업 #1" 을 인시던트 진단 직후 같은 세션에 처리. 호스트
프로필 + 활성화 절차 + 함정.

### 호스트가 가진 watchdog 옵션

- CPU: Intel Core i7-7820X (Skylake-X)
- Chipset: Intel **X299** PCH
- `/lib/modules/.../iTCO_wdt.ko.zst` 있음, `/lib/modules/.../wdat_wdt.ko.zst`
  있음, `/lib/modules/.../softdog.ko.zst` 있음
- `/dev/ipmi*` 없음 (워크스테이션 보드, BMC 없음 → IPMI watchdog 불가)
- `/sys/firmware/acpi/tables/WDAT` **있음** (308 bytes, INTEL SKL 레퍼런스)

### 함정 — iTCO_wdt 는 silently 무동작

처음엔 X299 PCH 의 표준 watchdog 이니 `sudo modprobe iTCO_wdt` 하면 끝일
줄 알았음:

```
$ lsmod | grep iTCO
iTCO_wdt           16384  0
intel_pmc_bxt      16384  1 iTCO_wdt   ← 의존성도 로드 OK

$ ls /sys/class/watchdog/
(empty)                                  ← 디바이스 등록 안 됨

$ ls /dev/watchdog*
ls: cannot access ...
```

모듈은 로드되는데 platform device 가 안 만들어짐. 진단 단서는
`/sys/firmware/acpi/tables/WDAT` 의 존재. **BIOS 가 ACPI WDAT 표준
인터페이스를 노출하면 ACPI 가 PCH 의 TCO 리소스를 claim 해버려서
iTCO_wdt 의 platform-driver 경로가 못 잡는다.** 이 경우 드라이버는
`wdat_wdt` 를 써야 함.

### 정답 — wdat_wdt 로 즉시 작동

```bash
sudo modprobe wdat_wdt
# → /dev/watchdog0 즉시 생성, /sys/class/watchdog/watchdog0/identity = "wdat_wdt"
```

디바이스 속성:
- `identity`: wdat_wdt
- `timeout`: **30** (초). read-only — `echo 60 > timeout` 거부됨. WDAT spec
  의 set-timeout 액션이 없으면 fixed.
- `state`: inactive (활성화 전)
- `bootstatus`: 0 (직전 부팅은 사용자 하드리셋, watchdog trip 아님)
- `nowayout`: 0 (디바이스 닫으면 watchdog 멈춤 — systemd 디버깅 친화)

### 영구 활성화 (적용 완료)

```bash
# 부팅 시 자동 로드
echo wdat_wdt | sudo tee /etc/modules-load.d/watchdog.conf

# systemd 가 ping 하도록
sudo mkdir -p /etc/systemd/system.conf.d
sudo tee /etc/systemd/system.conf.d/watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=10s
RebootWatchdogSec=10min
EOF

sudo systemctl daemon-reexec
```

`RuntimeWatchdogSec=10s` 는 timeout (30s) 의 1/3. ping 한 번 놓쳐도 두 번
더 기회. 시스템 freeze 시 ~30초 후 PCH reset.

### 적용 후 검증 (현재 상태)

```
WatchdogDevice=/dev/watchdog0
RuntimeWatchdogUSec=10s
WatchdogLastPingTimestamp=Tue 2026-05-26 02:42:52 UTC   ← live ping 중
RebootWatchdogUSec=10min

/sys/class/watchdog/watchdog0/state = active

lsof /dev/watchdog0:
COMMAND PID USER FD   TYPE DEVICE SIZE/OFF NODE NAME
systemd   1 root 13w   CHR  243,0      0t0 1887 /dev/watchdog0
```

PID 1 이 디바이스를 잡고 10초마다 ping. 영구 설정 둘 다 박혀있어서 다음
reboot 후에도 자동 활성.

### 효과

이번 인시던트 시나리오 (호스트 hard hang, 사용자 출근 전까지 다운) 의
**다운 시간 4일 → 30~60초로 단축**. 자동 재부팅 후 compose 가 unless-stopped
정책으로 컨테이너 자동 기동. 단, 024 의 stale network ID 함정이 다시
재현될 가능성은 별개로 남음 (이번 reboot 에선 발생 안 함).

### 다음 freeze 발생 시 forensic

- `cat /sys/class/watchdog/watchdog0/bootstatus` — 0 이 아니면 직전 부팅이
  watchdog 트립이었다는 직접 증거 (사용자 하드리셋과 구분 가능)
- `sudo dmesg | grep -iE "wdat|reboot.*reason"` — 트립 메시지 흔적
- `journalctl --list-boots` — 자동 재부팅 간격으로 freeze 빈도 추정

### 부수효과 / 주의

- 디버깅 중 `kill -STOP 1` 같은 PID 1 정지 시 시스템 자동 reboot. 평상시엔
  무관하지만 인지 필요.
- `nowayout=0` 이므로 의도적으로 watchdog 비활성화하려면 `/dev/watchdog0`
  을 여는 프로세스가 magic close (V 문자 write) 후 close 하면 됨.
  systemd 가 잡고 있는 동안엔 다른 프로세스는 디바이스 못 엶.
- BIOS 펌웨어 업데이트 시 ACPI WDAT 가 빠지거나 변경될 수 있음. 그땐
  부팅 후 `/dev/watchdog0` 확인 필요.

## 참고

- `pages.duration_ms` 분 단위 슬로다운 표 (위) — baseline 으로 보존.
- 4개 소스 cross-source 타임라인 표 (위) — 다음 freeze 진단 시 같은 방식
  적용.
- watchdog 영구 설정 위치: `/etc/modules-load.d/watchdog.conf`,
  `/etc/systemd/system.conf.d/watchdog.conf`
- 메모리: `reference_watchdog_setup.md` (추가됨).
