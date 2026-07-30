# HANDOFF — 2026-07-30 (코어 5 격리로 프리즈 + 세그폴트 둘 다 멈춤)

> **🟢 이 박스가 현재 상태의 전부다.**
> 07-27 이후 이틀간 호스트가 **12번 더 죽었고**(MTBF 3.8h), kdump 12건 전수
> 디코드 결과 전부 동일한 **Fatal MCE — Bank 0(IFU), internal parity,
> PCC=1, ADDRV=0** 이며 **에러 CPU 가 10/12 건 CPU 5** — 결함이 **물리 코어
> 5 하나**에 몰려 있다. → **2026-07-29 06:58:33 UTC 부터 코어 4·5 오프라인**
> (`offline-bad-cores.service`, 16→12스레드). devlog 041.
>
> **✅ 17h 시점 결과 (07-30 00:05 확인): 두 증상이 동시에 멈췄다.**
> - 호스트 MCE 패닉 **0건** (기대 4.5건, 우연일 확률 1.1%).
>   uptime 20h = MCE 시대 최장 (이전 최장 10h47m).
> - LLM HTTP 500 **0건 / 1,441 요청** (기대 6.5건, 0.15%). 엔진 연속 가동
>   19h43m, `RestartCount` 안 늘어남. 마지막 500 은 격리 2.5h 전 04:22:50.
> - 부하는 동일 (요청 ~100/h, 생성 ~81k tok/h, GPU1 95~100%).
> → **040 의 "하나의 열화 CPU 가 프리즈와 세그폴트를 모두 낸다" 가 실험으로
> 확인됐고, 위치가 코어 5 로 특정됐다.** 039 의 AWQ/Marlin 가설 완전 종결.
>
> **⏳ 남은 판정: 24h = 2026-07-30 06:58 UTC (15:58 KST).**
> 이후 유일한 관찰 포인트는 **격리한 4/5/12/13 이 아닌 CPU 번호로 MCE 가
> 뜨는지** — 뜨면 다이 전체 열화 → **CPU 교체 즉시 착수**. CPU 4 에서 이미
> 2건 나왔으므로 교체 계획 자체는 유효하다. 지금 번 것은 **시간**이다.
>
> **BIOS 업데이트는 후순위** — 마이크로코드는 이미 OS 가 `0x2007006` 로드
> 중이고, 한 코어 편중은 전압 문제보다 실리콘 열화 그림. 중고 보드 벽돌
> 리스크를 먼저 질 이유가 없다. `--quantization awq` 는 **취소** — 하지 말 것.
>
> ⚠️ **서비스가 멀쩡해 보이는 것에 속지 말 것.** 복구가 2분이라 클라이언트
> 엔 짧은 502 로만 보인다. 판단은 `uptime` + `journalctl --list-boots`.
>
> ℹ️ `chandra-a` 는 **07-29 08:10 에 의도적 정지** (GPU 0 을 규조류 검출
> 작업에 사용 중). OCR 재개 시 `docker compose up -d chandra-a`.

이 파일은 작업 인수인계용. 작업 단위로 갱신.

## 방금 한 작업 ② (2026-07-30 00:05 — 격리 17h 결과 확인, devlog 041 §8)

**실험 성공.** 세 갈래가 같은 방향을 가리킨다.

| 지표 | 기대값 | 관측 | 우연일 확률 |
|---|---|---|---|
| 호스트 MCE 패닉 | 4.5건 | **0** | 1.1% |
| LLM HTTP 500 | 6.5건 | **0** | 0.15% |
| 엔진 재시작 | ~9회 | **0** | — |

- **호스트**: uptime **20h00m** = MCE 시대 최장(이전 10h47m). `/var/crash` 에
  새 디렉터리 0건. 격리 이후 순수 무크래시 **17h07m**.
- **⭐ 세그폴트도 같이 멈췄다** (설계 시 기대하지 않은 결과, MCE 보다 강한 증거):

  | | 기간 | 요청 | 에러 | HTTP 500 |
  |---|---|---|---|---|
  | 격리 전 | 07-27 07:48 ~ 07-29 06:58 (47.2h) | 5,321 | 160 | **24** |
  | 격리 후 | 07-29 06:58 ~ 07-30 00:05 (17.1h) | 1,441 | **0** | **0** |

  마지막 500 이 **07-29 04:22:50**, 7초 뒤 `llm` 재시작(`04:22:57`) — 격리보다
  2.5h 앞선다. 이후 `RestartCount=1` 그대로, **엔진 연속 가동 19h43m**.
  (에러 160건 = 500 24건 + `http=0` 136건. 후자는 재시작 60~90초 창의 연결
  실패로 보이며 1:5.7 비율이 그 해석과 맞음. 판정은 500 숫자만 사용.)
- **부하 교란 없음**: 11h~23h 매시간 요청 ~100건 / 생성 ~81,000토큰,
  GPU1 95~100% / 228W, load 2.0~2.1 — 크래시 구간과 동일.
  ⚠️ 단 **07:00~10:45 의 3h47m 은 부하 공백**(PaperMeister 재시작 전)이라
  증거에서 할인해야 함. 위 확률은 그걸 반영 안 한 낙관값.
- **의미**: 040 의 통합 가설("하나의 열화 CPU 가 두 증상 모두")이 **코어 5 를
  떼니 둘이 동시에 멈추는** 것으로 입증됨. 039 의 AWQ/Marlin 가설 완전 종결.
- **증명하지 않는 것**: "다이 전체로 번지는가". CPU 4 에서 2건 나왔으므로
  CPU 교체 계획은 유효. 얻은 것은 **준비할 시간**.

## 방금 한 작업 (2026-07-29 — MCE 12건 전수 분석 + 코어 격리, devlog 041)

세부 `devlog/20260729_041_mce_localized_to_core5_and_cpu_offlining.md`.

- **발단**: 사용자가 "오늘은 오래 잘 돌고 있네" 라고 했는데 실제 uptime 은
  2시간이었고 마지막 크래시가 **2시간 전**이었다. 038 에서 겪은
  "watchdog 이 사고를 은폐한다" 구조가 그대로 반복.
- **040 이후 크래시 12회** (07-27 09:04 ~ 07-29 04:03).
  **MTBF 3.8h / 중앙값 1h36m**, 최단 13분. 12건 중 11건 MCE 확인
  (07-28 15:00 건은 `KDUMP_NUM_DUMPS=5` 로테이션으로 소실).
- **시그니처 12건 전부 동일**: `Bank 0: b200000000070005`
  (3건은 `f2…` = **OVER 비트**, 같은 에러 연속 발생 오버플로).
- **⭐ 코어 국소화**: 에러 CPU 가 **CPU 5 ×10 / CPU 4 ×2 / 나머지 0**.
  `/proc/cpuinfo` 로 `CPU n → 물리 코어 n`, HT 형제는 `n+8` 확인.
  로그의 `[ C13 ]` 은 **기록한** CPU, `CPU 5:` 가 **에러 낸** CPU.
  040 의 "코어 4·5 가 가장 따뜻" 관측과 같은 곳.
- **부하 무관 입증**: 07-28 04:12 건만 커널 모드(`CS=0x10`)이고 RIP 가
  `intel_idle_ibrs+0x8d` — **C-state 진입 중에도 죽는다.** 나머지 11건은
  `CS=0x33` 유저스페이스(vLLM). "LLM 부하/발열 탓" 가설 폐기.
- **rasdaemon 이틀간 `mce_record` 0건** — CE 가 아예 없고 곧바로 치명적
  UC 로 간다. → **CE 축적 관측 전략은 무의미** (040 의 백로그 2번 종결).
- **적용**: `sudo chcpu -d 4,5,12,13` → `0-3,6-11,14-15` (16→12스레드,
  load 2.0 이라 영향 없음). 06:58:33 UTC 커널 로그로 확인.
- **영속화가 실험 성립 조건** — `chcpu` 는 재부팅에 안 남는데 이 박스는
  3.8h 마다 리셋되므로, 놔두면 다음 크래시에 코어가 되살아나 실험이 스스로
  무효화된다. `/etc/systemd/system/offline-bad-cores.service` (oneshot,
  `RemainAfterExit`, `ExecStop` 으로 복구) enable 완료.
  `chcpu -d` 는 이미 오프라인이면 **exit 0** 이라 재실행 안전.
  커널 cmdline·BIOS 로는 "특정 인덱스만 끄기" 표현 불가 → 유닛이 유일 수단.
- **판정표** (지수분포, MTBF 3.8h 기준):
  8h 무크래시=우연 12% / **12h=4%** / **24h=0.1%**.
  기준 시각은 `uptime` 이 아니라 **06:58:33 UTC**.
- **부가 — apport `.crash` 에서 dmesg 뽑는 법 확립**: `/var/crash/*.crash`
  는 **0644 라 sudo 없이 읽힌다**(`dmesg.*` 는 0600). `VmCoreDmesg: base64`
  는 **줄마다 독립 base64 블록** → 디코드해 이어붙이면 gzip.
  vmcore 가 로테이션돼도 dmesg 는 남아서, 이번 전수 분석이 이걸로 가능했다.

## 이전 작업 (2026-07-27 — 프리즈 4회 발견 + forensic 인프라 구축)

상태 점검 중 **2026-07-26 하루에 호스트가 4번 freeze** 했고 watchdog 이
매번 자동 리셋한 것을 발견. 사용자는 인지 못 한 상태였음 (70~100초 만에
복구돼서 밖에서 보면 무사고). devlog 026 금요일 프리즈와 동일 시그니처.
세부 `devlog/20260727_038_silent_freezes_and_kdump_enablement.md`.

- **프리즈 시각 (UTC)**: 04:17:29 / 04:42:27 / 06:26:53 / 19:26:30.
  부팅 간격 100s / 70s / 86s / 79s = watchdog 트립 + POST 패턴.
  직전 3일(07-23 18:37 ~ 07-26 04:17)은 안정이었음.
- **죽을 때 로그**: 4번 다 1분 주기 `ocrserver-metrics.service` 의
  "Finished" 한 줄 뒤로 **아무것도 없음**. shutdown 없음, panic/oops 없음,
  **Xid/NVRM 없음**, MCE 없음, OOM 없음. `metrics.db` 도 마지막 샘플까지
  평탄 (load ~2.0, mem 10GB, gpu0 0%/41°C, gpu1 100%/78°C). 선행 신호 0.
- **동시간대 vLLM EngineCore 크래시 9회** (03:15~20:05). 컨테이너
  RestartCount=2, restart 정책으로 자력 복구. 프리즈와 **인과 미확정** —
  크래시→프리즈 3건, 크래시만 3건, 프리즈만 1건으로 선후 불일정.
  → 이 건은 같은 날 **devlog 039** 로 따로 파고들었음 (아래 참조).
- **핵심 발견 — vmcore 체인이 3군데 다 끊겨 있었음** (그래서 026 이후
  지금까지 원인 규명이 계속 막혔던 것):
  1. `USE_KDUMP=0` (Ubuntu 기본값) → `kexec_crash_loaded=0`.
     `kdump-tools.service` 는 `active (exited)` 로 **멀쩡해 보이지만**
     `/etc/init.d/kdump-tools` 가 `[ "$USE_KDUMP" -ne 0 ] || exit 0` 로
     빠져나가고 있었음. **서비스 status 로 판단하면 속는다.**
  2. `hardlockup_panic=0` → 조용한 프리즈가 panic 으로 전환 안 됨 →
     kdump 트리거 자체가 발생 안 함. 우리 증상에 가장 결정적.
  3. HW watchdog `timeout=10s` < 하드락업 판정 시간(~20s) → 1·2 를 고쳐도
     판정 전에 리셋되는 **구조적 불가능** 상태. `max_timeout=613` 확인.
- **적용 완료 + 검증**: `USE_KDUMP=1` + `KDUMP_NUM_DUMPS=5`,
  `/etc/sysctl.d/60-lockup-panic.conf` (`hardlockup_panic=1`,
  `panic_on_oops=1`, `panic=60`), watchdog `RuntimeWatchdogSec=10s→180s`.
  → `kexec_crash_loaded=1`, `timeout=180`, `kdump-config status` =
  **ready to kdump**. 컨테이너 5개 무영향.
  `softlockup_panic` 은 **의도적 제외** (GPU 24/7 부하 중 오탐 재부팅 위험).
- **커널 범프 내성 확인**: 037 의 wdat_wdt 함정이 kdump 엔 해당 없음.
  `kdump-config load` 가 매 부팅 `manage_symlinks` 로 `KVER=$(uname -r)`
  기준 심볼릭 링크를 재생성 → 커널 올라가도 자동 추종.
- **메모리 정정 2건** (기존 기록이 틀렸음):
  - watchdog timeout "30초 read-only, 변경 불가" → **오류**. 실제 10초였고
    `max_timeout=613`, `RuntimeWatchdogSec` 으로 변경 가능 (180s 검증 완료).
  - `bootstatus != 0 = 트립 흔적` → **이 보드에선 성립 안 함**. 리셋 4회
    후에도 계속 `0`. `0` 보고 "트립 없었다" 결론내면 정반대로 틀림.
    대신 `journalctl --list-boots` + 각 부팅 마지막 줄을 쓸 것.
- **한계**: CPU 하드락업이면 잡히지만 PCIe fatal / CPU 완전 정지면 NMI 가
  안 떠서 vmcore 없이 리셋됨. 07-26 프리즈가 어느 쪽인지는 **아직 미상**
  (양쪽 다 무로그). 다음 프리즈 후 `/var/crash` 가 비었으면 후자로 판정하고
  netconsole / serial console 로 넘어갈 것.
- **✅ end-to-end 검증 통과** (05:04, OCR/LLM 유휴 시점에 sysrq 강제 panic):
  - `/var/crash/202607270505/` 에 **vmcore 449M** + dmesg 158K 확보.
    `file(1)` 이 "Flattened kdump compressed dump v6" 로 정상 인식.
  - 백트레이스 그대로: `write_sysrq_trigger → __handle_sysrq →
    sysrq_handle_crash → panic → vpanic`
  - 타임라인: panic 05:04:03 → 캡처커널 부팅완료 05:05:14(**71초**) →
    덤프완료 05:05:43(**29초**) → 정상부팅 05:06:34. **총 2분 31초.**
  - 재부팅 후 kdump 자동 재무장, 컨테이너 5개 자동 복귀 확인.
- **⚠️ 검증에서 드러난 마진 부족 → watchdog 180s→300s 재상향 (적용 완료)**:
  panic~덤프완료가 **100초**인데 (캡처 커널 단일 CPU 부팅에만 71초),
  systemd 는 timeout/2 주기로 ping 하므로 프리즈 시 잔여가 90~180초
  균등분포 → **약 11% 확률로 덤프가 잘림**. 300s 로 올려 잔여 150~300초
  확보. 검증: `/sys/class/watchdog/watchdog0/timeout` = **300**.
  교훈: **캡처 커널 부팅 시간이 덤프 시간보다 길다** — 타임아웃 산정 시
  덤프 시간만 계산하면 부족.
- **⚠️ 미해결 — 커널 디버그 심볼 없음**: `crash` 는 설치돼 있으나
  **ddebs 에 7.0.0-28 이 없음** (보유: -14 / -15 / -22 / -26 뿐, apt 로 재확인).
  ddebs 저장소는 등록해 둠 (`/etc/apt/sources.list.d/ddebs.sources` +
  `ubuntu-dbgsym-keyring`) — -28 이 올라오거나 다음 커널 범프 때 자동 인식.
  **단 이게 프리즈 대기를 막지는 않음**: 커널이 kallsyms 로 백트레이스를
  이미 심볼화해서 dmesg 에 찍으므로(위 sysrq 결과가 증거) 원인 규명에는
  대개 충분하고, vmcore 는 나중에 심볼 구해서 분석 가능.
- makedumpfile 이 커널 7.0.0 미지원 경고 출력
  (`The kernel version is not supported`). 덤프는 유효하게 나왔으나
  `-d 31` 페이지 필터링이 최적 아닐 수 있음.

## 이전 작업 ② (2026-07-27 — EngineCore 세그폴트 추적, devlog 039)

038 의 "EngineCore 크래시 9회" 를 마저 판 것. 세부
`devlog/20260727_039_enginecore_segfault_memory_corruption.md`.

- **먼저 038 오진 정정**: "크래시 로그가 logrotate 로 유실" 은 **틀렸음**.
  로그는 단일 `json.log` 37.6MB 에 06-24~07-27 **195,954줄 전부 온전**.
  로테이션 설정 자체가 없었음. 진짜 원인은 **`docker logs` CLI 가 같은
  파일에서 매번 다른 부분만 반환**한 것 (전체=06-24~06-25 /
  `--tail 100000`=07-11~07-23 / `--since` 07-26=0줄, 0.08초 즉시 반환).
  **`docker logs` 를 로그 유무 판정에 쓰지 말 것.** 원본 직접 읽기:
  ```bash
  CID=$(docker inspect ocrserver-llm-1 --format '{{.Id}}')
  docker run --rm -v /var/lib/docker/containers:/c:ro nginx:alpine \
    cat /c/$CID/$CID-json.log > llm_raw.jsonl
  ```
- **확인된 사실**: EngineCore 는 **SIGSEGV** 로 죽고 있음
  (`!!!!!!! Segfault encountered !!!!!!!`). 33일치 로그에서 **10건**.
- **결정적 소견 — 메모리 손상 시그니처**: 10건의 충돌 지점이 **전부 다름**.
  참조카운트(`Py_DECREF`/`subtype_dealloc`), 모듈 임포트
  (`import_ensure_initialized`), **에러 문자열 포맷**(`_PyErr_FormatV`),
  torch 디스패치(`c10::Dispatcher::callBoxed`), 텐서 해제
  (`THPVariable_clear`) 등 서로 무관한 곳. `Py_INCREF`/`Py_TYPE` 반복 등장
  = 포인터 역참조 중 사망. **코드 버그면 같은 자리에서 반복해 죽는다** —
  사방에서 죽는 건 메모리 손상 패턴.
- **두 번째 실패 유형**: 07-26 04:13:31 은 세그폴트가 아니라 C++ 예외 —
  `c10::Error: self->cdata.use_count() == 1 INTERNAL ASSERT FAILED at
  python_variable.cpp:3623` (`THPVariable_clear`). 이것도 refcount 무결성
  실패라 같은 영역을 가리킴.
- **입력과 무관**: 500 유발 요청 크기(1,893~4,351자)가 정상 분포
  (p50 2,783 / p90 4,693 / max 10,601) 안에 완전히 들어감. 확률적 발생.
- **모델 교체와 강한 상관** (같은 GPU 에서의 자연 실험):
  | 기간 | 모델 | 요청 | 500 | 비율 |
  |---|---|---|---|---|
  | ~06-24 | Qwen3-14B fp16 | 8,901 | 1 | 1/8,901 |
  | 06-24~ | Qwen3-32B-AWQ | 44,449 | 39 | 1/1,140 |

  **약 8배 상승.** 첫 세그폴트 06-25 16:10, 모델 교체 06-24. 이 기간
  GPU·이미지(05-08 생성분 불변)·워크로드 모두 동일.
- **Chandra 비교는 무효**: OCR 이 06-09 부터 7주째 유휴라 chandra 는 일을
  안 하고 있음(비교군 불가). 게다가 **chandra 도 vLLM**(0.21.0). 현 구성상
  "vLLM vs Chandra" 와 "GPU1 vs GPU0" 가 완전히 교락돼 관측만으론 분리 불가.
- **관측 사각지대 2곳 확인**:
  - **호스트 RAM non-ECC 확정** — `dmidecode`: `Error Correction Type: None`.
    Samsung M378A4G43MB1-CTD 32GB ×3 (A0/B0/D0, C채널 공석 = 비대칭 구성).
    EDAC 미노출 → 비트 플립이 어디에도 안 찍힘.
  - **GPU ECC 두 장 다 Disabled** → ECC 카운터 전부 `[N/A]`.
  - Xid 0건, NVLink 에러 0, CUDA 에러/OOM 없음.
- **⭐ 작업량 정규화 후 결론이 바뀜 (하드웨어 불변 전제)**: 보드/CPU 교체는
  프로젝트 시작 전이었고 OS 도 그때 새로 설치(05-11) → **관측된 모든 사건이
  동일 하드웨어 위**. 메모리도 첫 부팅부터 `3/8 slots`, 29회 부팅 내내 동일.
  하드웨어가 불변이면 하드웨어는 **변화(급증)** 를 설명 못 함. 남은 설명인
  "더 많이 돌려서 더 많이 죽었다"(노출량)도 아래처럼 부정됨:

  | | 요청 | 생성토큰 | 엔진가동 | 크래시 | 토큰/크래시 | 가동h/크래시 |
  |---|---|---|---|---|---|---|
  | 14B fp16 | 8,473 | 309만 | 46.8h | 1 | 3,096,837 | 46.8h |
  | 32B-AWQ | 33,907 | 2,064만 | 265.2h | 39 | **529,373** | **6.8h** |

  토큰 6.7배·시간 5.7배 늘었는데 크래시는 39배 → **정규화 후에도 약 6배**.
- **호스트 RAM 가설 반증**: `metrics.db` 기준 평균 호스트 RAM 이
  14B **16,110MB** → 32B-AWQ **10,320MB** 로 **감소**. "메모리를 많이
  건드릴수록 불량 셀을 밟는다" 는 논리의 예측과 정반대 → 세그폴트의
  주범은 호스트 RAM 이 아님.
- **결론: 문제가 둘이다. 분리 추적할 것.**

  | | 호스트 프리즈 | EngineCore 세그폴트 |
  |---|---|---|
  | 시기 | 05-22(14B), 07-26×4(32B) — 두 시대 모두 | 사실상 32B 시대만 |
  | 모델 상관 | 없음 | 강함 (정규화 후 6배) |
  | 주 용의자 | 하드웨어 기반 (RAM/PSU/보드) | **AWQ/Marlin 커널** |
  | 문서 | devlog 038 (kdump 로 vmcore 대기) | devlog 039 |
- **DIMM 4장 중 1장 미인식** (별건): `CPU1_DIMM_C0` 가 `No Module Installed`.
  **첫 부팅부터** 그랬고 29회 내내 동일 → 최근 고장 아님. 미인식 스틱은
  주소공간에 없어서 그 자체로 손상을 일으키진 않음. **BIOS 가 F1/2017-07-04
  출시 초기 펌웨어**라 메모리 트레이닝 미숙 가능성 있음(스틱 불량이 아닐 수도).
  이 박스엔 접촉불량 전례 있음(034/035 NVLink 브리지) → 재장착 먼저 시도.

> ⚠️ **이 절(②)의 결론은 devlog 040 에서 철회됨.** "AWQ/Marlin 커널 유력"
> 도, "프리즈와 세그폴트는 별개" 도 무효. 아래 ③ 을 볼 것.

## 이전 작업 ③ (2026-07-27 오전 — 첫 vmcore 확보, 원인 CPU MCE 확정, devlog 040)

**kdump 가 설치 당일 첫 실전 프리즈를 캡처했다.** 038 에서 고쳐 둔 3-고리가
실제로 작동했다.

```
boot -2  07-27 05:06:34 → 09:02:42   ← 5번째 프리즈 (가동 3h56m)
boot -1  07-27 09:03:28 → 09:03:55   ← 캡처 커널 (27초)
boot  0  07-27 09:04:45 →            ← 정상 복귀   (총 다운타임 2분 3초)

/var/crash/202607270903/dump.202607270903   427MB   ← 첫 실물, 보존할 것
```

- **사인**: `Kernel panic - not syncing: Fatal machine check`.
  `MCi_STATUS=0xb200000000070005` → **UC=1, PCC=1(Processor Context Corrupt),
  ADDRV=0, MCACOD=0x0005(internal parity), Bank 0 = IFU(명령어 인출 유닛)**.
  CPU 4, SOCKET 0, `RIP CS=0x33`(유저스페이스 실행 중).
- **`ADDRV=0` = 연관 메모리 주소 없음 → DRAM 에러가 아니다.**
  EDAC 도 전 기간 무에러. **DIMM 가설은 배제.**
- **과열 아님**: 전 부팅 통틀어 스로틀/`temperature above threshold` 0건,
  Package 47C, max MHz 4500 = i7-7820X 정격(오버클럭 흔적 없음).
  단 VRM/보드 온도는 `gigabyte-wmi: No temperature sensors usable` 라 관측 불가.
- **하드웨어 실체**: CPU 가 Xeon 이 아니라 **중고 i7-7820X (소비자용 HEDT)**,
  보드 X299 AORUS Gaming 3 Pro, **BIOS F1 / 2017-07-04 = 출시 당일 펌웨어**.

**그리고 같은 날 오전 09:50:48 · 09:57:38 에 세그폴트 2회**(사용자가 502 로
인지, 도커가 자동 재시작). 이때 호스트는 멀쩡. 요청 수로 정규화하니:

| 구간 | 요청 | 세그폴트 | /1k req |
|---|---|---|---|
| 06-25 ~ 06-28 | 16,464 | 3 | 0.182 |
| **07-23 ~ 07-24** | **12,346** | **0** | **0.000** |
| 07-25 ~ 07-27 | 5,412 | 9 | **1.663 (9.1배)** |

**07-23 은 전 기록 중 가장 바쁜 날(10,217건)인데 크래시 0건.** 그런데 기동
설정은 40여 회 재시작 내내 완전히 동일하다 —
`vLLM 0.20.2 / Qwen3-32B-AWQ / quantization=awq_marlin / MarlinLinearKernel`.
**정적 소프트웨어 버그는 스스로 9배 나빠지지 않는다.**

→ **039 철회 2건**: ① AWQ/Marlin 커널 가설 (설정 불변인데 2차 점프가 있었고,
MCE 도 설명 못 함) ② 프리즈/세그폴트 분리 (하나의 열화 CPU 가 둘 다 설명).
039 의 "32B 전환 후 6배" 는 `--enforce-eager` × 레이어수 40→64 로 **호스트
CPU 명령어량이 급증**한 것으로도 똑같이 설명된다.

- **rasdaemon 도입** (09:16, `active`/`enabled`). CE(정정 가능 MCE) 상시 기록
  시작. **첫 50분간 `mce_record` 0건** — 관측 기간이 짧아 해석 보류.
  `ras-mc-ctl --errors` 는 `signal_event` 스키마 불일치로 죽으니
  `sqlite3 /var/lib/rasdaemon/ras-mc_event.db "select * from mce_record ..."` 로 볼 것.

## 이전 작업 (2026-07-23 — 드라이버 버전 불일치 복구 + 재발 방지)

호스트 리부팅(~09:24) 직후 `unattended-upgrades` 가 NVIDIA 드라이버를
`595.71.05 → 595.84` 로 자동 업그레이드 → 로드된 커널 모듈(구)과 userspace(신)
**Driver/library version mismatch** → GPU 컨테이너(chandra-a, llm) 전면 다운.
`unattended_upgrades_docker` gotcha 의 드라이버 변종. 세부
`devlog/20260723_037_nvidia_driver_mismatch_unattended_upgrade.md`.

- **복구**: `sudo reboot` 로 로드 모듈을 595.84 로 정렬 → compose restart 정책
  으로 전 컨테이너 복귀. **검증 완료** (리부팅 후 uptime 몇 분 시점):
  - `nvidia-smi` 595.84, NVML 595.84 일치. 두 GPU 정상, 두 `VLLM::EngineCore`
    프로세스(chandra-a GPU0, llm GPU1) 기동.
  - 컨테이너 전부 Up. cold start: llm ~1.5분, chandra-a ~6분 후 둘 다 healthy.
  - 운영 경로(nginx:8080) `/health` 200, `/llm/health` 200,
    `/api/services._meta` mode=`llm+ocr`, llm_model=`Qwen/Qwen3-32B-AWQ`.
- **재발 방지 (적용 완료)**: `/etc/apt/apt.conf.d/52-nvidia-blacklist` 신설 —
  `unattended-upgrades` 가 `nvidia-` / `libnvidia` / `linux-firmware-nvidia`
  자동 업그레이드 못 하도록 blacklist. `apt-config dump` 로 병합 확인.
  단 blacklist 는 **자동** 업그레이드만 막음 — 수동 `apt upgrade` 는 여전히
  드라이버를 올리므로 드라이버 갱신은 계획된 리부팅과 함께만.
  주의: 패턴 `nvidia-` 가 `nvidia-container-toolkit` 도 함께 고정.
- **2차 회귀 — 하드웨어 watchdog (같은 리부팅)**: 같은 리부팅이 커널을
  7.0.0-27 → **7.0.0-28** 로도 범프 → 커널이 배포하는 per-kernel denylist
  (`/usr/lib/modprobe.d/blacklist_linux_7.0.0-28-generic.conf` 의
  `blacklist wdat_wdt`)를 systemd-modules-load 가 존중 → `/dev/watchdog0`
  미생성, devlog 026 watchdog 사망. **수정 완료**: `modules-load.d` 대신
  oneshot 서비스(`/etc/systemd/system/wdat-watchdog-load.service`)가
  `wdat_wdt` 를 **by-name modprobe**(blacklist 우회)로 강제로드. enable 완료,
  state=active/bootstatus=0/WatchdogDevice=/dev/watchdog0 검증. 커널 범프
  내성 확보. 메모리 `reference_watchdog_setup.md` 갱신.

## 이전 작업 (2026-06-24 오후 — NVLink/27B/LLMx2)

NVLink 복구(브리지 재안착) 확인 → 034 에서 보류했던 Qwen3.5-27B-dense 를
TP=2 로 재실측 → 그래도 미채택 → 부산물로 /status 에 LLMx2 모드 추가.
세부 `devlog/20260624_035_*` (NVLink 복구), `036_*` (27B 재실측 + LLMx2).

- **NVLink 복구**: 전원 off → 브리지 재장착 → on. `topo -m` NODE→**NV2**,
  4서브링크 25.781 GB/s, `nvlink -e`=0, dmesg sublink Error 소멸. 근본 원인
  브리지 접촉 불량 확정. 정상값 기준: `topo -m`=NV2, `nvlink -e`=0.
- **27B dense TP=2 재실측**: OOM 은 풀림(각 GPU 25.68 GiB). 하지만 aggregate
  **12.85 tok/s** (현 32B-AWQ 20.2 보다 ~37% 느림) + GPU 2장 점유(OCR 중단)
  + 콜드스타트 ~13분. GDN/mamba 하이브리드+멀티모달이라 무거움. **미채택**,
  034 판단 실측 재확인. → 32B-AWQ 단일 GPU 가 여전히 최적.
- **/status LLMx2 모드** (wrapper **0.2.2 → 0.2.3**): LLM 이 두 GPU(TP=2)를
  점유하는 형상을 표시. compose llm 의 `--tensor-parallel-size`/`device_ids`
  개수로 GPU 수 파싱(`_parse_llm_gpus`) → `_mode_from_probes` 가 n==0 &
  llm_ok & gpus>=2 면 `llmx2`. status.html 칩 "LLM×2 (2 GPU)".
  `_meta.llm_gpus` 노출. 현 compose 는 단일 GPU 라 칩 표시는 향후 TP=2 LLM
  배포 시 자동. wrapper+llmwrapper 둘 다 0.2.3 recreate.

## 이전 작업 (2026-06-24 오전 — LLM 모델 업그레이드)

`llm` 서비스 모델을 **Qwen3-14B(fp16) → Qwen3-32B-AWQ(int4)** 로 교체. OCR
은 안 건드림 (듀얼 분할 그대로). 이미지/wrapper 변경 없음 — compose 의 `llm`
command 모델 인자만 변경.

- **선정 근거 (실측 A/B)**: RTX 8000(Turing sm_75) 단일 GPU 에서 3개 후보를
  vLLM 0.20.2 같은 이미지로 실측:
  - **Qwen3.5-35B-A3B-GPTQ-Int4 (MoE)**: ✅ 기동되나 콜드스타트 ~11분
    (멀티모달 비전타워 warmup + GDN/FlashInfer JIT), A/B 에서 6건 중 1건
    HTTP 500, 처리량 ~15 tok/s. 탈락.
  - **Qwen3.5-27B-GPTQ-Int4 (dense)**: ❌ 로드 중 CUDA OOM. int4 로 안 덮인
    부분(비전타워+GDN)이 fp16 으로 올라가 ~47GB 초과, 단일 48GB 에 안 들어감.
    `--max-num-seqs 2` + expandable_segments 도 동일 OOM. TP=2 면 들어가지만
    두 GPU 점유 → OCR 전면 중단이라 부적합.
  - **Qwen3-32B-AWQ (표준 트랜스포머)**: ✅ 채택. 로드 ~74초, 6/6 성공,
    한국어 요약/메타추출 품질 우수.
- **속도 핵심 발견**: `llmserver.db` 실제 메타추출 프롬프트 6건으로 동일
  하네스 측정 → **14B-fp16 18.5 tok/s vs 32B-AWQ 20.2 tok/s**. 32B 가 오히려
  ~9% 빠름. 디코딩은 메모리 대역폭 병목이라 int4(토큰당 ~18GB 읽기) 가
  fp16 14B(~28GB) 보다 적게 읽어서. 즉 **속도 손해 없는 업그레이드**.
  (DB 과거 14B 실측 18.5 tok/s 와 통제 벤치 18.5 일치 → 방법론 검증됨.)
- **품질**: 14B/32B/35B 모두 메타추출은 정확(한국어 문서 포함). 32B 는 DOI
  접두사 정리 등 미세 개선. 실제 워크로드(서지 메타추출) 는 14B 로도 이미
  충분했고, 32B 의 진짜 이점은 어려운 케이스/요약/챗 헤드룸.
- **배포 절차**: dev tree `docker-compose.yml` 의 llm command 모델 인자만
  수정 → `/srv/ocrserver/` 로 cp → `docker compose --project-directory
  /srv/ocrserver --profile llm up -d --force-recreate llm`. served-model-name
  은 `qwen` 유지 → 클라이언트(PaperMeister 등) 코드 무변경.
- **검증**: `/llm/health` 200(46초), 운영 경로(nginx→llmwrapper→vllm) 로
  `model:"qwen"` 샘플 추론 정상, llmwrapper 가 DB 기록 정상(qwen/ok/2111ms).
- **/status 페이지 동적화 (wrapper 0.2.1 → 0.2.2)**: status.html 에
  `Qwen3-14B` 가 하드코딩돼 있어 stale. 매번 안 고치도록 동적화 — main.py
  의 compose 파서(`_refresh_compose_cache`)가 `llm` 서비스 command 의 첫
  non-flag 토큰을 모델명으로 뽑아 `/api/services._meta.llm_model` 로 노출,
  status.html 이 그걸로 `#llmModelShort/#llmModelFull` 채움. 앞으로 모델
  교체 시 compose 만 바꾸면 페이지가 자동 반영. wrapper + llmwrapper 둘 다
  0.2.2 로 recreate (같은 이미지 공유, 드리프트 방지).
- **모델 캐시**: `Qwen3-32B-AWQ` 는 `/srv/ocrserver/hf_cache` 에 받아둠.
  테스트로 받은 `Qwen3.5-35B-A3B-GPTQ-Int4`(~18GB) / 일부 27B 메타데이터도
  캐시에 남아있음 — 디스크 정리 시 후보.
- **✅ 해결 — NVLink 복구** (devlog 034 진단 → 035 복구): 전원 off →
  NVLink 브리지 재장착 → 전원 on (034 의 1순위 권고). `topo -m` 이
  NODE(PCIe) → **NV2**(bonded 2×NVLink) 로 복구, 4서브링크 모두
  25.781 GB/s, `nvlink -e` 에러 카운터 0, dmesg sublink Error 소멸. 근본
  원인 = 브리지 접촉 불량 확정. 이제 **TP=2 큰 모델 재시도 가능** (034 에서
  PCIe 폴백 때문에 보류했던 27B dense / MoE 등). 정상값 기준: `topo -m`=NV2,
  `nvlink -e`=0.

## 이전 작업 (2026-06-09)

LLM (`/llm/*`) 트래픽을 기록할 수 있게 wrapper 코드베이스에 LLM proxy
모드를 합치고, OCR 결과에 빈 페이지 있을 때 client 가 강제로 다시 OCR
시킬 수 있게 `POST /ocr` 에 `force` 옵션 추가. 세부
`devlog/20260609_032_llm_proxy_and_force_flag.md`.

- **구조**: 같은 이미지 (`honestjung/ocrwrapper:0.2.1`) 를 두 컨테이너로
  띄움. `wrapper` (`WRAPPER_ROLE=ocr`, 기본) 는 기존 OCR + 대시보드 +
  `/api/llm/*` (조회용, LLM DB RO). `llmwrapper` (`WRAPPER_ROLE=llm`,
  compose profile `[llm]`) 는 `/v1/*` proxy + 기록 전담. event loop /
  DB 분리로 OCR 큐가 LLM streaming proxy 의 어떤 stall 에도 영향받지 않게.
- **DB**: `data/llmserver.db` (WAL) 신규. llmwrapper RW, wrapper RO.
  `llm_requests` 테이블 — submitted/completed/model/endpoint/client_ip/
  request_json/response_text/prompt_tokens/completion_tokens/total_tokens/
  latency_ms/http_status/status/error/streamed. 본문은 65KB 로 truncate.
- **라우팅**: 외부 URL 그대로 (`/llm/v1/chat/completions`). nginx `/llm/`
  upstream 만 `llm:8000` → `llmwrapper:8000`. SSE 위해 `proxy_buffering
  off` + `proxy_request_buffering off`. `/llm/health` 만 별도 location
  으로 vllm 직결 (`/status` 가 실제 백엔드 health 보려면 필요).
- **SSE streaming**: chunk 통과 + 누적 패턴. `aiter_lines()` 로 라인 단위
  forward, `data: {...}` 의 `delta.content` 누적, vllm 마지막-1 chunk 의
  `usage` 객체 캡처. 종료 시 `asyncio.create_task(db_llm_insert(...))` —
  finally 에서 직접 await 하면 client_abort 시 GeneratorExit race.
  `client_abort` 도 status 로 기록.
- **force flag**: `POST /ocr` 의 `force: bool = Form(False)`. True 면
  `db_find_existing_by_hash` + `db_find_done_by_filename` 둘 다 skip,
  새 `job_id` 로 전체 OCR. 기존 row 보존, 다음 dedup 에선 새 잡이
  최신이라 우선. 응답에 `forced: true` 포함. 빈 페이지 판정 (whitespace-
  only 포함) 은 client 책임.
- **검증**:
  - non-streaming chat: status=ok, 15/50/65 토큰, 2813ms
  - streaming 정상: status=ok, 9/10/19 토큰, usage chunk 캡처
  - streaming 중단: status=client_abort
  - `/api/llm/stats?range=24h` 통계 정상, `/status` 의 LLM 카드 24h row +
    최근 요청 리스트 노출
  - POST /ocr 같은 파일 → cached:true / force=true → 새 job_id

- **배포**: wrapper image `0.1.14` → `0.2.1`. 큐 비어 있는 상태에서
  recreate, in-flight 잡 영향 0. nginx 는 single-file bind mount inode
  함정 회피 위해 `--force-recreate`.

## 이전 작업 (2026-05-28 밤)

큰 PDF (Stewart 1773p) 제출 시 대시보드가 4분 이상 먹통이던 문제 해결.
세부 `devlog/20260528_031_per_page_render_in_worker.md`.

- **원인**: `_run()` 이 PDF 전체 페이지를 upfront 로 PyMuPDF render +
  base64 list 에 쌓아두고 OCR 시작. 1773p = 단일 thread to_thread 에서 ~9분
  GIL convoy → 이벤트 루프 starve.
- **수정**: `_render_pdf` 제거, `_pdf_page_count` (페이지 수만) +
  `_render_one_page` (단일 페이지) 로 분리. `_ocr_page` 가 자기 페이지를
  `_sem` 슬롯 안에서 직접 render → chandra POST. render 동시성이 OCR
  세마포어 (12) 와 자연 일치.
- **왜 GIL convoy 안 일어나나**: chandra 25s/page, render 0.3s/page → 어느
  순간이든 12 슬롯 중 0-1개만 render 중, 나머지는 chandra await. 동시 render
  스레드 ≈ 1.
- **부가 fix**: `_render_sem` 삭제 (불필요), POST 핸들러의 sha256 +
  fitz.open + 디스크 write 도 `asyncio.to_thread` 화 (POST 동안에도 대시
  보드 응답).
- **배포**: `0.1.13` → `0.1.14`. Stewart 진행 중이었는데 lifespan resume
  으로 45/1773 부터 이어 처리. 다운타임 중 chandra 응답 4개 잡혀서 49
  로 올라옴.

## 이전 작업 (2026-05-28 저녁)

`db_find_done_by_hash` 가 `status='done'` 만 매칭해서, 같은 파일을 처리
중인 동안 재제출하면 **중복 잡** 이 만들어지던 문제. 오늘 Stewart 가
processing 중일 때 PaperMeister 가 재제출 → 두 번째 Stewart 잡이 생겨서
서버 hang 트리거된 사건의 latent 원인.

- 함수 rename: `db_find_done_by_hash` → `db_find_existing_by_hash`
- SQL: `status IN ('done','processing','queued')`, ORDER `(status='done') DESC, submitted_at DESC` (done 우선)
- 응답에 `in_progress: bool` 필드 추가 — 클라이언트가 "그냥 polling 하자" 결정 가능
- failed 잡은 여전히 dedup 안 됨 (재시도 가능 유지)
- client_id scoping 그대로 (다른 client 끼리는 dedup 안 일어남)
- 배포: `0.1.12` → `0.1.13`. 신규 잡 0건 상태에서 무손실 재기동.

## 이전 작업 (2026-05-28 오후)

PaperMeister client 가 큐 깊이 과소계상 (큰 책을 1페이지로 카운트) 으로
12개 큰 PDF 를 동시 제출 → 서버 wrapper recreate 시 lifespan resume 가
12개 모두 동시 render → GIL convoy → wrapper unresponsive 인시던트.
세부 `devlog/20260528_029_total_pages_hint_and_render_sem.md`.

- **변경 (a)**: POST `/ocr` 에 `total_pages: int | None = Form(None)` hint
  파라미터 추가. 주어지면 sync 파싱 스킵, `_jobs`/`db` 에 초기값으로 박음.
  `_run()` 이 `_render_pdf` 결과로 어차피 덮어쓰니까 거짓 hint 자동 보정.
  응답에도 `total_pages` 포함 (클라이언트 첫 poll 불필요).
- **변경 (b, 긴급 추가)**: `_render_sem = asyncio.Semaphore(2)` 도입.
  `_run()` 의 `await asyncio.to_thread(_render_pdf, ...)` 를 감쌈. PyMuPDF
  를 N 스레드 동시 호출 시 GIL convoy 로 이벤트 루프 starve 됨 — 12잡 동시
  resume 이 직접 hang 일으킴. semaphore=2 로 막아도 12잡 동시 resume 자체는
  trigger 가능, 후속 작업 필요 (per-page render).
- **데이터 복구**: Stewart Antarctica (1343/1773 done) 만 'processing'
  유지, 나머지 11잡 (모두 0/N) DB 에서 'failed' 마킹. host python 은
  root-owned DB write 불가 → 1회용 컨테이너로 SQL UPDATE.
- **배포**: `0.1.10` → `0.1.11` (hint) → `0.1.12` (render_sem 긴급 추가).
- **PaperMeister 쪽 fix**: 별도 (다른 리포). `wrapper_submit()` 이 로컬에서
  PyMuPDF 로 페이지 수 미리 계산해서 큐 깊이 계산에 사용 + 서버에 hint
  로 전송. 12잡 동시 제출 재발 방지.

## 이전 작업 (2026-05-28 오전)

PaperMeister 가 60s 타임아웃으로 POST /ocr 이 5건 연속 실패한 인시던트
진단 + 수정. 세부 `devlog/20260528_028_render_in_thread_event_loop_freeze.md`.

- **원인**: `wrapper/main.py:_run()` 이 `async def` 인데 안에서 PDF 전체
  페이지를 동기 루프로 래스터화 (`fitz.get_pixmap` + `tobytes("jpeg")` +
  `base64.encode`). 220p + 518p 큰 잡 두 개가 같은 시간 접수되면서 200+초
  동안 이벤트 루프 freeze → 그 윈도우에 들어온 POST 들이 응답 못 받음,
  nginx 는 client (PaperMeister 60s timeout) 끊긴 후 **499** 로 기록.
- **수정**: 헬퍼 `_render_pdf()` 로 sync 부분 분리, `asyncio.to_thread()`
  로 호출. PyMuPDF C 코드는 GIL 풀어주므로 진짜 병렬 render 가능.
- **부수 발견**: 어제 (devlog 027) 의 nginx 변경 (`/api/` 의 `error_page`
  제거) 이 **실제 nginx 컨테이너에는 적용 안 돼 있었음**. Docker
  single-file bind mount 는 컨테이너 시작 시점의 호스트 inode 를 잡고,
  `cp` 로 호스트 파일 교체 시 새 inode 가 되면 컨테이너는 옛 파일을 계속
  참조. `nginx -s reload` 로도 안 됨. `docker compose up -d --no-deps
  --force-recreate nginx` 로 컨테이너 재기동해야 새 inode 잡힘.
  → `mode-*.sh` 가 reload 로 끝나고 있는 것도 같은 버그에 취약. 별도 fix
  필요 (아래 곧 해야 할 작업 #8).
- **배포**: wrapper `0.1.9` → `0.1.10`, nginx 컨테이너 재기동, lifespan
  resume 으로 in-flight 2 잡 (canadiannaturali 498p + Valent 9p) 이어
  처리.
- **검증**: 배포 후 throughput 30 ppm, /api/stats 5 trials 35-104ms 안정.

## 이전 작업 (2026-05-27)

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
- nginx 모드: **OCR 1 GPU + LLM** (`nginx.llm.conf` 활성, mode chip `llm+ocr`)
- 운영 디렉터리: `/srv/ocrserver/` (jikhanjung 소유, sudo 없이 docker
  compose 가능)
- 운영 컴포즈는 prebuilt image 만 참조

### CPU 상태 (2026-07-30 갱신)
- **코어 4·5 오프라인** — 온라인 CPU `0-3,6-11,14-15` (16→12스레드).
  `offline-bad-cores.service` (enabled, `active (exited)`) 가 부팅마다 적용.
  **관찰 시작 2026-07-29 06:58:33 UTC → 17h07m 무크래시 (12h 문턱 통과).**
  남은 판정 24h = 07-30 06:58 UTC.
  해제: `sudo systemctl disable --now offline-bad-cores.service`
  ⚠️ 해제하면 프리즈와 세그폴트가 **둘 다** 돌아온다.
- CPU 는 중고 **i7-7820X**, BIOS **F1 / 2017-07-04** (미업데이트, 후순위).
  마이크로코드는 OS 가 `0x2007006` 로드 중 (`intel-microcode` 패키지).

### 컨테이너 / 이미지 (운영서버) — 2026-07-27 10:05 확인 (이후 재부팅 12회)
```
SERVICE      IMAGE                         STATUS
chandra-a    honestjung/ocrserver:0.1.1    Up 1h (healthy, GPU 0)
chandra-b    honestjung/ocrserver:0.1.1    Exited (0) 7주 전 (profile=ocr, 비활성)
nginx        nginx:alpine                  Up 1h (nginx.llm.conf)
wrapper      honestjung/ocrwrapper:0.2.3   Up 1h (WRAPPER_ROLE=ocr, OCR_CONCURRENCY=6)
llmwrapper   honestjung/ocrwrapper:0.2.3   Up 1h (WRAPPER_ROLE=llm)
llm          vllm/vllm-openai:latest       Up 8분 (healthy, GPU 1, Qwen3-32B-AWQ)
```
현재 부팅은 **2026-07-27 09:04:45 UTC** 시작 (09:02:42 프리즈 → kdump 캡처 →
자동 복구). `llm` 은 09:57:39 재시작 상태 (**RestartCount=2**, 09:50·09:57
세그폴트 연속 2회). 재시작 후 200 OK 정상 서빙 중, 생성 ~23 tok/s.

### 워크로드 현황 (2026-07-27)
- **OCR: 7주째 유휴.** 마지막 잡 **2026-06-09 07:00**. 큐 0/0.
  누적 7,767건 (done 7,720 / done_with_errors 12 / failed 35).
  chandra-a 는 GPU0 메모리만 점유, util 0% / 38°C.
- **LLM: PaperMeister 재개돼 가동 중** (07-27 하루 803건까지 집계).
  GPU1 util 100% / 77°C / 226W / 44.5GB.
- **CPU: Package 47°C** (스로틀 이력 없음). 코어 4·5 가 가장 따뜻.

### RAS / 크래시 포렌식 (2026-07-29 갱신)
- **`/var/crash` 2.1GB** — vmcore 5개(`KDUMP_NUM_DUMPS=5` 상한이라 더 안 늘음)
  + apport `.crash` 8개. 040 의 첫 vmcore `202607270903` 은 **로테이션으로
  이미 밀려났다** (dmesg 는 `.crash` 쪽에 남아 있음).
  현재 보유 vmcore: `202607281901` / `202607290200` / `202607290222` /
  `202607290330` / `202607290404`.
- **⭐ `.crash` 는 0644 라 sudo 없이 읽힌다** (`dmesg.*` 는 0600).
  `VmCoreDmesg: base64` 는 **줄마다 독립 base64 블록** → 줄별 디코드 후
  이어붙여 gunzip. vmcore 가 지워져도 dmesg 는 이쪽에 남는다.
- **rasdaemon**: `active`/`enabled` (07-27 09:16~).
  DB `/var/lib/rasdaemon/ras-mc_event.db` (world-readable).
  **07-29 기준 이틀 관측에도 `mce_record` 0건** — CE 없이 곧바로 치명적
  UC 로 감. CE 축적을 기다리는 전략은 폐기.
  ⚠️ `ras-mc-ctl --errors` 는 `signal_event` 스키마 불일치로 죽음 —
  sqlite3 직접 조회할 것.
- **crash 유틸 설치됨**, **dbgsym 은 미확보** (ddebs 에 7.0.0-28 미발행).

### DB
- `data/ocrserver.db` — OCR 잡/페이지. wrapper RW. ~1.5GB (jobs 7700+).
- `data/metrics.db` — host metrics. metrics_collector 가 RW, wrapper RO.
- `data/llmserver.db` — **신규**. llmwrapper RW, wrapper RO. WAL.

### 호스트 보호 / 메트릭
- **하드웨어 watchdog: 활성** — `wdat_wdt`, `/dev/watchdog0`.
  **timeout 300s** (2026-07-27 에 10s → 180s → 300s. kdump 덤프 시간 확보용,
  sysrq 검증에서 100초 필요 확인 후 재상향). systemd 가 절반 주기로 ping.
  - ⚠️ **`bootstatus` 는 이 보드에서 신뢰 불가** — 07-26 리셋 4회 후에도
    계속 `0`. 트립 판정은 `journalctl --list-boots` + 각 부팅 마지막 줄로.
  - **2026-07-23 변경**: 로드 방식이 `/etc/modules-load.d/watchdog.conf`
    → oneshot `wdat-watchdog-load.service` (by-name modprobe, 커널
    denylist 우회). 커널 범프 시 재발 방지. 상세는 devlog 037.
- **kdump: 활성 + 실동작 검증 완료** (2026-07-27 신규, devlog 038).
  `USE_KDUMP=1`, `hardlockup_panic=1`, watchdog 300s 세 개가 **세트로**
  있어야 동작. 덤프 위치 `/var/crash` (`KDUMP_NUM_DUMPS=5`).
  검증: `cat /sys/kernel/kexec_crash_loaded` → **1** 이어야 함
  (`systemctl status kdump-tools` 는 꺼져 있어도 정상처럼 보이니 쓰지 말 것).
  - 실측 소요: panic → vmcore 저장완료 **100초**, 총 다운타임 2분 31초.
    덤프 크기 449M (호스트 RAM 사용 10GB 기준).
  - sysrq 테스트 덤프는 검증 후 삭제함. **`/var/crash` 는 현재 비어 있음**
    (`kdump_lock` / `kexec_cmd` 는 kdump-tools 운영 파일이라 삭제 대상 아님).
    → 앞으로 여기 타임스탬프 디렉터리가 생기면 **실제 프리즈**의 vmcore.
  - 커널 디버그 심볼(dbgsym)은 **아직 없음** — ddebs 에 7.0.0-28 미발행.
    ddebs 저장소는 등록해 둠. 심볼 없이도 dmesg 백트레이스는 심볼화돼 읽힘.
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

devlog 041 로 우선순위를 다시 조정했다. **문제는 하나 — 열화된 CPU 코어.**
지금은 "코어 5 국소 결함인가" vs "다이 전체로 번지는가" 의 구분 단계이고,
격리 실험이 그 답을 공짜로 준다. **BIOS 는 그 뒤로 밀었다.**

1. **코어 격리 24h 판정 (코드 작업 없음)** — **12h 는 통과했다** (§방금 한
   작업 ②). 남은 것은 **2026-07-30 06:58 UTC (15:58 KST)** 의 24h 지점.
   ```bash
   uptime; journalctl --list-boots --no-pager | tail -5
   ls -la /var/crash/; cat /sys/devices/system/cpu/online
   docker inspect ocrserver-llm-1 --format '{{.RestartCount}} {{.State.StartedAt}}'
   ```
   - 유지되면 devlog 041 §8 에 확정 한 줄 추가하고 이 항목 종결.
   - 새 `/var/crash/2026*` 디렉터리가 생기면 즉시 에러 CPU 확인:
     **4/5/12/13 이 아닌 CPU 면 → 다이 전체 열화 → 2번으로 직행.**
   - `.crash` 가 아직 없으면 `sudo grep -h 'Hardware Error' /var/crash/2026*/dmesg.*`
2. **CPU 교체** — 격리가 안 먹거나 다른 코어에서 재발하면 **유일한 답.**
   LGA2066 소켓. 중고 아닌 물건이거나, 이 워크로드(24/7 vLLM)에 맞는
   다른 구성으로 갈지 판단.
3. **⭐ BIOS F1 → 최신 업데이트 — 계획서 작성 완료, 사용자가 실행 예정**
   → **`devlog/20260730_P01_bios_f1_to_latest_plan.md`** (준비물·절차·검증 15항목).
   - **목적은 오직 "미래의 CPU 교체 경로 확보"** — 죽은 CPU 로는 BIOS 를
     못 올리고, 신형 CPU 를 꽂아도 구 BIOS 가 인식 못 해 부팅 불가가 된다.
     **MCE 치료는 기대하지 않는다** (마이크로코드는 이미 OS 가 `0x2007006`
     로드 중, 한 코어 편중은 전압보다 실리콘 열화 그림).
   - **선행 조건: 24h 판정 통과 후에 실행** — 전에 하면 코어 격리 실험
     결과가 오염된다.
   - 플래시 자체 위험은 낮다: **Q-Flash 는 CPU 0 단독 실행이라 결함 코어 5 가
     명령어를 인출하지 않는다.** 컨테이너도 재부팅으로 자동 정지.
   - ⚠️ 놓치기 쉬운 것: BIOS 리셋으로 **Above 4G Decoding** 이 꺼지면 GPU
     2장 초기화 실패 가능 → **진입 전 설정 화면 전부 촬영**.
   - 사후 검증은 P01 §7 (037 watchdog / 038 kdump 함정 포함 15항목).
   - 성공 시 Cascade Lake-X 까지 열려 **i9-10900X** 가 9900X 보다 나은
     후보가 된다 (P01 §9).
4. **완화책 (원인 규명 아님, 급하면)** — `--enforce-eager` 를 빼거나
   14B fp16 으로 롤백하면 호스트 CPU 명령어량이 줄어 크래시 빈도는
   내려갈 것으로 예상. **결함 CPU 를 덮는 것뿐이라는 점을 명확히 할 것.**

### 종결된 항목 (devlog 041)

- ~~**CE 스트림 관측**~~ → **종결.** rasdaemon 이틀 관측에도 `mce_record`
  0건. CE 없이 곧바로 UC 로 가는 유형이라 기다려도 안 쌓인다.
- ~~**2회차 vmcore 로 Bank 0 IFU 확인**~~ → **완료.** 12건 전수가 동일
  시그니처. 재검토 조건 없음.

### 취소된 항목 (devlog 040 §6)

- ~~**Marlin 커널 배제 테스트** (`--quantization awq`)~~ → **취소.**
  설정이 40여 회 재시작 내내 불변인데 크래시율이 07-25 부터 9배로 뛰었다.
  정적 커널 버그로는 설명 불가. 실행하지 말 것.
- ~~**14B fp16 롤백 (판별 목적)**~~ → 위 5번으로 강등 (완화책일 뿐).
- ~~**GPU 스왑** (`device_ids` 1→0)~~ → 취소. MCE 는 CPU 소켓 0 사건이고
  GPU 와 무관.
- ~~**DIMM 재장착 / 슬롯 교체**~~ → 취소. `ADDRV=0` 으로 DRAM 배제.
  단 `CPU1_DIMM_C0` 미인식 자체는 BIOS 업데이트 후 재확인 가치 있음
  (메모리 트레이닝 개선으로 4장 다 잡힐 수 있음 — 성능 이슈지 안정성 아님).

### 그 외 (우선순위 낮음)

6. **dbgsym 확보 대기** — ddebs 에 7.0.0-28 미발행 (저장소 등록은 완료).
   `apt-cache policy linux-image-$(uname -r)-dbgsym` 로 주기 확인.
   이번 건은 MCE 라 dmesg 만으로 결론이 났으나, **다음 건이 커널 Oops 면
   심볼이 필요**하다. `crash` 유틸은 설치돼 있음.
7. **호스트 메모리 테스트** — 우선순위 대폭 하향 (DRAM 배제됨).
   그래도 완전 무죄는 아니므로 케이스 열 일 있으면 memtest86+ 수 시간.
8. **GPU ECC 활성화 검토** — 가용 VRAM 1~2% 감소로 현재 44.5GB 쓰는 모델이
   빠듯해질 수 있어 켠 뒤 로딩 확인 필요. GPU 리셋(리부팅) 동반.

### C. 기타

11. **docker 로그 크기 제한은 보류** — `daemon.json` 에 `max-size` 가 없어
    무한 증가하는 건 맞지만(llm 37.6MB/33일), 지금 켜면 **오래된 크래시
    로그가 실제로 지워짐**. 조사 끝난 뒤에 설정할 것.
5. 잔재 정리 (미관/혼동 방지): 부팅 시 `wdat_wdt is deny-listed` 경고
   (037 에서 대체된 옛 modules-load.d 잔재), `wdat-watchdog-load.service`
   의 `Documentation=` 이 URL 이 아니라 부팅마다 `Invalid URL` 3줄.
6. **디스크**: 루트 83% 사용 (271G/344G, 59G 여유). `hf_cache` 에 테스트로
   받은 Qwen3.5-35B-A3B(~18GB)/27B 메타데이터가 정리 후보 (devlog 033).

기존 백로그는 [TODOs.md](TODOs.md) (mode 스크립트의 nginx reload,
lifespan resume sync read, 잡 fair 스케줄링, fitz.open 중복 등).

## 참고 위치

- 데브로그: `devlog/20260520_013_*.md` ~
  `20260729_041_mce_localized_to_core5_and_cpu_offlining.md`
  - 파일명 규칙: `YYYYMMDD_<NNN>_*.md` = **사후 기록**,
    `YYYYMMDD_P<NN>_*.md` = **계획서** (2026-07-30 신설).
    현재 계획서: `20260730_P01_bios_f1_to_latest_plan.md`
- 메모리(자동 컨텍스트): `~/.claude/projects/-home-jikhanjung-projects-ocrserver/memory/`
  - `reference_watchdog_setup.md` (2026-07-27 정정: timeout 180s 변경 가능,
    bootstatus 신뢰 불가)
  - `reference_kdump_setup.md` (신규 — kdump 3-고리 구조 + 한계)
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
  # ⚠️ bootstatus 는 이 보드에서 신뢰 불가 (리셋 후에도 계속 0). 아래를 쓸 것:
  uptime; journalctl --list-boots --no-pager | tail -10
  # 크래시 원인 (sudo 없이): /var/crash/*.crash 의 VmCoreDmesg 디코드
  ls -la /var/crash/
  # CPU 격리 상태
  cat /sys/devices/system/cpu/online   # 정상값 0-3,6-11,14-15
  ```

---

_세션 전반 (프리즈 → kdump) — 상태 점검 중 07-26 호스트 프리즈 4회 발견(watchdog 이 매번 자동 복구해서 은폐돼 있었음). 죽을 때 로그는 4번 다 무증상(panic/Xid/MCE 전무). 원인 규명이 막힌 근본 이유가 vmcore 체인 3군데 단절(`USE_KDUMP=0` + `hardlockup_panic=0` + watchdog 10s < 락업 판정 20s)임을 확인하고 셋 다 수정. **sysrq 강제 panic 으로 end-to-end 검증 통과 — vmcore 449M 확보, 총 다운타임 2분 31초.** 검증 중 마진 부족(캡처커널 부팅 71초 + 덤프 29초 = 100초 vs watchdog 잔여 90~180초)이 드러나 watchdog 을 300s 로 재상향. 커널 범프 내성 확인(037 함정 해당 없음). watchdog 메모리 2건 정정(timeout 변경 가능, bootstatus 신뢰 불가). devlog 038. **근본 원인은 여전히 미상 — 다음 프리즈의 vmcore 가 관건.** 남은 것은 dbgsym(ddebs 에 7.0.0-28 미발행, 저장소만 등록)._

_세션 후반 (EngineCore 세그폴트) — 038 의 EngineCore 크래시를 devlog 039 로 마저 추적. **원본 로그를 직접 읽어보니 038 의 "로그 유실" 은 오진**이었고(로테이션 없음, `docker logs` CLI 가 부분만 반환), 실제로는 **SIGSEGV 10건**이 온전히 남아 있었음. 충돌 지점이 10건 모두 무관 → **메모리 손상 시그니처**. 이어서 "하드웨어는 보드/CPU 교체 이후 계속 동일" 이라는 전제를 반영해 **작업량으로 정규화**하니 결론이 바뀜: 32B-AWQ 는 토큰당·가동시간당으로도 **약 6배** 더 자주 죽고, 정작 **호스트 RAM 은 16.1GB→10.3GB 로 덜 씀** → 호스트 RAM 불량 가설 반증, **AWQ/Marlin 커널이 유력**. 프리즈와 세그폴트는 **별개 문제로 분리**(프리즈는 두 모델 시대 모두, 세그폴트는 32B 시대만). 내일 첫 작업은 GPU 스왑이 아니라 **`--quantization awq` 로 Marlin 배제 테스트**._

_세션 종료: 2026-07-27 — 프리즈(038)와 세그폴트(039) 두 라인을 분리해 정리하고 마감. **프리즈 쪽은 kdump 3-고리 복구 + sysrq 실동작 검증까지 완료**(watchdog 300s), 이제 다음 프리즈의 vmcore 만 기다리면 됨. **세그폴트 쪽은 SIGSEGV 확인 + 작업량 정규화로 AWQ/Marlin 커널을 1순위 용의자로 좁힘**. 오늘 내 오진 2건(로그 유실 / 호스트 RAM 주범)은 devlog 038·039 에 정정 표시로 남김. 커밋 6개, devlog 2편, 메모리 4건. 마감 시점 상태: 컨테이너 5개 정상, git clean, `/var/crash` 비어 있음(테스트분 삭제), PaperMeister 재개돼 GPU1 가동 중, 05:06 재부팅 이후 요청 142건·EngineCore 500 0건. **내일 첫 작업은 `--quantization awq` 로 Marlin 배제 테스트** — 적용 후 기동 로그의 `quantization=` 값이 실제로 바뀌었는지 확인하는 것이 실험 성립 조건._

_2026-07-30 00:05 (격리 17h 결과 — 실험 성공) — **두 증상이 동시에 멈췄다.** 호스트 MCE 패닉 0건(기대 4.5, 우연 1.1%)에 uptime 20h 로 MCE 시대 최장(이전 10h47m), 그리고 **설계 시 기대하지 않았던 LLM HTTP 500 도 1,441 요청에 0건**(기대 6.5, 0.15%) — 마지막 500 이 격리보다 2.5h 앞선 07-29 04:22:50 이고 이후 엔진 연속 가동 19h43m, `RestartCount` 불변. 그 사이 부하는 요청 ~100/h·생성 ~81k tok/h·GPU1 95~100% 로 크래시 구간과 동일했다(단 07:00~10:45 3h47m 공백은 할인해야 함). → **040 의 "하나의 열화 CPU 가 프리즈와 세그폴트를 모두 낸다" 가 실험으로 확인되고 위치가 코어 5 로 특정됨. 039 의 AWQ/Marlin 가설 완전 종결.** 다만 이건 "코어 5 결함" 만 증명하고 "다이 전체로 번지는가" 는 답하지 않으므로 **CPU 교체 계획은 유효** — 얻은 것은 12스레드 안정 운영으로 준비할 **시간**이다. devlog 041 §8~10. **남은 판정은 24h = 07-30 06:58 UTC (15:58 KST)**, 이후 관찰 포인트는 **4/5/12/13 이 아닌 CPU 번호로 MCE 가 뜨는지** 하나뿐._

_2026-07-29 (코어 국소화 → 격리 실험) — 사용자의 "오늘은 오래 잘 돌고 있네" 로 시작했으나 실제로는 **uptime 2시간, 마지막 크래시 2시간 전**이었고 040 이후 이틀간 **12번 더 죽어 있었다**(MTBF 3.8h). watchdog+kdump 복구가 2분이라 서비스가 멀쩡해 보인 것 — 038 의 은폐 구조 반복. **apport `.crash` 가 0644 라 sudo 없이 읽힌다**는 걸 발견해(줄별 base64 → gunzip) **MCE 12건을 전수 디코드**했고, 전부 동일 `Bank 0 / 0x0005 / PCC=1` 인 데 더해 **에러 CPU 가 10건 CPU 5, 2건 CPU 4, 나머지 6개 코어는 0건** — 결함이 **물리 코어 5 하나**에 국소화됨을 확인. 1건은 커널 `intel_idle_ibrs` 에서 터져 **부하/발열 가설도 폐기**. rasdaemon 은 이틀간 CE 0건이라 CE 관측 전략 종결. → **BIOS 를 후순위로 내리고**(마이크로코드는 이미 OS 가 `0x2007006` 로드 중, 중고 보드 벽돌 리스크 회피) 리스크 0 인 **코어 4·5 오프라인**을 먼저 적용. `chcpu` 가 재부팅에 안 남는데 이 박스는 3.8h 마다 리셋되므로 **영속화가 실험 성립 조건** — `offline-bad-cores.service` 로 고정. devlog 041. **다음 작업은 코드가 아니라 판정: 06:58:33 UTC 기준 12h 무크래시=유의미(4%), 24h=확정(0.1%). 다른 코어에서 재발하면 다이 전체 열화 → CPU 교체 즉시 착수.**_

_2026-07-27 오전 (첫 vmcore → 원인 확정) — 038 에서 고쳐 둔 kdump 가 **설치 당일 09:02 프리즈를 그대로 캡처**. 사인은 **Fatal Machine Check Exception, Bank 0 (IFU), internal parity error, PCC=1** — `ADDRV=0` 이라 **DRAM 이 아니라 CPU 코어 내부** 결함. DIMM 가설 배제, 과열도 로그상 근거 없음(스로틀 0건). 같은 날 오전 세그폴트 2회(09:50·09:57)를 요청 수로 정규화하니 **설정이 40여 회 재시작 내내 완전히 동일한데 크래시율이 07-25 부터 9.1배** — 특히 **07-23 은 전 기록 최다 요청(10,217건)인데 크래시 0건**. 정적 소프트웨어 버그로는 불가능한 변화라 **039 의 AWQ/Marlin 가설과 프리즈/세그폴트 분리를 둘 다 철회**하고, **열화된 CPU 하나로 두 증상을 통합 설명**하는 것으로 결론. 하드웨어 실체가 중고 **i7-7820X (Xeon 아님, 소비자용 HEDT)** + **BIOS F1/2017 출시 당일 펌웨어**라는 점이 이 결론과 부합. rasdaemon 도입해 CE 상시 기록 시작. devlog 040. **다음 작업은 BIOS 업데이트** — 남은 분기가 "실리콘 열화(A)" vs "전압/설정 부족(B)" 이고 B 는 BIOS 로 공짜 해결되므로 먼저 시도, 그래도 MCE 가 뜨면 A 확정 → CPU 교체. `--quantization awq` 실험은 **취소**._
