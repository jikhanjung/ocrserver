# devlog 039 — vLLM EngineCore 세그폴트 추적: 메모리 손상 시그니처 확인

날짜: 2026-07-27
태그: 인시던트 조사 (원인 미확정, 후보 3개로 좁힘 + 판별 실험 설계)
선행: [devlog 038](20260727_038_silent_freezes_and_kdump_enablement.md) 에서
발견한 "동시간대 EngineCore 크래시 9회" 를 마저 판 것

## 요약

`llm` 컨테이너(Qwen3-32B-AWQ)의 EngineCore 가 **SIGSEGV 로 죽고 있음**을
확인. 보존 로그 33일치에서 세그폴트 **10건**을 찾았는데 **충돌 지점이 10건
모두 서로 무관한 위치**였음 — 참조카운트, 모듈 임포트, 에러 문자열 포맷,
torch 디스패치, 텐서 해제 등. 이건 특정 코드 버그가 아니라 **메모리 손상
(memory corruption) 의 전형적 시그니처**.

원인은 아직 확정 못 했고 후보 3개로 좁힘. 판별 실험(GPU 스왑)은 내일 수행.

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

## Chandra 와의 비교는 성립하지 않는다

"Chandra OCR 에선 이런 현상 없었다" 는 관찰은 **비교군으로 못 씀**:

1. **Chandra 는 7주째 유휴** — 마지막 OCR 잡 2026-06-09 07:00, GPU0 util 0%.
   EngineCore 크래시는 전부 그 이후 발생. 일 안 하는 프로세스가 안 죽는 건
   정보가 아님.
2. **Chandra 도 vLLM** (0.21.0, llm 은 0.20.2). "vLLM 문제냐" 는 질문이
   성립 안 함 — 양쪽 다 vLLM.

즉 현 구성에서 "vLLM vs Chandra" 와 "GPU1 vs GPU0" 는 **완전히 교락
(confounded)** 되어 있어 관측만으로는 분리 불가.

## 원인 후보 (미확정)

| 가설 | 무작위 충돌 지점 | 32B 전환 8배 | 호스트 프리즈 |
|---|---|---|---|
| **호스트 RAM 불량 (non-ECC)** | ✅ | ⚠️ 약함 | ✅ 설명함 |
| **AWQ/torch 네이티브 경로의 메모리 손상 버그** | ✅ | ✅ | ❌ |
| GPU1 하드웨어 | ⚠️ | ❌ (같은 GPU 에서 1→39) | ⚠️ |

### 관측 사각지대 두 곳 (둘 다 "안 보이는" 상태)

- **호스트 RAM: non-ECC 확정**. `dmidecode` 결과
  `Error Correction Type: None`. Samsung `M378A4G43MB1-CTD` 32GB UDIMM ×3
  (A0/B0/D0 장착, C 채널 공석 — X299 쿼드채널에 **비대칭 3장 구성**).
  EDAC 도 미노출 → 비트 플립이 발생해도 **어디에도 안 찍힘**.
- **GPU ECC: 두 장 다 Disabled**. `ecc.mode.current = Disabled` 라
  ECC 카운터가 전부 `[N/A]`. RTX 8000 은 ECC 지원 모델인데 기본 off.

Xid 0 건, NVLink 에러 0 (035 수리 후 유지), CUDA 에러/OOM 없음.

호스트 프리즈는 **2026-05-22 에도 있었음**(devlog 026, 모델 교체 두 달 전)
이라 프리즈와 세그폴트가 별개일 수도, 둘 다 RAM 에서 올 수도 있음.

## 다음 단계

1. **GPU 스왑 실험 (내일)** — 교락을 푸는 유일한 방법. OCR 이 유휴라
   GPU0 가 비어 있음. `docker-compose.yml` 의 `llm` 서비스
   `device_ids: ['1']` → `['0']`. 크래시율이 하루 ~9 건이라 **하루면 판정**:
   - GPU0 에서도 계속 죽음 → GPU1 하드웨어 배제, 소프트웨어/호스트 RAM
   - GPU0 에서 뚝 끊김 → GPU1 하드웨어 확정
   - 주의: chandra-a 와 GPU0 공유 시 VRAM 부족 (42.8G + 44.5G > 48G).
     chandra-a 를 잠시 내리고 할 것.
2. **호스트 메모리 테스트** — 1번에서 GPU 하드웨어가 배제되면 다음 용의자.
   non-ECC 라 소프트웨어 테스트(memtest86+ 등) 외에 방법이 없음.
   비대칭 3-DIMM 구성이라 한 장씩 빼고 돌리는 절반법도 가능.
3. **GPU ECC 활성화 검토** — 하드웨어 가설을 숫자로 계량화.
   단 가용 VRAM 이 1~2% 줄어 현재 44.5GB 쓰는 모델이 빠듯해질 수 있으므로
   켠 뒤 로딩 확인 필요. GPU 리셋(리부팅) 동반.
4. **완화책 후보** (원인 확정 전 임시): AWQ 대신 fp16 14B 로 롤백하면
   크래시율이 1/8 로 떨어질 것으로 예상되나, 품질/속도 손해와 맞바꾸는
   것이라 원인 규명 후 판단.

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
