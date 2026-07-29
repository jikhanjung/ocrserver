# devlog 041 — MCE 12건 전수 분석: 범인은 코어 5 하나, 코어 격리 실험 시작

날짜: 2026-07-29
태그: 인시던트 조사 (**결함 위치를 물리 코어 단위로 국소화**), 완화책 적용
선행: [devlog 040](20260727_040_fatal_mce_ifu_parity_kdump_first_capture.md)
(MCE 원인 확정, 1건 기준)

## 요약

사용자가 "오늘은 꽤 오래 에러 없이 잘 돌고 있네" 라고 한 것에서 시작했는데,
**사실은 정반대였다.** 040 이후 이틀 동안 호스트가 **12번 더 죽었고**,
그중 마지막은 대화 시점 기준 2시간 전이었다.

kdump 가 남긴 **12건의 MCE 를 전수 디코드**한 결과 040 의 결론이 확정됐을
뿐 아니라 한 단계 더 좁혀졌다:

> **에러를 낸 CPU 가 사실상 하나다 — 물리 코어 5 (논리 CPU 5/13).**
> 12건 중 10건이 CPU 5, 2건이 CPU 4. 나머지 6개 코어는 단 한 번도 없다.

그래서 이번엔 BIOS 업데이트(040 의 1순위)보다 먼저, **공짜이고 되돌릴 수
있고 몇 초면 끝나는** 코어 격리를 실험으로 넣었다. 2026-07-29 06:58:33 UTC
부터 **코어 4·5 를 오프라인** 상태로 관찰 중이다.

## 1. 사용자 인식과 실제의 괴리

이게 이 건의 구조적 함정이라 먼저 적는다.

- watchdog(300s) 이 리셋하고 kdump 가 덤프를 뜨고 컨테이너가 자동 복귀하는
  전 과정이 **약 2분**이다.
- 클라이언트(PaperMeister) 입장에선 짧은 502 몇 개일 뿐이다.
- 즉 **호스트가 하루 6번 죽어도 서비스는 "잘 도는 것처럼 보인다".**

038 에서 "사용자가 프리즈 4회를 인지 못 하고 있었다" 고 적었던 그 구조가
그대로 유지되고 있다. **가동 상태 판단에 `docker ps` 나 체감을 쓰면 안 되고
`uptime` + `journalctl --list-boots` 를 봐야 한다.**

## 2. 040 이후 크래시 12회

`journalctl --list-boots` 에서 27초짜리 짧은 부팅 = 캡처 커널이므로,
그 앞뒤로 크래시 경계를 정확히 뜬다.

| # | 부팅 시작 (UTC) | 크래시 | 가동시간 | 에러 CPU | STATUS |
|---|---|---|---|---|---|
| 1 | 07-27 09:04:45 | 16:03:50 | 6h59m | **5** | b2…0005 |
| 2 | 07-27 16:05:17 | 17:48:29 | 1h43m | **5** | f2…0005 (OVER) |
| 3 | 07-27 17:50:00 | 18:12:54 | **23m** | **5** | b2…0005 |
| 4 | 07-27 18:15:05 | 19:44:51 | 1h30m | **5** | b2…0005 |
| 5 | 07-27 19:46:41 | 20:00:05 | **13m** | **5** | b2…0005 |
| 6 | 07-27 20:01:35 | 07-28 04:11:46 | 8h10m | **4** | b2…0005 (커널 idle) |
| 7 | 07-28 04:13:02 | 15:00:11 | 10h47m | ? | 덤프 로테이션으로 소실 |
| 8 | 07-28 15:01:39 | 19:00:26 | 3h59m | **5** | f2…0005 (OVER) |
| 9 | 07-28 19:02:33 | 07-29 01:59:09 | 6h57m | **5** | b2…0005 |
| 10 | 07-29 02:00:57 | 02:21:49 | **21m** | **5** | b2…0005 |
| 11 | 07-29 02:23:43 | 03:29:15 | 1h6m | **5** | f2…0005 (OVER) |
| 12 | 07-29 03:31:06 | 04:03:43 | 33m | **5** | b2…0005 |

- 총 경과 45.9h, 크래시 12회, 다운타임 약 2분×12 → **MTBF 3.8h**,
  **중앙값 가동 1h36m**.
- 7번은 `KDUMP_NUM_DUMPS=5` 로테이션에 밀려 vmcore·apport 둘 다 없다.
  다만 캡처 커널이 떴다는 것 자체가 panic 을 증명한다.
- 040 의 09:02 건까지 포함하면 **13건 중 12건에서 MCE 확인**.

### 시그니처는 12건 전부 동일

```
mce: [Hardware Error]: CPU 5: Machine Check Exception: 5 Bank 0: b200000000070005
mce: [Hardware Error]: Machine check: Processor context corrupt
Kernel panic - not syncing: Fatal machine check
```

`Bank 0` = IFU, `MCACOD 0x0005` = internal parity, `UC=1`, `PCC=1`,
`ADDRV=0`, `SOCKET 0`, `microcode 2007006`. 040 의 판독 그대로다.
상위 니블이 `f2` 인 3건은 **OVER 비트**가 서 있는 것 — 같은 에러가
연달아 여러 번 나서 오버플로했다는 뜻이라 더 나쁜 쪽이다.

## 3. ⭐ 새 발견 — 결함이 코어 단위로 국소화된다

이번 전수 분석의 핵심.

| 에러 CPU | 물리 코어 | APIC | 건수 |
|---|---|---|---|
| CPU 5 | **코어 5** | 0x0a | **10** |
| CPU 4 | 코어 4 | 0x08 | 2 |
| 그 외 6개 코어 | — | — | **0** |

`/proc/cpuinfo` 기준 논리 CPU → 물리 코어 매핑은 `CPU n → core n`
(n<8), HT 형제는 `CPU n+8`. 로그의 `[ C13 ]` 태그는 **에러를 기록한** CPU
(코어 5의 HT 형제)이고, `CPU 5:` 가 **에러를 낸** CPU 다.

8코어 중 두 개, 사실상 **한 개**에 몰려 있다. 무작위였다면 균등하게
흩어졌어야 한다. 040 에서 "코어 4·5 가 가장 따뜻하다" 고 관측했던 것과도
같은 곳을 가리킨다.

### 부수 발견 — 부하와 무관하다

6번(07-28 04:12)은 유일하게 유저스페이스가 아니다.

```
RIP !INEXACT! 10:<ffffffffacf22e1d> {intel_idle_ibrs+0x8d/0x170}
```

`CS=0x10` = 커널 모드, 그것도 **C-state 진입 경로**. 나머지 11건은 전부
`CS=0x33` 유저스페이스(vLLM). 즉 **바쁠 때만 죽는 게 아니라 놀 때도 죽는다.**
"LLM 부하가 원인" 이나 "발열" 로 설명하려는 시도를 여기서 접어야 한다.

### rasdaemon 은 이틀 내내 0건

```
sqlite3 /var/lib/rasdaemon/ras-mc_event.db "select count(*) from mce_record;"
0
```

정정 가능 에러(CE)가 **하나도 없다.** 서서히 나빠지는 게 아니라 곧바로
치명적 UC 로 간다. → **CE 축적을 기다리는 관측 전략은 무의미하다**
(040 의 "곧 해야 할 작업 2번" 은 이걸로 종결).

## 4. 040 의 "BIOS 먼저" 판단 수정

040 은 남은 분기를 "실리콘 열화(A)" vs "전압/설정 부족(B)" 로 두고 B 가
공짜니까 **BIOS 부터** 하자고 했다. 이번에 확인한 사실 두 가지가 그 우선
순위를 낮춘다.

1. **마이크로코드는 이미 최신급이다.** 로드된 리비전이 `0x2007006` 이고
   `intel-microcode 3.20260210.1ubuntu2` 가 설치돼 초기 부팅에 BIOS 것을
   덮어쓴다. BIOS F1 이 낡았어도 **마이크로코드 측면의 이득은 이미 받고
   있다.** BIOS 업데이트로 남는 이득은 VRM/전압 테이블·메모리 트레이닝뿐.
2. **에러가 코어 하나에 몰린다.** 전압 부족이면 여러 코어에서 고르게
   나야 자연스럽다. 한 코어만 계속 터지는 건 실리콘 쪽 그림이다.

거기에 BIOS 업데이트는 **중고 보드 + 운영 박스에서 부팅 불가 리스크**를
지는 작업이다. 리스크 있는 쪽을 먼저 할 이유가 없어졌다.

## 5. 적용 — 코어 4·5 오프라인

리스크 0, 비용 0, 즉시 되돌릴 수 있는 실험을 먼저 넣는다.

```bash
sudo chcpu -d 4,5,12,13     # 코어 4·5 + HT 형제
cat /sys/devices/system/cpu/online
# 0-3,6-11,14-15
```

```
Jul 29 06:58:33 jikhanserver kernel: smpboot: CPU 4 is now offline
Jul 29 06:58:33 jikhanserver kernel: smpboot: CPU 5 is now offline
Jul 29 06:58:33 jikhanserver kernel: smpboot: CPU 12 is now offline
Jul 29 06:58:33 jikhanserver kernel: smpboot: CPU 13 is now offline
```

16스레드 → 12스레드. 현재 load average 가 2.0 수준이라 실사용 영향 없음.

### ⚠️ 영속화가 실험 성립 조건

`chcpu -d` 는 **재부팅하면 날아간다.** 이 박스는 평균 3.8시간마다 리셋되므로
영속화 없이는 다음 크래시에서 코어 5 가 조용히 되살아나 **실험이 스스로
무효화된다.** 이 함정 때문에 systemd 유닛으로 고정했다.

`/etc/systemd/system/offline-bad-cores.service`:

```ini
[Unit]
Description=Offline degraded CPU cores 4,5 (+HT siblings 12,13) - IFU parity MCE
After=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/chcpu -d 4,5,12,13
ExecStop=/usr/bin/chcpu -e 4,5,12,13

[Install]
WantedBy=multi-user.target
```

`enable --now` 완료, `active (exited)` 확인. `chcpu -d` 는 **이미 오프라인인
CPU 에 대해 exit 0** 이라(`CPU 4 is already disabled`) 재실행에 안전하다.

되돌리기: `sudo systemctl disable --now offline-bad-cores.service`

> 커널 cmdline 으로는 "특정 인덱스 CPU 끄기" 를 표현할 방법이 없다
> (`maxcpus=` 는 개수, `nosmt` 는 HT 전체). BIOS 의 Active Processor Cores
> 도 위에서부터 잘라내는 방식이라 코어 5 만 빼는 게 안 된다. **systemd
> 유닛이 유일하게 정확한 수단.**

## 6. 판정 기준

지수분포 가정(MTBF 3.8h)에서, 변화가 없는데 우연히 버틸 확률:

| 무크래시 지속 | 우연일 확률 |
|---|---|
| 8h | 12% |
| **12h** | **4%** |
| **24h** | **0.1%** |

→ **12시간 무크래시면 유의미, 24시간이면 확정.**

관찰 시작: **2026-07-29 06:58:33 UTC** (15:58 KST).
현재 부팅 시작은 04:05:21 UTC 이므로, `uptime` 이 아니라 이 시각 기준으로
셀 것.

### 결과별 해석

| 결과 | 해석 | 다음 |
|---|---|---|
| 크래시 정지 | 코어 5 국소 결함 확정 | 12스레드로 안정 운영하며 CPU 교체 준비. BIOS 는 급하지 않음 |
| **다른 코어에서 재발** | 열화가 다이 전체로 번지는 중 | 격리는 미봉책 → **CPU 교체가 유일한 답** |
| 빈도만 감소 | 위와 동일 | 시간만 벌어둔 상태 |

CPU 4 가 이미 2건 나온 것은 **두 번째 시나리오의 약한 선행 신호**다.
코어 5 만 끄지 않고 4 까지 같이 끈 이유이기도 하다.

## 7. 부가 — apport `.crash` 에서 dmesg 뽑는 법

`/var/crash/*.crash` 는 root:root 0644 라 **sudo 없이 읽힌다**
(`dmesg.*` 는 0600 이라 sudo 필요). 안에 `VmCoreDmesg: base64` 필드가 있고,
**줄마다 독립 base64 블록**이며 디코드해서 이어붙이면 gzip 스트림이 된다.

```python
lines = open(path,'rb').read().split(b'\n')
i = lines.index(b'VmCoreDmesg: base64')
buf = b''.join(base64.b64decode(l.strip())
               for l in lines[i+1:] if l.startswith(b' '))
txt = gzip.decompress(buf).decode('utf8','replace')
```

이 경로 덕분에 로테이션으로 vmcore 가 지워진 뒤에도 **dmesg 는 8건 더
남아 있었고**, 그게 이번 전수 분석을 가능하게 했다. 040 이 1건으로 내린
결론을 12건으로 확인할 수 있었던 게 이 파일들 덕분이다.

## 8. 남은 것

- **12h / 24h 시점 판정** (위 6절).
- `/var/crash` 2.1GB, 루트 83% (56G 여유) — 상한 5개라 더 늘지는 않음.
- BIOS 업데이트는 **관찰 결과 이후로 연기**. 실행하더라도 stock/auto 유지.
- CPU 교체 검토(LGA2066)는 유효. 두 번째 시나리오면 즉시 착수.
