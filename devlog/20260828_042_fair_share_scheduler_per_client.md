# devlog 042 — 클라이언트 간 GPU 슬롯 공평 분배 (wrapper 0.2.4 → 0.2.6)

날짜: 2026-08-28
태그: wrapper 기능 (스케줄러), 배포 사고 1건 (nginx 502 ~2분), 문서 갱신
선행: [devlog 041](20260729_041_mce_localized_to_core5_and_cpu_offlining.md)

## 요약

PaperMeister 인스턴스 두 개가 같은 OCR 서버를 쓰기 시작했는데, 한쪽이 큰
PDF를 넣으면 다른 쪽이 **1쪽짜리 PDF 하나도 못 시작하고 몇 분씩** 기다렸다.
원인은 wrapper의 동시성 제어가 FIFO 세마포어 하나뿐이라는 것. 이를
**활성 클라이언트 수로 슬롯을 나누는 공평 분배 스케줄러**로 바꿨다
(wrapper **0.2.3 → 0.2.4**). 혼자면 12, 둘이면 6/6, 한쪽이 끝나면 다시 12.

배포 중 `docker compose up -d llmwrapper`가 `depends_on`으로 `llm`을
같이 띄우는 바람에 컨테이너 IP 배정이 바뀌고 nginx가 옛 wrapper IP를 물고
있어 **약 2분간 502**가 났다. 교훈 2개는 §5.

## 1. 증상 — 실제로 잡힌 현장

배포 직전 `/api/jobs`:

```
691f7f5a papermeister-7ceac4ea 184/296 age 9m 1971-유정자 규조 지질광상조사연구보고제13호.pdf
0227a05c papermeister-7355a25d 0/1     age 6m Kobayashi 1953, short notes.pdf
6a483f1b papermeister-7355a25d 0/1     age 6m Xing - 2014 - Geology Bedrock of China.pdf
... (같은 클라이언트의 1쪽짜리 job 12개 전부 0/1, 6분째)
```

296쪽 job 하나가 슬롯 12개를 전부 쥐고, 뒤에 온 클라이언트의 1쪽짜리
12개는 그 job의 남은 100여 쪽이 **다** 줄을 선 뒤에야 차례가 온다.

## 2. 원인

`wrapper/main.py` 0.2.3:

- `_sem = asyncio.Semaphore(OCR_CONCURRENCY)` 하나를 모든 job의 모든
  페이지가 공유.
- `_run()`이 PDF의 전 페이지 코루틴을 `asyncio.gather`로 **한꺼번에**
  띄우고, 각 `_ocr_page`가 `async with _sem`.
- `asyncio.Semaphore`의 대기열은 FIFO. 먼저 gather된 296개가 앞에 서고
  나중 클라이언트의 12개는 그 뒤. 클라이언트라는 개념이 스케줄링에 없음.

## 3. 설계 — `_FairScheduler`

`asyncio.Condition` + 클라이언트별 카운터 두 개(`_inflight`, `_waiting`).

- **활성 클라이언트** = 지금 페이지가 처리 중이거나 슬롯을 기다리는
  `client_id` 집합. 없는 요청은 전부 `None` 키 하나.
- **클라이언트당 상한** = `ceil(OCR_CONCURRENCY / 활성 수)`. 매 acquire
  마다 다시 계산되므로 고정 상한(방법 1, "클라이언트당 6")과 달리 **혼자일
  때 GPU 한 장이 놀지 않는다.**
- 시작 조건: `전체 inflight < total` **and** `내 inflight < 내 상한`.
- release 때 `notify_all` → 모든 대기자가 조건 재평가. 대기자 수백 개여도
  페이지 완료가 초당 수 건이라 비용 무시 가능.
- 상한이 줄어드는 순간(B 합류) A는 이미 12개를 돌리고 있을 수 있는데,
  A는 새 페이지를 못 시작하고 B가 빈 자리를 차지하므로 **페이지 한 장
  처리 시간 안에 6/6으로 수렴**한다. 시뮬레이션과 실배포 모두에서 확인.

시뮬레이션(60쪽 A 먼저, 0.5초 뒤 30쪽 B, 12슬롯):

```
t=0.5 (A 혼자):  active 1, limit 12, inflight 12
t=1.0 (A+B):     active 2, limit 6,  A inflight 6 / B inflight 6
B 종료 후:       active 1, limit 12
max inflight seen per client: A 12, B 6   (전역 12 초과 0회)
```

실배포 직후 `/api/services.scheduler`:

```json
{"total": 12, "active_clients": 2, "per_client_limit": 6, "inflight": 8, "waiting": 76,
 "per_client": {"papermeister-7355a25d": {"inflight": 2, "waiting": 0},
                "papermeister-7ceac4ea": {"inflight": 6, "waiting": 76}}}
```

6분간 0/1이던 1쪽짜리 12개가 배포 후 1분 안에 전부 끝났다.

## 4. API / UI 변경

- `POST /ocr`의 `client_id`가 dedup 키에 더해 **분배 단위**가 됐다. 여러
  프로젝트가 붙으려면 서로 다른 `client_id`가 필수 (같으면 한 클라이언트로
  묶임).
- `/api/services`에 `scheduler` 블록 신설 (위 예시).
- `/api/stats`: `active_clients` 추가.
- **`recommended_concurrency` 의미 변경** (사용자 요청): 서버 전체 값
  (`alive × 6`)이 아니라 **호출한 클라이언트의 몫**. 사용 가능 슬롯
  `min(OCR_CONCURRENCY, alive × OCR_PER_BACKEND_CONCURRENCY)`을 활성
  클라이언트 수로 나눈 올림값. `?client_id=` 또는 `X-Client-ID`로 자신을
  밝히면 아직 제출 전이라도 "내가 들어가면 받을 값"(활성+1로 나눔)이 나온다.
  `/api/stats`, `/api/services` 둘 다 동일.
- **0.2.5 (같은 날 후속)** — "`?client_id=`를 붙여야 한다는 걸 어떻게
  알지?"라는 지적. 응답에 힌트가 없었다. `/api/stats`·`/api/services`에
  `recommended_concurrency_new_client`(새 클라이언트가 합류하면 받을 값)와
  `client_id`(서버가 받은 id, 없으면 null)를 추가. id 없이 부르면 두 권장값이
  다르게 나와 그 자체가 힌트이고, 제출 전이면 `_new_client`를 쓰면 안전하다.
  id를 주면 둘이 같아진다. 0.2.5 배포는 `--no-deps` + reload로 무사고.
- **0.2.6** — 대시보드 `/` job 테이블에 「클라이언트」 열. 두 클라이언트가
  섞여 돌 때 어느 job이 누구 것인지 한눈에 보이도록.
- `/status` "권장 동시성" → "클라이언트당 권장 동시성 (활성 N, 처리 x /
  대기 y)". `/` 상단 칩 → "OCR 백엔드 2/2 · 클라이언트 2개 · 클라이언트당 6".
- 문서: `WRAPPER_API.md`에 「클라이언트 간 공평 분배」 섹션, `scheduler`·
  `recommended_concurrency` 설명. 같은 날 앞서 0.2.1~0.2.3 누락분
  (`force`, `total_pages`, `in_progress`, `_meta.images/llm_model`, 파일명
  fallback dedup, 503 during mode switch)도 문서에 채웠다 (`8861592`,
  `a18f219`).

## 5. 배포 사고 — nginx 502 약 2분 (08:27~08:29 UTC)

순서: `cp docker-compose.yml /srv/ocrserver/` → `docker compose up -d
wrapper llmwrapper`.

1. `llmwrapper`는 `depends_on: llm`이라 **`llm` 컨테이너가 같이 시작**됐다.
   OCR×2 모드에서 GPU 1은 chandra-b가 42.8GB 쓰는 중. 15초 만에
   `docker compose --profile llm stop llm`으로 내려서 실제 GPU 충돌은
   없었다(메모리 변동 없음, chandra-b 에러 0).
2. 그 여파로 새 wrapper가 **다른 IP**(172.18.0.5 → 172.18.0.2)를 받았고,
   nginx는 `upstream wrapper { server wrapper:8000; }`를 시작 시 한 번만
   해석하므로 옛 IP로 `connect() failed (113: Host is unreachable)`.
   wrapper 자체는 직접 호출 200/0.15s로 멀쩡했다.
3. `docker compose exec nginx nginx -s reload`로 즉시 복구.

`mode-ocr.sh`/`mode-llm.sh`가 wrapper 재생성에 `--no-deps`를 쓰는 이유가
이것이었다. 두 번째 배포(recommended_concurrency 반영)는
`up -d --no-deps wrapper llmwrapper` + 곧바로 `nginx -s reload`로 무사고.

**규칙**: wrapper/llmwrapper 재생성은 항상 `--no-deps`. nginx reload는
같은 날 §8로 불필요해졌다.

## 8. nginx: static upstream → resolver + 변수 proxy_pass (같은 날 오후)

§5의 두 번째 원인을 근본 해결. `nginx.ocr.conf`/`nginx.llm.conf` 공통:

```nginx
resolver 127.0.0.11 valid=10s ipv6=off;   # Docker embedded DNS
server {
    set $wrapper http://wrapper:8000;
    location /ocr { proxy_pass $wrapper; ... }   # 6곳 전부 변수로
```

LLM 설정은 추가로 `set $llm`, `set $llmwrapper`, 그리고 `/llm/` 접두어
제거가 literal `proxy_pass http://llmwrapper:8000/`의 trailing slash에
의존하고 있었으므로 `rewrite ^/llm/(.*)$ /$1 break;`로 명시.

변수 `proxy_pass`는 요청마다(10s 캐시) 이름을 다시 풀기 때문에 wrapper가
새 IP를 받아도 nginx가 따라간다. 부수 효과: 설정 로드 시 이름을 풀지
않으므로 **`llm` 컨테이너가 꺼진 상태에서도 LLM 설정이 `nginx -t`를
통과**한다 (static upstream 때는 불가).

`chandra` upstream은 `least_conn`이 필요해 static 유지. chandra 재생성은
mode 스크립트가 reload를 이미 하므로 문제 없음.

검증 순서:
1. 두 파일을 컨테이너에 `docker cp` → `nginx -t -c /tmp/cand.*.conf` 둘 다 OK
2. `/srv`에 `cp`(in-place, inode 3674466 유지) → `nginx -t` → reload
3. `/`, `/api/stats`, `/ocr`, `/status`, `/metrics`, `/static/*`, `/health`,
   `/v1/models` 전부 200
4. wrapper `--force-recreate` → IP가 우연히 같은 .2 → 증명 안 됨
5. `compose rm -sf wrapper` → alpine 컨테이너로 .2 선점 → wrapper up → **.7**
   → reload 없이 즉시 200 × 5, nginx 로그 `Host is unreachable` 0건 → 선점
   컨테이너 제거

## 6. 호스트 근황 (부수 관찰)

- 코어 4·5 격리 이후 부팅 기록: 07-29 04:05 → **08-13 07:54 (15일, 정상
  종료)** → **08-28 03:14 (15일, 정상 종료)** → 현재. 두 번 다
  `systemd-shutdown` 로그로 끝났고 MCE 패닉은 **0건**. 041의 결론이
  한 달째 유지되고 있다.
- 오늘 03:14 재부팅 후 03:15 `offline-bad-cores.service` 정상 적용,
  온라인 CPU `0-3,6-11,14-15`.
- OCR 워크로드 재개: 07-27 시점 "7주째 유휴"였는데 오늘 PaperMeister
  인스턴스 2개가 동시에 OCR을 돌리고 있다 (누적 7,823건). 이번 작업의
  동기.
- 오늘 `mode-ocr.sh`로 **OCR×2 모드** 전환 (chandra-b 기동 ~6분,
  `init engine 344s`).

## 7. 파일

- `wrapper/main.py` — `_FairScheduler`, `_recommended_concurrency()`,
  `/api/stats`·`/api/services` 시그니처(`client_id` query, `X-Client-ID`)
- `wrapper/status.html`, `wrapper/dashboard.html` — 라벨
- `docker-compose.yml` — `ocrwrapper:0.2.3 → 0.2.4 → 0.2.5 → 0.2.6` (wrapper, llmwrapper)
- `nginx.ocr.conf`, `nginx.llm.conf` — resolver + 변수 proxy_pass (§8)
- `docs/WRAPPER_API.md`, `docs/ENDPOINTS.md`
- 이미지 `honestjung/ocrwrapper:0.2.6` Hub 푸시 (digest `ae338a81564b`). 0.2.4/0.2.5는 로컬 중간 단계
