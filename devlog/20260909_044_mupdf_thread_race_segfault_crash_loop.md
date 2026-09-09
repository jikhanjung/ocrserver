# devlog 044 — 깨진 ICC 프로파일 PDF 하나가 wrapper 를 세그폴트 크래시 루프에 빠뜨림 (→ MuPDF 락 + resume 상한)

날짜: 2026-09-09
태그: 장애 1건 (00:54~01:06 UTC, OCR 전면 502 12분), wrapper 0.2.7
선행: [devlog 043](20260908_043_nginx_static_upstream_ip_reuse_404_storm.md)

## 요약

PaperMeister 가 6쪽짜리 PDF (`Hansen - A new trilobite species of
Hemisphaerocoryphe from.pdf`, hash `694ab34…`) 를 올리자 wrapper 프로세스가
**즉시 세그폴트**(`segfault at 0 ip 0`). 잡은 DB 에 `processing` 으로 남고,
재시작한 wrapper 의 lifespan resume 이 같은 PDF 를 다시 렌더 → 또 세그폴트
→ **무한 크래시 루프**. 12분간 11번 재시작, 그동안 `/ocr`·대시보드 전부
nginx 502.

원인은 **PyMuPDF 가 스레드 세이프하지 않다**는 것. 이 PDF 는 ICC 프로파일이
깨져 있어 MuPDF 가 `cmsOpenProfileFromMem failed` 를 내는데, 순차 렌더는
6쪽 다 정상이지만 wrapper 처럼 워커 스레드 6개가 동시에 `fitz.open()` +
`get_pixmap()` 을 하면 MuPDF 컬러스페이스 초기화 경로에서 레이스 →
널 함수 포인터 호출(`ip=0`). 재현 3회 중 2회 exit 139, 나머지 1회도 한
페이지 `FzErrorFormat`.

고침 (0.2.7):
1. **`_mupdf_lock`** — `_pdf_page_count`·`_render_one_page` 의 MuPDF 호출을
   전역 `threading.Lock` 으로 직렬화. 렌더는 쪽당 ~100ms, OCR 은 쪽당 수 초
   이므로 처리량 손실 없음. 락 대기 중엔 GIL 을 놓으니 이벤트 루프 영향 없음.
2. **resume 상한** — `jobs.resume_count` 컬럼 추가(자동 마이그레이션).
   기동 시 `processing` 잡을 재개할 때마다 +1, `OCR_RESUME_MAX_ATTEMPTS`(기본 3)
   에 도달하면 `failed` + `resume aborted: wrapper restarted Nx ...`. 어떤
   이유로든 프로세스를 죽이는 PDF 가 서비스 전체를 잡아먹지 못하게.

## 1. 타임라인 (UTC)

| 시각 | 사건 |
|---|---|
| 00:54:13 | PaperMeister `GET /ocr` (목록) 후 `POST /ocr` → job `7f775429`, 6쪽. `GET /ocr/7f775429` 200 |
| 00:54:14 | 커널: `uvicorn[32167]: segfault at 0 ip 0000000000000000 ... error 14`. 컨테이너 exit 139 |
| 00:54:15~00:56:07 | 도커 `restart: unless-stopped` 로 재기동 × 11. 매번 `[resume] re-spawned 1 'processing' job(s)` → `MuPDF error: format error: cmsOpenProfileFromMem failed` → 1초 내 세그폴트. backoff 1s→52s 로 늘어남 |
| 00:55 | 조사 시작. `docker compose ps`: wrapper `Restarting (139)`, RestartCount 11 |
| 01:00 | 원샷 컨테이너로 재현: 순차 렌더 OK / 6스레드 동시 렌더 exit 139 (2/3) |
| ~01:03 | 사용자가 제안된 SQL 로 잡 `7f775429` 를 `failed` 로 마킹 → 루프 종료 |
| 01:05 | 0.2.7 빌드, 새 이미지에서 6스레드 × 5회 전부 OK |
| 01:06 | `up -d --no-deps wrapper llmwrapper`. 기동 정상, `/api/services` 200 |
| 01:08 | 같은 PDF 를 `client_id=claude-verify-044` 로 재제출(job `e07cb269`) → 6/6 `ok`, 크래시 없음 |

## 2. 증거

- 세그폴트 11건이 CPU 0·1·2·3·7·8·9·11·15 에 분산 — 격리한 코어 4·5 와
  무관, 하드웨어가 아니라 소프트웨어 레이스.
- 같은 파일이 06-03 에 처음 제출됐을 땐 `done_with_errors`(5 ok / 1 failed).
  당시엔 렌더가 순차(별도 단계)였고, 0.1.14 에서 렌더를 OCR 워커 안으로
  옮기면서(GIL convoy 해결) 동시 렌더가 시작됐다. 그 뒤 처음 만난 ICC 깨진
  PDF 가 이 건.
- 재현 스크립트 (0.2.6 이미지, 6 스레드 각각 `fitz.open(stream=b)` +
  `get_pixmap(dpi=150)`): run1 exit 139, run2 exit 139, run3 exit 0 이지만
  page 3 `FzErrorFormat`. 0.2.7 (`main._render_one_page` 직접 호출, 락 포함):
  5/5 exit 0, 6쪽 모두 ok.
- PyMuPDF 문서: "PyMuPDF is not thread-safe" — 알려진 제약. 대부분의 PDF
  에서 우연히 되고 있었을 뿐.

## 3. 왜 락이지 서브프로세스가 아닌가

서브프로세스 렌더는 크래시 격리까지 되지만 쪽당 fork/spawn + 바이트 전달
비용이 있고, 이 서버의 병목은 GPU 라 렌더 직렬화로 잃는 게 없다. 락으로
레이스 자체가 사라지면 세그폴트도 사라진다(5/5). 그래도 남는 "어떤 이유로든
프로세스가 죽는 PDF" 는 2번 항목(resume 상한)이 받아준다 — 잡 하나가
`failed` 로 끝나고 서비스는 산다.

## 4. 운영 메모

- 이런 루프에 걸리면 **DB 에서 잡을 `failed` 로 마킹**하는 게 즉효
  (`docker run --rm -v /srv/ocrserver/data:/data --entrypoint python3
  honestjung/ocrwrapper:0.2.7 -c "..."`; DB 가 root 소유라 원샷 컨테이너).
  0.2.7 부터는 3번째 재시작에서 자동으로 같은 일이 일어난다.
- 세그폴트 여부는 `journalctl -k | grep segfault` 가 가장 빠르다. 컨테이너
  로그엔 크래시 직전 줄만 남고 traceback 이 없다.
- PaperMeister 는 `7f775429` 가 `failed` 이므로 그냥 다시 올리면 된다
  (failed 는 dedup 대상 아님). 실제로 같은 파일이 0.2.7 에서 6/6 성공.

## 5. 같은 세션의 디스크 정리

루트 LV 99% (6.2GB 남음) 이었다. sudo 없이 가능한 것만:
`docker builder prune -af` 4.7GB, dangling 이미지·`ocrwrapper:0.2.1~0.2.5`
태그 제거, `~/backups/papermeister-*.db.gz` 최신 3개만 남기고 21개(약 22GB)
를 `/mnt/disk1/backups/papermeister/` 로 이동 → **31GB 여유(91%)**.
남은 큰 덩어리: `/srv/ocrserver/hf_cache/hub` 97GB (root 소유 — Qwen3.5
27B/35B-A3B 52GB 는 실험 후 미사용, sudo 로 이동 가능), `data/pdfs` ~55GB,
도커 이미지 87GB (ocrserver 52 + vllm 32).
