# 20260515_007 — 인시던트: docker daemon 자동 재시작 + 과대 페이지 400 에러

## 증상

야간 배치(전날 저녁 큐잉, 새벽까지 진행) 중 두 가지 문제가 겹쳐서 발생.

1. 새벽에 두 건이 `done_with_errors`로 끝남 — 모든 페이지 실패, `done_pages=0`.
2. 아침에 확인하니 큐는 0이고 새 잡은 안 들어가지만 DB의 `processing` 행 3건이 있음에도 OCR이 진행되지 않음.

`/api/stats`:
```
{"counts":{"total":98,"queued":0,"processing":3,"done":93,"done_with_errors":2,"failed":0}}
```

## 원인 1: Docker daemon 자동 재시작 (UTC 2026-05-15 00:14)

`unattended-upgrades`가 `docker-ce`를 29.4.3 → 29.5.0으로 올리면서 daemon을 graceful 재시작.

```
$ journalctl -u docker --since yesterday | grep -i 'level=' | ...
05-15 00:13:59 ... Container failed to exit within 10s of signal 15 - using the force
05-15 00:14:05 ... Processing signal 'terminated'
05-15 00:14:07 ... Daemon shutdown complete
05-15 00:14:07 ... Starting docker.service
05-15 00:14:09 ... Loading containers: done
```

```
$ grep -E 'docker|Upgrade' /var/log/apt/history.log | tail
... apt upgrade -y
Upgrade: docker-ce-cli (5:29.4.3, 5:29.5.0), docker-ce (5:29.4.3, 5:29.5.0), ...
```

호스트 자체는 안 죽음 (uptime 2일 22시간 유지). `restart: unless-stopped`이라 컨테이너는 자동 부팅되지만, `chandra-a`(vLLM + chandra-ocr-2)가 콜드 스타트 + CUDA graph capture로 **약 4–5분간 502** 상태.

이 구간에 wrapper의 retry budget이 초과되어 in-flight 페이지가 모두 실패하고, 이후 wrapper가 재시작되면서 in-memory `_jobs` dict가 날아가 DB의 `processing` 3건이 좀비로 남음.

### wrapper의 한계 두 가지

`wrapper/main.py:21`:
```python
_RETRY_DELAYS = [5, 15, 30, 60]   # + 즉시 1회 = 약 110초
```

110초 < chandra 콜드 스타트 (4–5분). 게다가 `lifespan`에 startup-time scan이 없어서 `processing` 행을 자동 재개하지도 않음.

## 원인 2: 과대 페이지 → vLLM HTTP 400 (max_model_len 초과)

새벽의 `done_with_errors` 2건은 daemon 재시작과 무관. 백엔드가 살아 있을 때 발생했고, 모든 페이지가 페이지당 1.5–6.7초 만에 일관되게 실패했음. 페이지 에러 본문 확인:

```
HTTP 400  {"error":{"message":"Input length (16070) exceeds model's maximum context length (12384).","type":"BadRequestError",...}}
```

재현(`/data/pdfs/<hash>.pdf` 1페이지를 150 DPI로 렌더 → POST):
- `Liñán - 1978 - Bioestratigrafia` p0 → 3604×4543 px → ~16k tokens → 400
- 똑같이 251p 전부 16k+ tokens → 251/251 fail
- chandra-a 설정: `--max-model-len 12384`

`wrapper/main.py:359` 로직상 비-502/503 HTTPStatusError는 retry 없이 즉시 break하므로 빠르게 끝남.

### 코퍼스 분포 조사 (90개 PDF)

| 통계 | longest-side px (150 DPI) | max megapixels |
|---|---|---|
| min | 1312 | 1.09 |
| median | 1650 | 2.05 |
| p90 | 1824 | 2.28 |
| max | **4902** | **17.21** |

| 분포 | 개수 | 비율 |
|---|---|---|
| ≤ 2000 px | 86 | 95.6% |
| 2000–2500 px | 2 | 2.2% |
| 2500–3500 px | 0 | 0% |
| **> 3500 px** | **2** | **2.2%** |

Outlier 2건이 정확히 오늘 100% 실패한 그 두 PDF. 즉 **97.8% 코퍼스는 현재 설정으로 정상**, 2.2%만 처리 불가.

흥미로운 발견: Liñán PDF는 page rect width가 **1729 pt (~24 inch)** 로 비정상적으로 큼. PDF 생성 시점부터 페이지 크기가 잘못 잡힌 듯. 단순히 DPI를 낮추는 건 정상 PDF 품질만 깎고 outlier에는 부적절.

## 복구 조치 (오늘 적용)

### 좀비 정리

DB 직접 수정으로 3건 `failed` 처리:

```python
docker exec ocrserver-wrapper-1 python3 -c "
import sqlite3, time
c = sqlite3.connect('/data/ocrserver.db')
now = time.time()
c.execute(\"UPDATE jobs SET status='failed', completed_at=?, error='vLLM 400: image exceeds max_model_len' WHERE job_id LIKE '85c86035%'\", (now,))
for jid in ('03978f00','6bebd4e9'):
    c.execute(\"UPDATE jobs SET status='failed', completed_at=?, error='daemon restart; resubmitted' WHERE job_id LIKE ?\", (now, jid+'%'))
c.commit()
"
```

### 정상 처리 가능 잡 재큐

저장된 PDF(`/data/pdfs/<hash>.pdf`)를 wrapper의 `POST /ocr`로 재업로드. dedup은 `status='done'`만 매칭하므로 fresh job이 생성됨:

```python
import httpx
for name, hashfile in [...]:
    with open(f'/data/pdfs/{hashfile}', 'rb') as f:
        r = httpx.post('http://localhost:8000/ocr',
                       files={'file': (name, f, 'application/pdf')}, timeout=120)
```

Schmitt(7p), Topper(49p) 모두 정상 진행 확인.

## 향후 검토 사항 (미적용)

### A. 픽셀 상한 클램핑 — 권장

고정 DPI가 아닌 longest-side 픽셀 상한 기반으로 동적 DPI 결정.

```python
TARGET_DPI = 150
MAX_LONG_PX = 2200   # chandra-ocr-2 max_model_len 12384에 안전한 ceiling
def render_page(page):
    long_pt = max(page.rect.width, page.rect.height)
    dpi = min(TARGET_DPI, MAX_LONG_PX * 72 / long_pt)
    return page.get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72))
```

- 정상 88건은 그대로 150 DPI 유지
- outlier만 다운스케일 (Liñán은 ~92 DPI까지 떨어짐 — OCR 품질 저하 감수)
- 코드 한 군데(`wrapper/main.py` 페이지 렌더 부분) 수정으로 끝

대안: `_ocr_page`에서 400 응답 메시지 파싱해서 한 번 다운스케일 재시도. 정상 path 그대로 두는 장점.

### B. wrapper retry budget 확장

`_RETRY_DELAYS = [5, 15, 30, 60, 120, 180]` 정도로 늘리면 chandra 콜드 스타트(~5분)를 견딜 수 있음. 단점은 백엔드가 진짜 죽은 경우 page failure 확정까지 시간이 더 걸림.

### C. Lifespan에서 좀비 청소

```python
async def lifespan(app: FastAPI):
    ...
    await db_init()
    # mark stale 'processing' rows as failed (or 'queued' + relaunch)
    await _db.execute("UPDATE jobs SET status='failed', error='wrapper restart' WHERE status='processing'")
    await _db.commit()
    yield
```

수동 개입 없이 다음 재시작 때 좀비 자동 정리.

### D. unattended-upgrades에서 docker-ce 제외

`/etc/apt/apt.conf.d/50unattended-upgrades`:
```
Unattended-Upgrade::Package-Blacklist {
    "docker-ce";
    "docker-ce-cli";
    "containerd.io";
};
```

자동 보안 업데이트 대신 사람이 결정하는 시점에 daemon 재시작 (긴 배치 외 시간).

### E. chandra-a `--max-model-len` 상향

12384 → 24576 등으로 늘리면 outlier도 처리 가능. 단점: GPU memory 추가 점유, 정상 페이지의 prefill latency도 영향.

## 저장 데이터 (참고)

- 라이브 deploy: `/srv/ocrserver/` (root 소유)
- DB: `/srv/ocrserver/data/ocrserver.db` → wrapper 컨테이너 내부 `/data/ocrserver.db`
- PDFs: `/srv/ocrserver/data/pdfs/<sha256>.pdf` (90개, 923 MB)

이 디렉토리 구조 덕분에 PDF 재업로드 없이도 `POST /ocr`로 원본 파일 재처리가 가능.
