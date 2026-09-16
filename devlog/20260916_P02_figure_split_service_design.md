# P02 — ocrserver 에 도판(figure) 분할 서비스 얹기: 서버 설계

**작성**: 2026-09-16 (같은 날 두 번 개정 — 사용자 결정 반영, 그리고 PaperMeister P16/P17 과 정합)
**상태**: 계획서, 미착수
**짝 문서**: PaperMeister `devlog/20260914_P16_Figure_Panel_Split.md` §6(서버 명세 초안) ·
`devlog/20260916_P17_…md` §3.1(명세 v2) · `docs/figure_pipeline_client_plan.md`(클라이언트 계획, 2026-09-16).
**이 문서의 위치**: P16 §6 이 서버 명세의 원본이다. 여기는 그것을 ocrserver 관점에서 받아 적고, 오늘 결정으로 달라진
것(§0)과 ocrserver 내부 구현만 더한다. 두 문서가 어긋나면 **P16 §6 → P17 §3.1 → 이 문서** 순으로 최신 것이 이긴다.

---

## 0. 사용자 결정 (2026-09-16) 과 P16 대비 달라진 것

| # | 결정 | P16 §6 대비 |
|---|---|---|
| D1 | ③ 패널 분할은 gpt-6-astra(Codex CLI) 만 | 같음 |
| D2 | **② 캡션 연결·분할도 Astra** | **변경**: `claude -p`(Opus 5) 안 씀. 워커는 `codex` 하나만 |
| D3 | 도판 추출(①)은 클라이언트 | 같음 |
| D4 | ①이 의심스러운 도판은 Astra 가 재판정하되, **주변 쪽을 더 볼지·텍스트 전체를 훑을지는 Astra 가 스스로 판단** | **추가**: `POST /figures/detect` + **논문 작업 폴더**(§3.3). ②도 같은 폴더를 쓴다 |
| D5 | P17 §3.2 채택 — **프롬프트·스키마는 요청에 실려 온다** | 서버는 스키마 검증만. 워커에 도메인 프롬프트 없음 |
| D6 | P17 §3.5 채택 — `panel_key` 에 entries 없음 | 서버 dedup 키도 같은 원칙(입력의 정체만) |
| D7 | ② **Astra 확정**, Opus 복귀 없음 | 워커는 `codex` 만. `claude` CLI 경로를 두지 않는다 |
| D8 | 호출 속도 **5분에 1건**으로 시작 | `FIGURES_MIN_INTERVAL=300` (초, 단계 무관, 워커 전역). 일일 상한은 두지 않는다 |

그리고 처음 초안(오늘 오전)에서 P16 을 읽은 뒤 버린 것: `/split` 이라는 별도 이름 · 1-based 쪽 번호 · 패널 좌표를 페이지
permille 로 환산해 주기 · wrapper 컨테이너 안에서 codex 호출. 전부 P16 §6 을 따른다 — 0-based, 패널은 도판 이미지 기준
0..1000, **codex 는 호스트 워커**(컨테이너에 Node·로그인 토큰이 없다).

---

## 1. 조사에서 확정된 사실 (설계에 쓰이는 것만)

- wrapper 는 **원본 PDF 를 보관**한다: `/srv/ocrserver/data/pdfs/{sha256}.pdf` (`wrapper/main.py:931-938`).
  단 P16 §2.3: RunPod 시절 OCR 한 논문은 없을 수 있다 → `HEAD /pdfs/{hash}` + `POST /pdfs`.
- chandra 의 `data-bbox` 는 페이지 기준 0–1000, 좌상단 원점. 클라이언트(`ocr_layout.py`)가 이미 파싱한다. 서버는 안 한다.
- 이 호스트에 `codex` 0.154.0 이 `/usr/local/bin/codex`, ChatGPT 로그인 상태. `codex exec --image` 는 **여러 장 받는다**
  (`-i, --image <FILE>...`, 2026-09-16 확인) → D4 의 앞뒤 쪽 이미지를 한 호출에.
- `scripts/subfigure/astra_cli_bbox.py`(fsis 사본) 가 ③ 호출부. `astra_panels.py` 의 PROMPT/SCHEMA/validate 는
  클라이언트가 프롬프트를 실어 보내기로 하면(P17 §3.2) 서버에선 **스키마 검증만** 남는다.
- wrapper 잡 테이블은 OCR 전용(`_run → _ocr_page`). `_FairScheduler`·`_mupdf_lock` 은 재사용. GPU 와 무관한 잡이라
  모드(`2ocr`/`llm+ocr`)·`_mode_switching` 에 막히면 안 된다(P16 §6.1-3).
- PaperMeister 는 PyMuPDF 가 아니라 **pypdfium2** 로 갈아탔다(2026-08-13, GPL-3.0). 클라이언트 렌더는 `pdfdoc.render_page`.
  서버 렌더는 계속 PyMuPDF(`_mupdf_lock` 아래).

---

## 2. 구조 — P16 §6.1a 그대로

```
PaperMeister ──HTTP──▶ wrapper (컨테이너)               잡 접수·상태·결과. SQLite 의 유일한 writer
                          ▲   │ 내부 API (docker 네트워크·localhost 만)
                          │   ▼
                figures-worker (호스트, systemd, jikhanjung)   claim → PDF 렌더(PyMuPDF) → codex exec → result
```

- 워커는 SQLite 를 만지지 않는다. `POST /internal/figures/claim` → `POST /internal/figures/{job_id}/items/{key}/result`
  → heartbeat. wrapper 가 heartbeat 끊김을 보면 item 을 `queued` 로 되돌린다.
- 워커는 **wrapper 이미지와 같은 git 태그**에서 실행(P16 §1.3 "옛 체크아웃" 사고). `scripts/figures_worker.py` 를 저장소에 두고
  systemd 유닛은 `scripts/systemd/`(기존 `ocrserver-metrics` 와 같은 자리).
- PATH 에 `codex` 명시(systemd 비대화형, P16 §6.5). 하위 프로세스 env 에서 `OPENAI_API_KEY`/`CODEX_API_KEY`/`ANTHROPIC_API_KEY` 제거.
- 렌더는 워커가 `PDF_DIR` 을 **읽기 전용 bind** 로 본다. 워커 프로세스는 하나라 MuPDF 스레드 레이스(devlog 044)는 없지만,
  같은 이유로 **워커 안에서도 렌더는 직렬**.

---

## 3. 엔드포인트 — P16 §6.2 + detect

```
HEAD /pdfs/{file_hash}            200 | 404
POST /pdfs                        multipart file (+client_id) → {file_hash}

POST /figures/detect              → {job_id}        ①′ 재판정 (도판 단위, Astra + 쪽 이미지)     ← 추가
POST /figures/link                → {job_id}        ② 연결·분할 (논문 단위, Astra 텍스트)
POST /figures/panels              → {job_id}        ③ 패널 (도판 단위, Astra 도판 이미지)
GET  /figures/{kind}/{job_id}     → job
GET  /figures/jobs?client_id=     → 목록
POST /figures/{kind}/{job_id}/resume                 치명 정지 후 운영자 재개
```

모든 요청에 `client_id`. **P17 §3.2 를 채택하면** 세 요청 모두 `prompt: {version, instructions, schema}` 를 싣고,
서버는 도메인을 모른다 — 요청 종류가 정하는 것은 **워커가 무엇을 렌더해 `--image` 로 붙이느냐** 뿐:

| kind | 렌더 | 쪽 번호 |
|---|---|---|
| detect | **논문 작업 폴더**(§3.3): 대상 쪽 이미지에 힌트 상자를 그려 `--image` 로 붙이고, 나머지는 Astra 가 폴더에서 연다 | 0-based |
| link | **논문 작업 폴더** 텍스트만. `--image` 없음. 설명 쪽은 Astra 가 찾아 읽는다 | 0-based |
| panels | `bbox_page_1000` 크롭 216dpi (fsis 와 동일) | 0-based |

### 3.1 `/figures/link` · `/figures/panels`

P16 §6.3·§6.4 + P17 §3.1 명세 v2 그대로. 달라지는 것은 `options.model` 기본이 `gpt-6-astra` 이고 link 도 `codex exec` 라는 것.
응답 스키마(`name`·`caption_pages`·`continuation_of`·`printed_label`·`non_compound_reason`·`annotation_indices`)는
클라이언트가 보낸 `prompt.schema` 가 정한다 — 서버는 **그 스키마로 검증**하고 통과한 JSON 을 그대로 돌려준다.

### 3.2 (삭제 — 초안의 `/split` 은 §3.1 로 흡수)

### 3.3 `/figures/detect` · `/figures/link` — 논문 작업 폴더를 주고 Astra 가 돌아다닌다

요청·응답 모양은 PaperMeister `docs/figure_pipeline_client_plan.md` §2.2·§2.3·§3 이 원본. 서버는 **쪽을 고르지 않는다** —
논문 전체를 폴더로 만들어 Codex 세션의 작업 디렉터리로 주고, 지시문이 "대상 쪽에서 시작해 필요하면 앞뒤 쪽을 열고,
필요하면 텍스트 전체를 검색하라" 고 한다. Codex CLI 는 원래 에이전트라 `cat`/`grep`/이미지 열기를 스스로 한다.

**작업 폴더** `/data/figure_ws/{file_hash}/` (한 번 만들면 TTL 7일 캐시, 같은 논문의 여러 item 이 공유):
```
README.txt          쪽 수 · 파일 규칙 · "쪽 번호는 0-based" 한 줄
text/p013.txt       쪽별 OCR 텍스트 — wrapper DB `pages.markdown` 에서 HTML 태그를 벗긴 것 (클라이언트가 보낼 필요 없음)
text/all.txt        전체 텍스트, 쪽 경계 마커 `=== page 13 ===` — grep 용
pages/p013.png      쪽 전체 100dpi 렌더 (PyMuPDF, `_mupdf_lock`, 워커 직렬)
item/figure.json    이 item 의 힌트(상자·이름·캡션 블록·플레이트 쪽) + reasons
item/target.png     대상 쪽 150dpi + 힌트 상자 빨강 (이건 `--image` 로도 붙인다)
```
- 렌더는 **미리 전부**(300쪽 ≈ 90 s, 100dpi ≈ 0.4MB/쪽) 한다. 세션 중 렌더 요청을 받으려면 `--sandbox workspace-write` 와
  헬퍼 스크립트가 필요한데, 읽기 전용 샌드박스가 더 안전하고 단순하다. 폴더는 논문당 한 번이라 detect 여러 건·link 가 나눠 쓴다.
- 호출: `codex exec -C /data/figure_ws/{hash} --sandbox read-only --model gpt-6-astra -i item/target.png --output-schema … -`.
  fsis `astra_cli_bbox.py` 의 "Do not read other files" 지시는 **detect/link 에서는 반대로** 뒤집는다. panels 는 그대로(도판 한 장만).
- **확인할 것(1 단계)**: Codex 에이전트가 세션 중 폴더의 PNG 를 스스로 열어 볼 수 있는지(`view_image` 류 도구). 안 되면
  detect 는 `-i` 로 대상 쪽 ±2 를 미리 붙이고 텍스트만 폴더에서 훑는 절충으로.
- **비용 상한**: 세션 타임아웃(detect 10분 · link 20분 초기값, `run_command` 의 프로세스 그룹 kill) + 지시문에 "먼저 텍스트를
  grep 해 후보 쪽을 고르고, 이미지는 후보만 열어라" — 300쪽을 전부 보게 두지 않는다. 상한에 걸리면 item `budget_exhausted`
  (실패·시도 소진과 구분). 결과의 `pages_consulted` 와 CLI usage 를 `figure_calls` 에 기록해 실제 비용을 본다.
- dedup 키 = `file_hash|page|hint_bbox|prompt digest` (입력의 정체만. 어디를 봤는지는 키가 아니다).
- 소요·한도는 **미실측**. 첫 배포에서 잰다 — 에이전트 세션이라 panels 보다 편차가 클 것.

## 4. 내부

- **테이블**: `figure_jobs(job_id, kind, client_id, file_hash, status, submitted_at, completed_at, prompt_version, error,
  resume_count)`, `figure_items(job_id, key, status, attempts, request_json, result_json, error, claimed_by, heartbeat_at,
  completed_at)`. 기존 `jobs`/`pages` 는 건드리지 않는다(OCR 경로 무변).
- **dedup**: `(file_hash, client_id, kind, item 입력 digest, prompt digest)` 가 done 이면 재사용, `force` 로 우회 (P16 §6.5).
- **스케줄**: kind 무관하게 워커 큐 하나, **동시 1**(codex 직렬). client_id 공평 분배는 `_FairScheduler` 인스턴스 하나 더.
- **재시도·치명 정지**: item 3회. 치명(`login required`·`Codex CLI not found`·`usage limit`·`rate limit`, stdout+stderr 양쪽)은
  **시도로 세지 않고** 워커를 멈추고 잡을 `paused` 로. 한도 해제 시각은 문구에서 읽되 박아두지 않는다(P16 §6.5).
  운영자가 `codex login` 후 `/resume`.
- **사용량 기록**: 호출마다 kind·model·prompt_version·CLI usage·경과를 `figure_calls` 에(`llm_requests` 와 같은 방식).
- **호출 간격** `FIGURES_MIN_INTERVAL=300`(초, D8): 워커가 `codex exec` 를 한 번 끝낸 뒤 다음 호출까지 최소 300 초 대기.
  단계(detect/link/panels) 무관 전역. 하루 ≈ 288건 상한이 자연히 생긴다. 로그(`figure_calls`)를 보며 `.env` 로 조정.
- **`/status`**: figures 큐 카드(대기·처리·오늘 호출·마지막 치명 오류·워커 heartbeat).
- **TTL**: 결과 30일(P16 §6.5). 진실의 원천은 클라이언트 DB.
- **속도 현실**: detect·link 는 에이전트 세션이라 미실측(상한 10/20분) · panels 도판당 90–130 s(fsis 초기)→15–30 s(운영). 전부 직렬.
- **작업 폴더 디스크**: 논문당 100dpi 전 쪽 ≈ 300쪽 120MB. TTL 7일, `/data` 는 루트 LV(82GB 여유) — 상한 20GB 넘으면 오래된 것부터.

---

## 5. 하지 않을 것

- 서버 쪽 레이아웃 파싱·규칙 기반 도판 판정·의심 사유 판정 (D3, 클라이언트 `figures.py`).
- Astra 외 모델·백엔드 교체 계층 (D1·D2·D7). `options.model` 은 받되 워커는 `codex` 만 안다. `claude` CLI 경로 없음.
- 서버 → 클라이언트 콜백. 폴링 + `GET /figures/jobs` 회수.
- 패널 이미지 서버 저장. bbox 만.
- 새 chandra 이미지 빌드.

---

## 6. 작업 순서·규모

| 순서 | 내용 | 규모 |
|---|---|---|
| 0 | **클라이언트 G 단계 대기** — 명세 v2 + 프롬프트 3벌 + detect 요청 모양이 넘어와야 시작 | — |
| 1 | wrapper 0.3.0: `/pdfs`, `figure_jobs`·`figure_items`, `/figures/*` 접수·조회·resume, 내부 claim/result/heartbeat, dedup, 공평 분배, `/status` 카드 | 2일 |
| 2 | `scripts/figures_worker.py` + systemd: claim 루프, **작업 폴더 빌더**(DB 텍스트 + 전 쪽 렌더 + 힌트 상자), `codex exec -C` 래퍼(`astra_cli_bbox.run_command` 재사용), 스키마 검증, 세션 상한, 치명 정지, 사용량 기록, 일일 상한 | 2–3일 |
| 3 | 검증: fsis 파일럿 도판 5장 panels → fsis Astra 결과와 패널 수·bbox 비교(같은 모델·프롬프트라 일치해야) · detect 는 devlog 261 "본문이 도판" 14건 + P38 플레이트 설명 사례 · link 1편 시간·`pages_consulted`·usage 측정 · **Codex 가 폴더 이미지를 열 수 있는지 먼저** | 1일 |
| 4 | 운영: 로그인 만료 알림, `FIGURES_MIN_INTERVAL` 조정, HANDOFF·WRAPPER_API 문서 | 반나절 |

---

## 7. 결정 기록 (2026-09-16, 사용자)

P17 🔴 2건 채택(D5·D6) · ② Astra 확정(D7) · 호출 5분에 1건(D8). 남은 확인은 워커 계정·systemd 유닛 형태뿐이며 구현 시 정한다
(jikhanjung, `ocrserver-metrics` 와 같은 방식이 기본).
