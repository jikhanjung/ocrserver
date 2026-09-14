# P37 — 도판 패널 분할을 추출 파이프라인에 편입 (Astra CLI)

**작성**: 2026-09-09
**동기**: 9/8 실험(devlog 248~252)으로 `gpt-6-astra` 가 화석 도판의 패널 경계를 쓸 만하게
잡는다는 것과, 그것을 ChatGPT 구독 로그인의 Codex CLI 로 돌리는 스크립트
(`scripts/astra_cli_bbox.py`)가 확보됐다. 이걸 **일회성 실험이 아니라 기존 추출 경로의 한 단계**로
넣는다. 대상은 **기존 subfigure 캡션이 있는 figure 만**이다 — 캡션이 없는 도판은 모델이 라벨을
지어내야 하므로 제외한다.

---

## 1. 기존 경로와 삽입 위치

```
.pdf ─(RunPod OCR)→ .pdf.json ─(regex)→ .pdf.extract.json ─(claude_augment, 호스트)→ .pdf.extract.claude.json
                                                                       │ figures[].subfigures[{label, description}]
                                                        (컨테이너 daily) sync_reference_figures → ReferenceFigure(subfigures, bbox)
                                                                       │
                                            ★ (호스트 cron) astra_subfigures.py → references/subfigures/<fig>/<key>.a1.json
                                                                       │
                                            ★ (컨테이너 daily) sync_reference_subfigures → ReferenceSubfigure 행
```

- **왜 호스트인가**: Codex CLI 는 claude CLI 와 같은 이유로 컨테이너에 없다(Node 패키지 + 로그인
  토큰 마운트). claude_augment 와 같은 레인 — **호스트는 파일만 쓰고, DB 는 컨테이너 명령이 쓴다**
  (dual-writer 금지, devlog 074).
- **왜 sync 이후인가**: 캡션 판정 기준이 `ReferenceFigure.subfigures`(DB) 이고 figure 이미지의
  정의(page·bbox)도 DB 행이다. 사용자가 bbox 를 고치면(`figure_update_bbox`) 이미지가 바뀌므로
  결과는 **그 시점의 소스 키**에 묶는다.

## 2. 실측 (2026-09-09 운영, 읽기 전용)

| | 건수 |
|---|---|
| ReferenceFigure (dismissed 제외) | 22,538 |
| subfigure 캡션 ≥ 1 | 11,754 |
| subfigure 캡션 ≥ 2 | 8,382 |
| 그중 bbox 있어 렌더 가능 | **4,182** (논문 1,066편) |
| 그중 지도(map) | 475 |
| bbox 없음 (렌더 불가) | 4,200 |

**bbox 없음 4,200 은 데이터가 아니라 sync 의 버그다.** `sync_reference_figures` 가 bbox 갱신을
`not obj.bbox_locked` 로 가드하는데 `bbox_locked` 기본값이 True 라, bbox 컬럼이 생기기 전에
만들어진 행은 **영원히 None** 이다. 잠금은 "사람이 만진 bbox 를 보호" 하려는 것이지 "빈칸을
보호" 하려는 것이 아니다 → **bbox 가 None 이면 잠금과 무관하게 채운다**(이 릴리스에 포함).
적용되면 후보가 ~8,000장으로 는다.

## 3. 처리량과 순서

CLI 1장 = 90~130초 실측(15470 93.6 s / 15866 129.7 s). 4,000장 × 100 s ≈ **110 시간 순수 CLI
시간**. 구독 한도는 미지수라 **10분마다 1장**(144/일)으로 시작하고 로그로 한도를 관측한 뒤 조정한다.
그러면 지도 제외 ~3,700장에 26일, bbox 수정 후 8,000장이면 두 달.

우선순위(기본): `figure_kind` **photo → unclassified → 나머지**, 지도(`map`)는 **기본 제외**
(분할 가치가 낮고 좌표맞춤엔 원본이 필요). 같은 kind 안에선 reference pk 오름차순(오래된 논문의
도판이 모식표본 도판일 확률이 높다). 수동 실행은 `--reference-ids/--figure-ids` 로 지정.

## 4. 구현

### 공유 로직 — `kprdb/services/figure_panels.py` (한 곳)
- `render_figure(fig, path, dpi=216)` — `export_figex_batch` 의 렌더를 여기로 이동(그 명령은
  import 로 전환). 실험 입력과 **같은 dpi·같은 clip** 이라 9/8 결과가 그대로 재현된다.
- `figure_source_key(fig, dpi)` — `page_no | bbox | dpi | PDF 크기` 의 sha256 앞 16자.
  결과 파일 이름이자 stale 판정 기준. `updated_at` 은 kind 확정만 해도 바뀌므로 안 쓴다.
- `candidate_figures(...)` — dismissed 제외 · subfigures ≥ `min_subfigures`(기본 2) · bbox 있음 ·
  PDF 있음 · kind 필터 · 우선순위 정렬.
- `result_dir(fig)` = `MEDIA_ROOT/references/subfigures/<fig_pk>/`.
  `find_result(fig)` — 현재 소스 키의 **완료 파일**(`<key>.a<n>.json`) 반환.
  `attempt_count(fig, key)` — `.run` 디렉터리 수. 최대 3회, 그 뒤엔 `--retry-errors` 로만.
- `sync_panels(fig, result_path)` — 트랜잭션 안에서 기존 행 삭제 → `ReferenceSubfigure` 재생성
  + `panel_sync` 메타. 소스 키가 같으면 no-op.

### 호스트 스크립트 — `scripts/astra_subfigures.py`
claude_augment 와 같은 계약: `/srv/fsis2026/scripts/.env` source → **`ADVANCED_FEATURES_SUBFIGURE_SPLIT=true`
아니면 exit 0** (전용 플래그 — AI 추출과 독립적으로 끌 수 있어야 한다) → flock
`/tmp/astra_subfigures.lock` → Django 읽기 전용(`PRAGMA query_only`) → 후보 → 렌더(임시 PNG) →
`astra_cli_bbox.extract()` → JSON. `--limit`(기본 1) `--dry-run` `--effort` `--timeout` `--min-subfigures`
`--include-maps` `--retry-errors` `--reference-ids` `--figure-ids`.
로그인 실패·CLI 부재는 **첫 건에서 중단**(한도 소진처럼 다음 건도 실패한다).

### 모델 — kprdb 0046
- `ReferenceSubfigure(figure FK related_name='panels', order, label, description, caption_index,
  bbox[px xyxy], bbox_normalized[permille], confidence)`. unique (figure, order).
- `ReferenceFigure.panel_sync` JSONField — `{source_key, model, method, image_width, image_height,
  is_compound, figure_kind, notes, elapsed_seconds, result_file, synced_at}`.
- `description` 은 sync 시점에 `caption_index` 로 `subfigures` 에서 복사(패널만 봐도 뜻을 안다).

### 컨테이너 명령 — `sync_reference_subfigures`
`subfigures/` 디렉터리를 훑어 figure 별 현재 소스 키의 완료 파일이 있고 `panel_sync.source_key` 와
다르면 반영. `--reference`, `--figure`, `--dry-run`. daily 17:50 UTC 라인에 추가.

### 화면
- `figure/<pk>/panel/<order>/image/` — 렌더 후 PIL crop(캐시). 기존 `figure_image` 권한 로직 공유.
- `task_page_figures` 응답에 `panel_count`.
- 워크스페이스 패널 타일 UI 는 **이 라운드 밖** — 먼저 실데이터가 쌓이고 나서.

## 5. 배포 순서

1. m710q 에서 빌드·배포(모델 마이그레이션 0046 + sync 수정). 운영은 `--reseed` 불필요.
2. 호스트: `git pull` (호스트 스크립트는 `~/projects/fsis2026` 체크아웃에서 돈다 — claude_augment 와 동일).
3. `/srv/fsis2026/scripts/.env` 에 `ADVANCED_FEATURES_SUBFIGURE_SPLIT=true`.
4. crontab 2줄:
   ```
   */10 * * * * cd /home/devops/projects/fsis2026 && DATABASE_PATH=/srv/fsis2026/db/db.sqlite3 MEDIA_ROOT=/srv/fsis2026/uploads /home/devops/venv/fsis2026/bin/python scripts/astra_subfigures.py --limit 1 >> /srv/fsis2026/backup/astra_subfigures.log 2>&1
   50 17 * * * docker exec fsis bash -c "python manage.py backfill_auto_classification && python manage.py sync_reference_figures && python manage.py sync_reference_subfigures"
   ```
5. 첫 하루 로그로 CLI 시간·한도 오류를 보고 주기 조정.

## 6. 하지 않는 것
- 캡션 없는 도판 분할(라벨 날조 위험) · 지도 분할(기본 제외) · crop 재검수 루프(모델에 crop 재전송) ·
  패널 ↔ 표본(RTS) 연결 — 다음 라운드. 패널 행은 그 FK 를 받을 자리다.
