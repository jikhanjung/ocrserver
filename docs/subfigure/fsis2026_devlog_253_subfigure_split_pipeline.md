# 253 — 도판 패널 분할을 추출 파이프라인에 편입 (P37 구현)

**날짜**: 2026-09-09
**계획**: `devlog/20260909_P37_subfigure_split_pipeline.md`
**상태**: 코드·테스트 완료(운영 호스트 kofhin 체크아웃, 커밋 5bb75d2, **미빌드·미배포**). 다음은 m710q 빌드.

## 무엇을 넣었나

9/8 실험용이던 Astra CLI 분할(`scripts/astra_cli_bbox.py`)을 기존 경로의 한 단계로 만들었다.
대상은 **`ReferenceFigure.subfigures` 에 캡션이 2개 이상 있는 figure 만**(지도 제외).

```
sync_reference_figures(컨테이너 daily) → ReferenceFigure(subfigures, bbox)
   → scripts/astra_subfigures.py(호스트 cron, Codex CLI) → references/subfigures/<fig>/<key>.a<n>.json
   → sync_reference_subfigures(컨테이너 daily) → ReferenceSubfigure 행 + ReferenceFigure.panel_sync
   → /kprdb/figure/<pk>/panel/<order>/image/
```

호스트는 파일만 쓰고 DB 는 컨테이너 명령이 쓴다 — claude_augment 와 같은 계약(devlog 074).

## 파일

| 파일 | 역할 |
|---|---|
| `kprdb/services/figure_panels.py` | **정의 한 곳**: 렌더(216dpi, 실험과 동일)·소스 키·후보 선정·결과 경로·`sync_panels`·`crop_panel` |
| `scripts/astra_subfigures.py` | 호스트 cron. `ADVANCED_FEATURES_SUBFIGURE_SPLIT=true` 아니면 exit 0. flock. **DB 읽기 전용**(`PRAGMA query_only`) |
| `kprdb/management/commands/sync_reference_subfigures.py` | 결과 파일 → 행. 파일 있는 figure 만 훑음. `--dry-run/--force/--reference/--figure` |
| `kprdb/models.py` + 마이그레이션 **kprdb 0046** | `ReferenceSubfigure`(figure FK `panels`, order, label, description, caption_index(es), bbox px, bbox_normalized, confidence) + `ReferenceFigure.panel_sync` |
| `kprdb/views_task.py`, `urls.py` | `figure_panel_image`(PDF 에서 다시 crop, 캐시 키에 source_key) · `task_page_figures` 에 `panel_count` · 권한 체크 `_figure_can_access` 로 공용화 |
| `kprdb/management/commands/export_figex_batch.py` | 렌더·메타 함수를 서비스로 이동(import) |
| `kprdb/management/commands/sync_reference_figures.py` | **bbox None 이면 잠금과 무관하게 채움**(아래) |
| `kprdb/tests.py` (+14) · `scripts/test_astra_subfigures.py` (+4) | 아래 |

## 설계에서 정한 것

- **소스 키** = `page_no | bbox | dpi | PDF 크기` 해시 16자. 결과 파일명이자 stale 판정 기준.
  사용자가 `figure_update_bbox` 로 bbox 를 고치면 키가 바뀌어 옛 결과는 자동으로 현재 이미지의
  것이 아니게 된다(`sync_panels` → `no_result`). `updated_at` 은 kind 확정만 해도 바뀌어 못 쓴다.
- **시도 = `.run` 디렉터리 수.** `astra_cli_bbox.extract` 가 기존 출력을 안 덮으므로 시도마다
  `a1, a2, …`. 3회 소진하면 `--retry-errors` 없이는 다시 안 부른다(구독 한도를 조용히 태우지 않는다).
  `.json` 이 있다 = 완료(실패는 `.run` 만 남는다).
- **치명 오류는 첫 건에서 멈춘다** — 로그인 만료·CLI 부재·한도(`is_fatal`). 다음 건도 똑같이 실패한다.
- **패널 이미지는 저장하지 않는다.** bbox 만 DB 에 두고 필요할 때 PDF 에서 다시 자른다(figure_image 와
  같은 방식). 렌더 크기가 결과와 다르면 비례 변환.
- **description 은 sync 시점에 복사** — `caption_index` 로 `subfigures` 에서. 패널 행만 봐도 뜻을 안다.
  캡션이 그새 바뀌어 인덱스가 범위 밖이면 그 인덱스만 버린다(파일 전체를 거부하지 않는다).
- 호스트 후보 조회는 **`panel_sync` 컬럼을 일부러 안 읽는다** — 그래서 0046 이 운영에 적용되기 전에도
  호스트 레인이 돈다(실제로 오늘 운영 DB 로 dry-run 했다).

## 실측 (운영 DB 읽기 전용, 2026-09-09)

`astra_subfigures.py --dry-run` → **후보 3,699** (photo 10 · unclassified 3,689 · 지도 475 제외).
첫 순서: fig 4713/4714(ref 2621, 8·16패널), 4910(ref 2654) … photo 가 먼저다.

**bbox 없는 4,200행** — `sync_reference_figures` 가 `not obj.bbox_locked` 로 bbox 갱신을 막는데
기본값이 True 라 bbox 컬럼 이전 행은 영원히 None 이었다. 잠금은 사람이 만진 값을 지키는 것이지
빈칸을 지키는 게 아니다 → None 이면 채운다. 배포 후 첫 daily 에 채워지고 후보가 ~8,000 으로 는다.
회귀 `SyncReferenceFiguresBboxFillTests` 2종(빈칸은 채움 / 값 있는 잠금은 보존).

## 테스트

- kprdb 신규 14: 서비스(키·후보·렌더·sync 수명주기·깨진 파일·crop 비례) / sync 명령(현재 키만 승격,
  깨진 파일이 sweep 을 안 멈춤) / 패널 이미지 뷰(302·200 PNG·404·학부생 403) / bbox 채움 2 /
  호스트 스크립트(`extract` mock — 결과·메타 배치, 재실행 skip, 시도 소진, 치명 전파).
- scripts pytest +4 (가드 fail-closed · env 로더 · fatal 마커). **scripts/ 57 passed.**
- `manage.py check` OK, `makemigrations --check` 무변.
- ⚠️ kofhin 에서 kprdb 전체 스위트를 돌리면 **무관한 ERROR 다수** — `Missing staticfiles manifest
  entry for 'geo-theme.css'`(이 호스트엔 collectstatic 산출물이 없다). 이 라운드 변경과 무관.
  전체 게이트는 m710q `build.sh` 에서.

## 배포 절차 (m710q)

1. `./deploy/build.sh 0.6.29` → `./deploy/remote-prod.sh 0.6.29` (마이그레이션 kprdb 0046. ghdb 도
   kprdb 를 INSTALLED_APPS 에 두므로 다음 ghdb 배포 때 같이 적용 — 지금 ghdb 배포는 불필요).
2. kofhin `~/projects/fsis2026` 에서 `git pull` (호스트 스크립트는 체크아웃에서 돈다).
3. `/srv/fsis2026/scripts/.env` 에 `ADVANCED_FEATURES_SUBFIGURE_SPLIT=true`.
4. crontab (**PATH 필수** — codex 는 node 스크립트이고 nvm 은 비대화형 셸에 안 붙는다.
   빠뜨리면 "ChatGPT login required" 로 잘못 보고된다. devlog 254 §1):
   ```
   */10 * * * * cd /home/devops/projects/fsis2026 && PATH=/home/devops/.nvm/versions/node/v24.16.0/bin:$PATH DATABASE_PATH=/srv/fsis2026/db/db.sqlite3 MEDIA_ROOT=/srv/fsis2026/uploads /home/devops/venv/fsis2026/bin/python scripts/astra_subfigures.py --limit 1 >> /srv/fsis2026/backup/astra_subfigures.log 2>&1
   ```
   그리고 17:50 라인 끝에 `&& python manage.py sync_reference_subfigures`.
5. 첫 하루 로그(`astra_subfigures.log`)로 CLI 시간·한도 오류를 보고 주기 조정.
   1장 90~130 s × 3,700장 ≈ 110 시간 → 10분 1장이면 26일.

## 남긴 것
- 워크스페이스 패널 타일 UI · 패널 ↔ 표본(RTS) 연결 · crop 재검수 루프 — P37 §6.
- 커밋은 안 했다(사용자 확인 후).
