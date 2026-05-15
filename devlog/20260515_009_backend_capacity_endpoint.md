# 20260515_009 — OCR backend capacity를 wrapper API로 노출

## 배경

GPU 1번을 LLM과 OCR 사이에서 토글하는 구조(`mode-llm.sh` ↔ `mode-ocr.sh`)인데, 클라이언트는 지금 어느 모드인지를 알 방법이 없었음. 그래서:

- PaperMeister 같은 클라이언트는 `min_queued_pages=6` 같은 상수 pref에 박혀 있어 OCR 모드(GPU 2장 = 권장 12)일 때도 6개 채우고 멈춤. backend가 놀게 됨
- "지금 OCR backend 몇 개 떠 있어?"를 알려면 `docker ps` 같은 운영용 명령에 의존해야 함

서버 쪽에 한 줄 추가해서 클라이언트가 mode 적응형으로 큐를 채울 수 있게 함.

## 변경 내용

### 1. 백엔드 health 직접 프로브

`OCR_BACKENDS=chandra-a,chandra-b` (env, 기본값) 명단을 wrapper 컨테이너에서 각각 `http://{name}:8000/health`로 GET해 alive 여부 판정. 누락된 backend는 DNS 실패로 `down` 처리되며 정상 동작.

nginx upstream config 파싱이나 docker socket보다 단순·정확. backend 추가 시 env 한 줄 변경.

### 2. 캐시 (TTL 5초)

`/api/stats`는 자주 폴링되므로 매번 cross-container HTTP 프로브를 돌리면 부담. `_refresh_probe_cache()`가 5초 TTL로 결과를 보관, `/api/stats`와 `/api/services`가 같은 캐시 공유. asyncio.Lock으로 thundering herd 방지.

### 3. /api/stats 응답 확장

```json
{
  "counts": {...},
  "ocr_backends_alive": 1,
  "ocr_backends_total": 2,
  "recommended_concurrency": 6,
  "mode": "llm+ocr",
  ...
}
```

| 필드 | 정의 |
|---|---|
| `ocr_backends_alive` | health 200 응답한 OCR backend 수 |
| `recommended_concurrency` | `alive × OCR_PER_BACKEND_CONCURRENCY` (기본 6) — 클라이언트가 채워둘 in-flight 페이지 수 권장값 |
| `mode` | 운영 모드 라벨 (아래) |

`mode` 값(GPU 2장 환경 기준):

| 값 | 의미 |
|---|---|
| `2ocr` | OCR backend 2개 (mode-ocr.sh) |
| `llm+ocr` | OCR 1개 + LLM (mode-llm.sh, 기본) |
| `1ocr` | OCR 1개만, LLM 없음 |
| `llm` | LLM만 alive (OCR 다운) |
| `down` | 응답 없음 |

GPU 3장 이상이면 `llm+2ocr`도 동적으로 생성됨.

### 4. /api/services 응답 확장

per-backend 상세 + 동일 메타. 대시보드/디버깅용:

```json
{
  "ocr_backends": {
    "alive": 1, "total": 2,
    "per_backend_concurrency": 6,
    "recommended_concurrency": 6,
    "per_backend": {
      "chandra-a": {"status": "ok", "http_status": 200},
      "chandra-b": {"status": "down", "error": "..."}
    }
  },
  "_meta": { "mode": "llm+ocr", "probe_age_s": 0.3, ... }
}
```

### 5. 대시보드 헤더 표시

navbar에 `OCR backends 1/2 · rec.6` 형식으로 노출. 이미 5초 폴링 중이라 별도 인프라 없음.

## 클라이언트 적용 패턴

```python
stats = requests.get("http://localhost:8080/api/stats").json()
target_inflight = stats["recommended_concurrency"]   # 모드 따라 6 또는 12
# 큐에 target_inflight 미만 남으면 추가 PDF 제출
```

PaperMeister는 `ocr_min_queued_pages` pref를 fallback으로 두고, server가 노출한 값이 있으면 그것 우선하는 식으로 wiring 예정.

## 검증

LLM 모드(현재 상태)에서:

```bash
$ curl -s http://localhost:8080/api/stats | jq '.mode, .recommended_concurrency, .ocr_backends_alive'
"llm+ocr"
6
1
```

OCR 모드 전환 시 `mode-ocr.sh` 후 chandra-b가 뜨면 자동으로 `2ocr` / 12 / 2로 전환 (캐시 5초 안에 반영).

## 새 환경변수

| 변수 | 기본값 | 용도 |
|---|---|---|
| `OCR_BACKENDS` | `chandra-a,chandra-b` | 프로브 대상 컨테이너명 |
| `OCR_BACKEND_PORT` | `8000` | health 포트 |
| `OCR_PER_BACKEND_CONCURRENCY` | `6` | backend당 권장 동시성 (RTX 8000 saturation point) |

## 향후

- wrapper의 자체 semaphore(`OCR_CONCURRENCY`)를 mode 변경에 따라 자동 조정 (지금은 env로 고정)
- 캐시 TTL을 env로
- `mode` 변경 시 webhook/SSE로 클라이언트 푸시 알림
