# devlog 039 — vLLM EngineCore 세그폴트 추적: 메모리 손상 시그니처 확인

날짜: 2026-07-27
태그: 인시던트 조사 (원인 미확정 — **AWQ/Marlin 커널이 유력**, 판별 실험 설계)
선행: [devlog 038](20260727_038_silent_freezes_and_kdump_enablement.md) 에서
발견한 "동시간대 EngineCore 크래시 9회" 를 마저 판 것

## 요약

`llm` 컨테이너(Qwen3-32B-AWQ)의 EngineCore 가 **SIGSEGV 로 죽고 있음**을
확인. 보존 로그 33일치에서 세그폴트 **10건**을 찾았는데 **충돌 지점이 10건
모두 서로 무관한 위치**였음 — 참조카운트, 모듈 임포트, 에러 문자열 포맷,
torch 디스패치, 텐서 해제 등. 이건 특정 코드 버그가 아니라 **메모리 손상
(memory corruption) 의 전형적 시그니처**.

원인은 아직 확정 못 했으나, **작업량으로 정규화한 뒤 AWQ/Marlin 커널이 가장
유력**해짐 (증거 6·7). 처음엔 호스트 RAM 불량을 유력 후보로 봤으나 정규화
과정에서 반증됨 — 더 자주 죽는 32B 시대가 호스트 RAM 을 **더 적게** 씀.

그리고 **프리즈와 세그폴트는 별개 문제**로 분리하는 게 맞다는 결론.
프리즈는 두 모델 시대에 걸쳐 있고 모델과 무관, 세그폴트는 32B 시대에 집중.

## 먼저 — 038 의 오진 정정

038 에서 "크래시 시점 로그가 logrotate 로 유실됐다" 고 적었는데 **틀렸음**.

- 컨테이너 `json.log` 는 **단일 파일 37.6MB, 06-24~07-27 전 구간
  195,954 줄**이 온전히 존재. 07-26 만 15,361 줄.
- `/etc/logrotate.d/` 에 docker 항목 없음, 컨테이너 `LogConfig.Config` 도
  `map[]` (빈 값) → **로테이션이 일어난 적 자체가 없음**.
- 컨테이너 ID 와 LogPath ID 일치 → 재생성으로 인한 로그 초기화도 아님.

실제 원인은 **`docker logs` CLI 가 같은 파일에서 매번 다른 부분만 반환**한
것이었음:

| 명령 | 반환 범위 |
|---|---|
| `docker logs` (전체) | 06-24 ~ 06-25, 10,349 줄 |
| `docker logs --tail 100000` | 07-11 ~ 07-23, 44,293 줄 |
| `docker logs --tail 15` | 07-27 (최신) |
| `--since/--until` 로 07-26 구간 | **0 줄** (0.08 초 만에 반환 — 타임아웃 아님) |

원인은 미상. **교훈: `docker logs` 를 로그 유무 판정 근거로 쓰지 말 것.**
원본 파일을 직접 읽어야 함 (root 소유라 원샷 컨테이너 사용 —
`reference_db_ops_via_one_shot_container` 와 같은 패턴):

```bash
CID=$(docker inspect ocrserver-llm-1 --format '{{.Id}}')
docker run --rm -v /var/lib/docker/containers:/c:ro nginx:alpine \
  cat /c/$CID/$CID-json.log > llm_raw.jsonl
```

## 핵심 증거 1 — SIGSEGV 확인

vLLM 이 자체 시그널 핸들러로 찍는 배너가 로그에 있었음:

```
!!!!!!! Segfault encountered !!!!!!!
```

즉 Python 예외도 CUDA 에러도 OOM 도 아니고 **네이티브 메모리 폴트**.
스택은 77 프레임 전부 CPython 인터프리터 내부 (`_PyEval_EvalFrameDefault`,
`method_vectorcall` 등) — Python 레벨 프레임이 안 나옴.

## 핵심 증거 2 — 충돌 지점이 매번 다르다 (결정적)

세그폴트 10 건의 **최내곽 프레임**:

| 시각 (UTC) | 최내곽 프레임 | 서브시스템 |
|---|---|---|
| 06-25 16:10 | `PyObject_IsTrue` | 진리값 판정 |
| 06-27 13:42 | `Py_DECREF` → `subtype_dealloc` | 참조카운트/해제 |
| 06-27 22:39 | `get_tools_for_instruction` | CPython 계측 |
| 07-25 02:20 | `Py_INCREF` → `import_ensure_initialized` | **모듈 임포트** |
| 07-25 19:39 | `c10::Dispatcher::callBoxed` | torch 디스패처 |
| 07-25 21:21 | `Py_INCREF` → `_PyObject_GetMethod` | 메서드 조회 |
| 07-26 03:15 | `get_tools_for_instruction` | CPython 계측 |
| 07-26 03:44 | `THPVariable_clear` | torch 텐서 해제 |
| 07-26 05:01 | `Py_TYPE` → `_PyErr_FormatV` | **에러 메시지 포맷 중** |
| 07-26 19:46 | `_PyObject_MakeTpCall` → torch dispatch | 호출 |

**서로 아무 관계 없는 위치들.** 특히 `Py_INCREF` / `Py_DECREF` / `Py_TYPE`
가 반복 등장 = **포인터 역참조 중 사망**.

> 결정적 논거: 커널/라이브러리 **버그**라면 같은 자리에서 반복해 죽는다.
> 사방에서 죽는 것은 누군가 엉뚱한 메모리에 써놓고 나중에 그걸 건드린
> 코드가 죽는 패턴 — 즉 **메모리 손상**.

## 핵심 증거 3 — 두 번째 실패 유형: refcount assert

07-26 04:13:31 의 사망은 세그폴트가 아니라 **C++ 예외**였음:

```
terminate called after throwing an instance of 'c10::Error'
what():  self->cdata.use_count() == 1 INTERNAL ASSERT FAILED at
         "/pytorch/torch/csrc/autograd/python_variable.cpp":3623
Exception raised from THPVariable_clear
```

PyTorch 내부 assert — 텐서 래퍼를 해제하는데 C++ 쪽 refcount 가 1 이
아니었다는 뜻. **이것도 참조카운트 무결성 실패**이고, 세그폴트 중 한 건
(07-26 03:44) 도 같은 `THPVariable_clear` 였음. 두 실패 유형이 같은
영역(Python↔C++ refcount interop)을 가리킴.

## 핵심 증거 4 — 입력과 무관하다

500 을 받은 요청들의 프롬프트 크기가 정상 요청 분포 안에 완전히 들어감:

```
크래시 유발 요청: 1,893 ~ 4,351 chars (msgs=1, temp=0.1)
정상 요청 분포  : min 1,642 / p50 2,783 / p90 4,693 / p99 6,270 / max 10,601
```

특정 프롬프트가 유발하는 결정론적 버그가 **아님**. 확률적 발생.

## 핵심 증거 5 — 모델 교체 시점과 강한 상관

`llmserver.db` 가 06-09 부터 있어서 **같은 GPU 에서** 모델 교체
(devlog 033, 06-24) 전후를 비교할 수 있었음:

| 기간 | 모델 | 요청 | EngineCore 500 | 비율 |
|---|---|---|---|---|
| ~06-24 | Qwen3-14B (fp16) | 8,901 | 1 | 1/8,901 |
| 06-24~ | Qwen3-32B-**AWQ** (int4) | 44,449 | 39 | 1/1,140 |

**약 8배.** 14B 시절 비율이면 44,449 건에서 5 건이 기대값인데 39 건 관측 —
우연으로 설명 안 됨. 첫 세그폴트가 06-25 16:10, 모델 교체가 06-24.

이 기간 동안 **GPU·vLLM 이미지·워크로드 모두 불변** (이미지는 05-08 생성분
그대로, 2개월간 미변경 — digest 확인). 바뀐 건 모델과 양자화뿐.

## 핵심 증거 6 — 노출량으로 정규화해도 6배 (하드웨어 불변 전제)

**전제 확인**: 이 서버는 예전에 보드 자체가 의심스러워 **보드/CPU 를 (중고로)
가져와 교체**한 이력이 있는데, 그 교체는 이 프로젝트 시작 **이전**이었고
OS 도 그때 새로 설치됨 (루트 fs 생성 2026-05-11 06:30, 첫 부팅 06:38).
따라서 **관측된 모든 사건은 동일한 하드웨어 구성 위에서 일어남.**
메모리도 첫 부팅부터 `DMI: Memory slots populated: 3/8` 로 29회 부팅 내내 동일.

하드웨어가 불변이므로, 하드웨어는 **변화(8배 급증)** 를 설명할 수 없음.
남은 설명은 "32B 시대에 그냥 더 많이 돌려서 더 많이 죽었다"(노출량 증가)
인데, 이것도 성립하지 않음:

| | 요청 | 생성토큰 합 | 엔진 가동 | 크래시 | 토큰/크래시 | 가동h/크래시 |
|---|---|---|---|---|---|---|
| 14B fp16 | 8,473 | 3,096,837 | 46.8h | 1 | **3,096,837** | **46.8h** |
| 32B-AWQ | 33,907 | 20,645,560 | 265.2h | 39 | **529,373** | **6.8h** |

토큰 6.7배 · 가동시간 5.7배 증가에 크래시는 39배. **일한 양으로 나눠도
여전히 약 6배 더 자주 죽음** (토큰 기준 5.85배, 가동시간 기준 6.9배).

## 핵심 증거 7 — 호스트 RAM 사용량은 오히려 **줄었다** (RAM 가설 반증)

불량 RAM 가설의 논리는 "메모리를 많이 건드릴수록 불량 셀을 밟을 확률이
올라간다" 인데, `metrics.db` 의 1분 샘플로 확인하니 **예측이 정반대**:

| | 평균 호스트 RAM | 최대 |
|---|---|---|
| 14B fp16 (06-09~06-24, 21,239 샘플) | **16,110 MB** | 18,196 MB |
| 32B-AWQ (06-24~07-27, 45,256 샘플) | **10,320 MB** | 13,420 MB |

**더 자주 죽는 쪽이 호스트 메모리를 2/3 만 씀.** 따라서 세그폴트에 관한 한
호스트 RAM 불량은 주범이 아님.

> ⚠️ 이 문서 앞부분에서 "호스트 RAM 불량이 프리즈까지 한 번에 설명하는
> 유일한 후보" 라고 썼던 것은 **정규화 전 숫자만 보고 한 판단이었고,
> 증거 6·7 로 철회함.**

## 결론 — 문제는 하나가 아니라 둘이다

| | 호스트 프리즈 | EngineCore 세그폴트 |
|---|---|---|
| 발생 시기 | 05-22(14B 시대), 07-26×4(32B 시대) — **두 시대 모두** | 사실상 **32B 시대만** |
| 모델과의 상관 | 없음 | 강함 (정규화 후에도 6배) |
| 주 용의자 | **하드웨어 기반** (RAM / PSU / 보드) | **AWQ/Marlin 커널** |
| 추적 문서 | devlog 038 (kdump 로 vmcore 대기) | 이 문서 |

두 라인을 **분리해서** 추적할 것. 섞으면 판단이 흐려짐.

## Chandra 와의 비교는 성립하지 않는다

"Chandra OCR 에선 이런 현상 없었다" 는 관찰은 **비교군으로 못 씀**:

1. **Chandra 는 7주째 유휴** — 마지막 OCR 잡 2026-06-09 07:00, GPU0 util 0%.
   EngineCore 크래시는 전부 그 이후 발생. 일 안 하는 프로세스가 안 죽는 건
   정보가 아님.
2. **Chandra 도 vLLM** (0.21.0, llm 은 0.20.2). "vLLM 문제냐" 는 질문이
   성립 안 함 — 양쪽 다 vLLM.

즉 현 구성에서 "vLLM vs Chandra" 와 "GPU1 vs GPU0" 는 **완전히 교락
(confounded)** 되어 있어 관측만으로는 분리 불가.

## 원인 후보 — 세그폴트 한정 (증거 6·7 반영 후 재순위)

| 가설 | 무작위 충돌 지점 | 정규화 후 6배 | 호스트 RAM 감소 | 종합 |
|---|---|---|---|---|
| **AWQ/Marlin 커널의 메모리 손상 버그** | ✅ | ✅ | ✅ 무관 | **유력** |
| 호스트 RAM 불량 (non-ECC) | ✅ | ❌ 설명 못함 | ❌ **반증** | 약함 |
| GPU1 하드웨어 | ⚠️ | ❌ 같은 GPU 에서 1→39 | — | 가장 약함 |

`quantization=awq_marlin` 이 엔진 기동 로그에서 확인됨. RTX 8000 은
Turing(sm_75) 이라 최신 커널 경로에서 뒷전으로 밀리는 세대이고
(로그에도 `FA2 is only supported on devices with compute capability >= 8`),
int4 Marlin 커널이 sm_75 에서 상대적으로 덜 검증됐을 가능성.

### 관측 사각지대 두 곳 (둘 다 "안 보이는" 상태)

세그폴트의 주범은 아니지만, **프리즈 라인** 에서는 여전히 중요함.

- **호스트 RAM: non-ECC 확정**. `dmidecode` 결과
  `Error Correction Type: None`. Samsung `M378A4G43MB1-CTD` 32GB UDIMM.
  EDAC 도 미노출 → 비트 플립이 발생해도 **어디에도 안 찍힘**.
- **GPU ECC: 두 장 다 Disabled**. `ecc.mode.current = Disabled` 라
  ECC 카운터가 전부 `[N/A]`. RTX 8000 은 ECC 지원 모델인데 기본 off.

Xid 0 건, NVLink 에러 0 (035 수리 후 유지), CUDA 에러/OOM 없음.

### 메모리 4장 중 1장 미인식 (별건이지만 기록)

물리적으로 DIMM 이 **4장** 꽂혀 있는데 시스템은 **3장(96GB)만** 인식.
`dmidecode` 기준 `CPU1_DIMM_C0` 가 `No Module Installed`.

- **첫 부팅부터 그랬음** — OS 설치 직후 2026-05-11 06:38 첫 부팅 로그에
  이미 `DMI: Memory slots populated: 3/8`. 29회 부팅 전부 동일
  (`MemTotal` 도 96,199,188 kB 로 불변). 최근에 죽은 게 아님.
- 미인식 스틱은 주소 공간에 안 들어오므로 **그 스틱 자체가 손상을 일으키진
  않음**. 다만 메모리 서브시스템에 실제 결함이 있다는 증거이고, 나머지
  3장 중 하나가 경계선일 사전확률을 올림.
- **BIOS 가 F1 / 2017-07-04 — 보드 출시 초기 펌웨어, 한 번도 갱신 안 됨.**
  X299 초기 BIOS 는 메모리 트레이닝이 미숙하기로 알려져 있어, 스틱 불량이
  아니라 **BIOS 문제일 가능성** 있음. 마이크로코드는 OS 가 갱신 중
  (`0x2000023 → 0x2007006`) 이라 CPU 에라타는 커버되지만 트레이닝은 BIOS 몫.
- 이 박스에는 **접촉 불량 전례** 있음 — devlog 034/035 의 NVLink 브리지가
  정확히 그 건이었고 재장착으로 복구됨. DIMM 도 재장착/슬롯 교체를 먼저
  시도할 가치가 있음 (스틱 불량 vs 슬롯 불량 판별도 됨).

## 다음 단계 — 두 라인으로 분리

### A. 세그폴트 라인 (변수 하나씩)

1. **⭐ Marlin 커널 배제 테스트 (최우선)** — 모델도 GPU 도 그대로 두고
   **양자화 커널만** 교체. 지금은 vLLM 이 자동으로 `awq_marlin` 을 선택하는데,
   `--quantization awq` 를 명시하면 일반 AWQ 커널 경로를 탐.
   변수 하나만 움직이는 가장 깨끗한 실험이고, 기대 크래시 간격이
   **6.8 엔진가동시간**이라 하루면 판정.
   ```yaml
   # docker-compose.yml, llm command 에 추가
         --quantization awq
   ```
   - 크래시 멎음 → **Marlin 커널 확정**
   - 그대로 죽음 → AWQ 경로 전반 또는 다른 원인. 2번으로.
   - 주의: 일반 awq 커널은 Marlin 보다 느림. 미지원이면 기동 실패할 수 있으니
     기동 로그의 `quantization=` 값을 반드시 확인할 것.
2. **14B fp16 롤백 테스트** — devlog 033 실측상 32B-AWQ 대비 9% 느릴 뿐이라
   비용이 작음. 크래시가 멎으면 "양자화 경로" 로 범위 확정.
   (`hf_cache` 에 14B 가 남아 있는지 먼저 확인.)
3. **GPU 스왑** — 위 둘로도 안 좁혀질 때. `llm` 의
   `device_ids: ['1']` → `['0']`. 증거 6 에서 GPU1 가설이 가장 약해졌으므로
   우선순위를 내림.
   ⚠️ chandra-a 와 GPU0 공유 시 VRAM 부족(42.8G+44.5G > 48G) → chandra-a 를
   잠시 내릴 것. OCR 은 어차피 유휴.

### B. 프리즈 라인 (devlog 038 과 연결)

4. **다음 프리즈의 vmcore 대기** — kdump 는 038 에서 활성화·검증 완료.
   `/var/crash` 에 새 디렉터리가 생기면 그게 답.
5. **DIMM 재장착 / 슬롯 교체** — 케이스 열 일이 생기면. C0 스틱 접점 청소 후
   재장착 → 그래도 미인식이면 슬롯을 바꿔 꽂아 스틱/슬롯 판별.
   4장이 다 잡히면 X299 쿼드채널 정상 구성으로 복귀.
6. **BIOS 업데이트 검토** — F1(2017) 은 출시 초기 펌웨어. 메모리 트레이닝
   개선이 들어있을 가능성. 단 중고 보드 + 운영 중인 박스라 위험 대비 이득
   판단 필요.
7. **호스트 메모리 테스트** — non-ECC 라 소프트웨어 테스트뿐.
   무중단 부분 테스트: `memtester 30G 2` (여유 49GB). 단 **실행 중인
   vLLM 이 점유한 물리 페이지는 검사 못 하므로 "통과 = 무죄" 가 아님**.
   제대로 하려면 memtest86+ 로 부팅해 수 시간.
8. **GPU ECC 활성화 검토** — 하드웨어 가설 계량화. 가용 VRAM 1~2% 감소로
   현재 44.5GB 쓰는 모델이 빠듯해질 수 있으니 켠 뒤 로딩 확인. GPU 리셋 동반.

## 참고 — 조사에 쓴 명령

```bash
# 원본 로그 확보 (docker logs 는 신뢰 불가)
CID=$(docker inspect ocrserver-llm-1 --format '{{.Id}}')
docker run --rm -v /var/lib/docker/containers:/c:ro nginx:alpine \
  cat /c/$CID/$CID-json.log > llm_raw.jsonl

# 세그폴트 지점 추출 (json 한 줄씩, log 필드가 실제 출력)
python3 - <<'PY'
import json
L=[(j.get('time','')[:19], j.get('log','').rstrip())
   for j in (json.loads(l) for l in open('llm_raw.jsonl',errors='replace'))]
idx=[i for i,(t,s) in enumerate(L) if 'Segfault encountered' in s]
for i in idx:
    print('---', L[i][0], '---')
    for t,s in L[i+1:i+6]: print('   ', s.strip()[:95])
PY

# 모델 교체 전후 크래시율
docker exec ocrserver-wrapper-1 python3 -c "..."   # llmserver.db, http_status=500
```
