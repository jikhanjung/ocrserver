# 20260515_010 — wrapper 재시작 시 in-flight job 자동 resume

## 배경

`wrapper`가 재시작되면 메모리의 `_jobs` dict와 asyncio task가 사라지지만 DB의 `processing` 행은 그대로 남아 영구 좀비가 됨. 오늘만 6+ 회 발생 (코드 수정 + 자동 docker-ce 업그레이드). 매번 수동으로 좀비 정리 + 재큐 필요했음.

## 변경 내용

### `_resume_processing_jobs()` 추가, `lifespan`에서 호출

`db_init` 직후 실행. `status='processing'` 행을 모두 스캔해서:

1. `file_hash` 없음 → `failed`로 마킹 (legacy 잡, 복구 불가)
2. `/data/pdfs/<file_hash>.pdf` 없음 → `failed`로 마킹
3. 그 외 → 저장된 PDF 읽고, `pages` 테이블에서 `status='ok'`인 page_num 집합 추출, `_run`을 `skip_pages={...}`로 재스폰

```python
done_set = {p["page_num"] for p in page_rows if p["status"] == "ok"}
asyncio.create_task(_run(jid, pdf_bytes, skip_pages=done_set))
```

### `_run`에 `skip_pages` 인자 추가

기존 흐름은 그대로 두고 두 군데만 분기:
- 신규 잡(skip_pages 비어있음): 기존처럼 `total_pages` 초기화 + DB update
- resume 잡: 초기화 스킵 (이미 `_resume_processing_jobs`가 in-memory state 복원해둠)

페이지 렌더 루프는 `if i in skip_pages: continue`로 건너뛰고, dispatch는 `(page_num, b64)` tuple 리스트로 명시 인덱싱:

```python
todo: list[tuple[int, str]] = []
for i in range(n):
    if i in skip_pages:
        continue
    ...
    todo.append((i, base64.b64encode(...).decode()))

await asyncio.gather(*[_ocr_page(job, i, b64, client) for i, b64 in todo])
```

기존엔 `enumerate(pages_b64)`로 인덱스를 매겼기 때문에 skip이 들어가면 인덱스가 어긋났음. 이 부분만 수정.

### in-memory `_jobs` 복원

resume 시 `_jobs[jid]`를 빈 새 dict가 아니라 DB 상태 그대로 복원:
- `done_pages` = 이미 `ok`인 페이지 수
- `failed_pages` = 0 (재시도하므로)
- `pages[i]` = 'ok' 페이지면 markdown 포함 dict, 아니면 None (재처리 시 채워짐)
- `client_id` 등 메타데이터 보존

`failed_pages`를 0으로 리셋하는 이유: skip_pages에 들어있지 않은 페이지(즉 이전에 failed였거나 미처리)는 모두 재시도 대상이고, 새로 채워질 결과 기준으로 카운트해야 정확.

## 검증

진행 중이던 잡(e0f07930, 0/4 pages 진행 중)이 있는 상태에서 wrapper rebuild + 재시작:

```
[before] e0f07930 processing 0/4 fail=0
[restart 진행 — 컨테이너 recreate]
INFO:     Started server process [1]
INFO:     Waiting for application startup.
[resume] re-spawned 1 'processing' job(s)
INFO:     Application startup complete.
[after 5s] e0f07930 processing 1/4 fail=0
[wait] TERMINAL: done 4/4 fail=0 client_id=papermeister-7355a25d
```

- 재시작 직후 1페이지 처리 시작 → 정상 resume
- 종료 시 status=done, 4/4 ok, 0 fail
- client_id 정상 보존

## Edge cases

| 상황 | 동작 |
|---|---|
| `file_hash` 없는 legacy 잡 | `failed`로 마킹, error="resume failed: no file_hash" |
| 저장 PDF 파일 누락 | `failed`로 마킹, error="resume failed: missing /data/pdfs/..." |
| 진행 0% (모든 페이지 미처리) | skip_pages=set(), 전체 재처리 |
| 일부 페이지 ok + 나머지 failed | ok는 보존, 나머지만 재시도 |
| `total_pages` mismatch (PDF 변경?) | 현재 `total_pages` 그대로 사용. PDF 자체는 hash 기반이라 사실상 불변 |

## 효과

- 코드 수정 후 `docker compose up -d wrapper` 자유롭게 가능 (좀비 없음)
- `unattended-upgrades`로 docker-ce가 자동 업그레이드되어도 in-flight job이 자동으로 살아남음
- OOM/host 재부팅 등 모든 재시작 시나리오에 robust
- GPU 시간 절약: 이미 처리된 페이지는 재OCR 안 함

## 미적용 (이번엔 보류)

- **A**: unattended-upgrades에서 docker-ce 제외 (운영 설정)
- **C**: `_RETRY_DELAYS` 확장 (chandra 콜드 스타트 대비)
- **D**: SIGTERM 시 graceful shutdown (in-flight 페이지를 우아하게 처리)

B3만으로도 좀비 발생은 완전히 막힘. C는 별도 효율 개선 항목.
