# P01 — BIOS F1 → 최신 업데이트 계획서

작성: 2026-07-30
대상: Gigabyte **X299 AORUS Gaming 3 Pro-CF** (rev. 1.0), 현재 **F1 / 2017-07-04**
상태: **계획 (미실행)**
선행 문서: [devlog 040](20260727_040_fatal_mce_ifu_parity_kdump_first_capture.md),
[devlog 041](20260729_041_mce_localized_to_core5_and_cpu_offlining.md)

> 이 문서는 **계획서**다. 실행하면 결과를 devlog 042 로 따로 남기고,
> 이 문서 §7 의 체크리스트에 통과/실패를 기입할 것.
> 파일명 규칙: `YYYYMMDD_P<NN>_<title>.md` = **계획서**,
> `YYYYMMDD_<NNN>_<title>.md` = **사후 기록**.

## 1. 왜 올리는가 — 목적은 하나뿐

**미래의 CPU 교체 경로를 확보하기 위해서다. 그 이상을 기대하지 말 것.**

현재 CPU(중고 i7-7820X)는 물리 코어 5 의 IFU 패리티 결함으로 죽어가는 중이고
(devlog 041), 코어 격리로 버티고 있다. 언젠가 교체해야 한다. 그런데:

> ⚠️ **CPU 가 완전히 죽은 뒤에는 BIOS 를 올릴 수 없다.**
> 신형 CPU 를 꽂아도 BIOS 가 인식 못 해 부팅이 안 되고, BIOS 를 올리려면
> 작동하는 구형 CPU 가 필요하다 — 방금 버린 그것이다. 이 보드에 CPU 없이
> 플래시하는 기능(Q-Flash Plus)이 있는지도 미확인이다.

즉 **지금 CPU 가 살아 있는 동안에만 할 수 있는 작업**이라서 지금 한다.

### 기대하지 않는 것 (중요)

| | 판단 |
|---|---|
| **MCE 치료** | ❌ 기대 안 함. 마이크로코드는 이미 OS 가 `0x2007006`(최신급)을 부팅 초기에 로드하므로 BIOS 것을 덮어쓴다. 남는 변수는 VRM/전압 테이블뿐이고, 에러가 코어 하나에 몰리는 것은 전압 부족보다 실리콘 열화 그림이다 (devlog 041 §4) |
| **성능 향상** | ❌ 없음 |
| **CPU 교체 경로 확보** | ✅ **이것이 유일한 목적** |
| DIMM_C0 인식 | 🎲 부수 기대. 메모리 트레이닝 개선으로 4번째 스틱이 잡힐 수 있음 (성능 이슈지 안정성 아님) |

## 2. 선행 조건 — 24h 판정 먼저

**2026-07-30 06:58 UTC (15:58 KST) 의 24시간 판정을 통과한 뒤에 실행한다.**

이유: BIOS 업데이트는 전압 테이블과 마이크로코드를 건드리므로, 판정 전에
올리면 이후의 안정성이 **"코어 격리 덕분인지 BIOS 덕분인지" 구분 불가**가
된다. 현재 17시간 깨끗한 데이터를 확보한 상태이므로 이를 오염시키지 말 것.

판정 명령:
```bash
uptime; ls -la /var/crash/; cat /sys/devices/system/cpu/online
docker inspect ocrserver-llm-1 --format '{{.RestartCount}} {{.State.StartedAt}}'
```

- [ ] 24h 판정 통과 확인 (결과 기입: ____________)

## 3. 위험 평가 — 생각보다 낮다

한때 "플래시 중 MCE 패닉 → 벽돌" 을 우려했는데, **이 시나리오는 사실상
성립하지 않는다**:

- **BIOS/Q-Flash 환경은 부트스트랩 프로세서(CPU 0) 하나로만 돌아간다.**
  나머지 코어는 halt 상태이므로 **결함 코어 5 가 명령어를 인출하지 않는다.**
- 재부팅하는 순간 컨테이너·vLLM 은 모두 내려가므로 별도로 세울 필요도 없다.
  (`sudo reboot` 이 graceful stop + DB WAL 정리까지 해준다.)

따라서 남는 위험은 **이 CPU 결함과 무관한 일반적인 것들**뿐이다:

| 위험 | 완화 |
|---|---|
| 플래시 중 정전 | UPS 연결 상태 확인 |
| 잘못된 BIOS 파일 | rev 1.0 / 모델명 정확히 대조 (§4) |
| BIOS 리셋으로 필수 설정 유실 | **§4 의 설정 사진 촬영이 이 대비다** |
| 플래시 실패로 부팅 불가 | Dual BIOS 여부 확인, 최악의 경우 §8 |

## 4. 사전 준비 체크리스트

### 4.1 현재 상태 스냅샷 (사후 비교용 — 반드시 먼저)

```bash
# 보드/BIOS 식별
cat /sys/class/dmi/id/board_name /sys/class/dmi/id/board_version \
    /sys/class/dmi/id/bios_version /sys/class/dmi/id/bios_date

# CPU / 마이크로코드
grep -m1 -E 'model name|microcode' /proc/cpuinfo
cat /sys/devices/system/cpu/online

# 메모리 구성 (DIMM_C0 미인식 여부)
sudo dmidecode -t memory | grep -E 'Locator|Size|Speed|Manufacturer|Part Number'

# 보호 장치 상태
cat /sys/class/watchdog/watchdog0/timeout
cat /sys/kernel/kexec_crash_loaded
systemctl is-enabled offline-bad-cores.service wdat-watchdog-load.service \
    ocrserver-gpu-power-limit.service ocrserver-metrics.timer

# GPU / NVLink 정상값 기준
nvidia-smi --query-gpu=index,name,power.limit --format=csv
nvidia-smi topo -m | head -5      # NV2 여야 정상
nvidia-smi nvlink -e | grep -c .  # 에러 카운터 0
```

- [ ] 위 출력 전부를 어딘가에 저장 (devlog 042 초안에 붙여둘 것)

### 4.2 ⭐ BIOS 설정 화면 사진 촬영

**BIOS 업데이트는 설정을 기본값으로 되돌린다.** 지금 잘 돌고 있는 구성을
복원하려면 현재 설정을 알아야 하고, OS 에서는 읽을 수 없다.

특히 놓치면 **GPU 두 장이 초기화 안 될 수 있는** 항목:

- [ ] **Above 4G Decoding** — 현재 값 기록. RTX 8000 48GB × 2 는 이게 없으면
      BAR 매핑이 안 될 수 있다. 기본값이 Disabled 인 보드가 많다
- [ ] **Re-Size BAR Support** (있으면) — 현재 값
- [ ] PCIe 슬롯 대역폭 설정 / Bifurcation
- [ ] XMP / 메모리 프로파일 — **현재 값 기록 후, 플래시 뒤에는 끌 것** (§6)
- [ ] CPU 전압·배수 관련 항목이 auto 인지 (수동 OC 흔적 없어야 정상)
- [ ] Boot 순서 / UEFI-CSM / Secure Boot
- [ ] Wake on LAN, Restore on AC Power Loss (정전 후 자동 복구 설정)

방법: 각 탭을 휴대폰으로 촬영. Q-Flash 진입 전에 전부 찍어둘 것.

### 4.3 준비물

- [ ] **BIOS 파일** — Gigabyte 지원 페이지에서 **X299 AORUS Gaming 3 Pro
      rev 1.0** 용 최신 BIOS 다운로드
      (https://www.gigabyte.com/Motherboard/X299-AORUS-Gaming-3-Pro-rev-10/support)
      - ⚠️ **`X299 AORUS Gaming 3` (Pro 없음) 는 다른 보드다.** 헷갈리지 말 것
      - `dmidecode`/`board_version` 으로 rev 확인
- [ ] **FAT32 포맷 USB** (다른 파일 없이 BIOS 파일만)
- [ ] 릴리즈 노트 확인 — 특히 **"support new Core X-series processors"** 계열
      항목이 포함된 버전 이상인지. 최신으로 가면 Cascade Lake-X(10000번대)까지
      커버되므로 §9 의 선택지가 열린다
- [ ] Dual BIOS / Q-Flash Plus 지원 여부 확인 (매뉴얼 또는 보드 실물)
- [ ] UPS 연결 확인

## 5. 실행 절차

```bash
# 1) 현 상태 최종 확인 (§2 판정 통과 여부)
uptime; ls -la /var/crash/

# 2) 재부팅 — 컨테이너 graceful stop + DB WAL 정리는 자동
sudo reboot

# 3) POST 중 Del 키로 BIOS 진입
# 4) (§4.2) 설정 화면 전부 촬영
# 5) Q-Flash 진입 → USB 의 BIOS 파일 선택 → 플래시
#    ※ 절대 전원 차단 금지. 완료 후 자동 재부팅
# 6) 재진입해서 §6 설정
```

**Windows 용 @BIOS 유틸리티는 쓰지 말 것** — Q-Flash(BIOS 내장)만 사용.

## 6. 플래시 직후 BIOS 설정

- [ ] **Load Optimized Defaults** 로 시작 (깨끗한 기준선)
- [ ] **Above 4G Decoding = Enabled** ← GPU 2장 필수 가능성
- [ ] §4.2 에서 찍은 사진 중 **필수 항목만** 복원 (Boot 순서, AC 복구 등)
- [ ] **XMP = Disabled**, 메모리는 auto/JEDEC — 3장 비대칭 구성이라 트레이닝이
      이미 불안정하다. 여기서 변수를 늘리지 말 것
- [ ] **CPU 전압/배수 전부 auto** — 수동 OC·오프셋 절대 금지.
      넣으면 MCE 원인 판정이 다시 흐려진다 (devlog 040 §3)
- [ ] 저장 후 부팅

## 7. 사후 검증 체크리스트 ⭐

**이 박스는 부팅 환경이 바뀔 때마다 뭔가 하나씩 조용히 죽어왔다**
(devlog 026 watchdog, 037 wdat_wdt denylist, 038 kdump 3-고리). 전수 확인할 것.

| # | 확인 | 통과 기준 | 결과 |
|---|---|---|---|
| 1 | `cat /sys/class/dmi/id/bios_version` | F1 이 아닌 새 버전 | |
| 2 | `cat /sys/devices/system/cpu/online` | `0-3,6-11,14-15` (코어 격리 유지) | |
| 3 | `systemctl is-active offline-bad-cores.service` | `active` | |
| 4 | `ls -l /dev/watchdog0` | 존재 (**037 함정**) | |
| 5 | `cat /sys/class/watchdog/watchdog0/timeout` | `300` | |
| 6 | `cat /sys/kernel/kexec_crash_loaded` | `1` (**038 함정**, 서비스 status 는 믿지 말 것) | |
| 7 | `grep -m1 microcode /proc/cpuinfo` | `0x2007006` 이상. 값 기록 | |
| 8 | `nvidia-smi` | GPU 2장 인식, 드라이버 정상 | |
| 9 | `nvidia-smi topo -m` | **NV2** (PCIe 폴백이면 브리지 재확인 — devlog 034/035) | |
| 10 | `nvidia-smi --query-gpu=power.limit --format=csv` | **230W** (025 의 systemd 서비스 재적용 확인) | |
| 11 | `docker ps` | wrapper / llmwrapper / nginx / llm 정상. chandra-a 는 의도적 정지 상태 | |
| 12 | `curl -s localhost:8080/health` 및 `/llm/health` | 200 | |
| 13 | `sudo dmidecode -t memory \| grep -c 'No Module'` | 1 이면 그대로, **0 이면 DIMM_C0 가 잡힌 것** (기록) | |
| 14 | `journalctl -b 0 -p err --no-pager \| head -30` | 새로운 에러 없음 | |
| 15 | `sqlite3 /var/lib/rasdaemon/ras-mc_event.db "select count(*) from mce_record;"` | 값 기록 (기준선) | |

추가 관찰 (수일):

- [ ] **MCE 재발 여부.** 뜨면 에러 CPU 번호를 확인 — **4/5/12/13 이 아니면
      다이 전체 열화 확정 → CPU 교체 즉시 착수** (devlog 041 §10)
- [ ] LLM HTTP 500 재발 여부 (`llmserver.db`)
- [ ] 부팅 시 `wdat_wdt is deny-listed` 경고 잔재 정리 여부 (미관 문제)

## 8. 실패 시 대응

| 증상 | 대응 |
|---|---|
| 플래시 후 부팅 불가 | Dual BIOS 있으면 자동 복구 대기. 없으면 CMOS 클리어(점퍼/배터리) 후 재시도 |
| POST 는 되는데 GPU 미인식 | **Above 4G Decoding** 부터 확인 (§6) |
| `/dev/watchdog0` 없음 | `systemctl status wdat-watchdog-load.service` — devlog 037 참조 |
| `kexec_crash_loaded=0` | `USE_KDUMP` / `hardlockup_panic` / watchdog timeout 3종 재확인 — devlog 038 |
| 메모리 트레이닝 실패로 부팅 지연/실패 | XMP 끈 상태인지 확인. 최악의 경우 DIMM 1장씩 줄여 부팅 |
| MCE 빈도가 오히려 악화 | BIOS 이전 버전으로 롤백 가능 여부 확인. 단 §1 대로 애초에 치료를 기대한 작업이 아님 |

## 9. 성공 후 열리는 선택지 — CPU 교체 후보

최신 BIOS 로 가면 Cascade Lake-X(10000번대)까지 지원되어, 9900X 보다 나은
선택지가 생긴다. **죽는 시점에 싼 쪽을 고르면 된다.**

| | i7-7820X (현재) | i9-9900X | **i9-10900X** |
|---|---|---|---|
| 코어/스레드 | 8C/16T | 10C/20T | 10C/20T |
| Base / Turbo | 3.6 / 4.3 | 3.5 / 4.4 | **3.7 / 4.5** |
| L3 | 11 MB | 19.25 MB | 19.25 MB |
| AVX-512 FMA | 1개 | 2개 | 2개 |
| 메모리 | DDR4-2666 | DDR4-2666 | **DDR4-2933** |
| PCIe 3.0 레인 | 28 | 44 | **48** |
| HW 취약점 완화 | 없음 | 없음 | **하드웨어 내장** |
| 출시 | 2017 Q2 | 2018 Q4 | **2019 Q4** |

- 멀티스레드는 7820X 대비 **+25~30%**, 싱글스레드는 사실상 동일 (같은 14nm
  Skylake-SP 코어). **성능 업그레이드가 아니라 결함 제거가 목적.**
- 현 워크로드는 12스레드로 load 2.0 이라 코어 수가 병목이 아니고, LLM 처리량은
  RTX 8000 메모리 대역폭 병목이다 (devlog 033) → **코어를 늘려도 tok/s 는 안 오른다.**
- 10900X 가 1년 늦게 팔린 물건이라 **중고 마모도 덜하다.**
- ⚠️ 어느 쪽이든 **또 중고**다. 지금 문제의 근원이 "중고 HEDT 의 알 수 없는
  이력" 이라는 점을 감수하는 선택이다. 신품 플랫폼(AM5 등)으로 넘어가는
  대안은 보드+DDR5 동반 교체가 필요하다.

## 10. 실행 요약 (한 장)

```
[선행] 24h 판정 통과 확인                              → §2
[준비] 상태 스냅샷 저장 + BIOS 설정 사진 + FAT32 USB   → §4
[실행] sudo reboot → Del → Q-Flash → 파일 선택         → §5
[설정] Optimized Defaults + Above 4G Decoding + XMP off → §6
[검증] 15개 항목 전수 확인                             → §7
[기록] devlog 042 작성, HANDOFF 갱신
```
