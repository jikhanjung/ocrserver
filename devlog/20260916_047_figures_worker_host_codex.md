# 047 — 호스트 워커 `figures_worker.py`: Codex/Astra 실행기 (P02 2단계) + wrapper 0.3.1

**날짜**: 2026-09-16
**상태**: 코드·e2e 검증 완료, wrapper 0.3.1 배포됨. **systemd 유닛 설치는 sudo 라 사용자 몫** (아래).
**앞 기록**: devlog 046 (API 뼈대) · P02 §2·§3.3

## 무엇을

`scripts/figures_worker.py` (호스트, jikhanjung, 단일 프로세스) — claim → 준비 → `codex exec` → result 루프.

| 단계 | 내용 |
|---|---|
| claim | `POST /internal/figures/claim` (loopback→nginx→wrapper, `X-Worker-Token`). 빈 큐면 30 s 후 다시. 서버가 `paused` 면 그대로 대기 |
| 로그인 확인 | 호출마다 `codex login status` (30 s). 아니면 `fatal` |
| 작업 폴더 (detect·link) | `/srv/ocrserver/figure_ws/{hash}/{ocr_digest}/` — 내부 API 로 받은 클라이언트 텍스트를 `text/pNNN.txt`(블록마다 `[Label x0 y0 x1 y1] text`, 그림 블록은 `[image: alt]`) + `text/all.txt`, PDF 전 쪽 100dpi `pages/pNNN.png`, `README.txt`(0-based 명시). `.ready` 마커로 논문당 한 번 |
| detect 입력 | 대상 쪽 150dpi + 힌트 상자 빨강 + "page N (0-based)" 라벨 → `items/<id>/target.png`, `--image` 로도 첨부. `-C` 는 폴더 루트 |
| panels 입력 | fsis `render_figure` 와 같은 permille×page.rect clip 렌더, `options.dpi`(216) + 긴 변 4000px 상한. `-C` 는 항목 폴더, `--image figure.png`. INPUT 에 `image_width/height, original_caption, existing_subfigures` 도 넣어 fsis 프롬프트가 그대로 돈다 |
| 호출 | `codex exec --ignore-user-config --ephemeral --skip-git-repo-check --sandbox read-only --model M -c model_reasoning_effort=E [-i …] --output-schema schema.json --output-last-message response.json --json -`, stdin = `instructions` + `=== INPUT (JSON) ===` + JSON. 프로세스 그룹, 환경에서 `OPENAI_API_KEY`/`CODEX_API_KEY`/`ANTHROPIC_API_KEY` 제거 |
| 판정 | 치명 마커(stdout+stderr, 노이즈 줄 제외) → `fatal` · 타임아웃(detect 600/link 1200/panels 600 s) → `budget_exhausted` · exit≠0 / turn.failed / JSON 아님 / 스키마 위반 → `failed` · else `done` |
| 스키마 검증 | 호스트에 `jsonschema` 없음 → type/required/properties/items/enum 만 보는 60줄 검증기 |
| heartbeat | 세션 중 60 s 마다. usage 는 `turn.completed` 이벤트 합산 |
| 간격 | 결과 후 서버가 알려준 `min_interval_s`(300) 만큼 `sleeping` (next_call_at 보고) |
| 정리 | 시작 시·하루 한 번 7일 지난 작업 폴더 삭제 |

산출물은 `…/items/<id>/run/a<시도>/{prompt.txt, schema.json, response.json, events.jsonl, stderr.log, run.json}` 에 남는다.

## e2e (라이브 wrapper, kruskal1964.pdf, client_id `claude-e2e-047`)

클라이언트 역할로 OCR 잡 `d460a98f` 의 쪽 텍스트를 `POST /figures/workspace`(`ocr_digest a5421b0a…`) 로 올리고 두 잡을 제출, 워커를
포그라운드로 돌렸다.

| kind | 프롬프트 | 결과 | 시간 | 토큰 |
|---|---|---|---|---|
| panels (p3, bbox 335 588 662 756, caption "FIGURE 1", entries []) | fsis `astra_cli_bbox.INSTRUCTIONS` + `astra_panels.SCHEMA` 그대로 | `is_compound=false, figure_kind=chart`, 패널 1개 전체, note "crop 이 오른쪽·아래에서 잘림" | 70.3 s | in 13.8k / out 85 |
| detect (p3, 힌트 335 **560** 662 756 — 일부러 위로 늘린 상자, reason no_caption) | 임시 detect 프롬프트 + 스키마 (P02 §4.5 요지) | `verdict=adjusted`, bbox **[337,591,665,757]**, caption "FIGURE 1" `same_page` p3, `pages_consulted=[3]`, note "Tightened the box to the graph boundary" | 95.2 s | in 31.0k(캐시 14.8k) / out 153 |

- detect 의 보정 상자는 chandra 의 `335 588 662 756` 과 3‰ 안 — 힌트가 틀려도 이미지를 보고 바로잡는다. Astra 가 실행한
  명령은 `cat README.txt; cat text/p003.txt` 뿐 — 필요한 만큼만 읽었다.
- 첫 시도의 실패 한 건: 서버 claim 응답의 `pdf_path` 가 **컨테이너 경로**(`/data/pdfs/…`) 라 워커가 `pdf_missing` 으로 실패
  (시도 1회 소모, 재큐). 워커는 자기 `PDF_DIR` 로 경로를 만들도록 고치고, wrapper 0.3.1 에서 `pdf_path` 를 응답에서 뺐다.
- `page_text` 가 `<img alt>` 만 있는 Figure 블록을 빈 본문으로 버리던 버그도 e2e 전에 잡았다 (alt 를 `[image: …]` 로 살림).
- 워커 stderr 노이즈(`failed to refresh available models`, `backend-api/ps/mcp`) 는 예상대로 매 호출 찍히고 무해.

## wrapper 0.3.1

claim 응답에서 `pdf_path` 제거 (위). 스모크 56 checks. 07:43 의 0.3.0 → 08:31 UTC 0.3.1 재생성. Hub `0.3.1`+`latest`.

## 설치 (sudo, 사용자)

```bash
sudo cp /home/jikhanjung/projects/ocrserver/scripts/figures_worker.py /srv/ocrserver/scripts/
sudo cp /home/jikhanjung/projects/ocrserver/scripts/systemd/ocrserver-figures-worker.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ocrserver-figures-worker.service
journalctl -u ocrserver-figures-worker -f      # "figures_worker 0.3.0 id=jikhanserver …" 가 보이면 됨
```
`/srv/ocrserver/scripts` 가 root 소유라 cp 도 sudo. 유닛은 `EnvironmentFile=/srv/ocrserver/.env`(토큰) + `User=jikhanjung`(codex 로그인).
확인: `/status` 카드가 "대기 (idle)" 로 바뀌고 `GET /api/figures` 의 `worker.worker_id` 가 호스트명.

## 추가 (같은 날 밤) — wrapper 0.3.3: 종료 시 즉시 재큐

- `POST /internal/figures/items/{id}/release` — 처리 중 항목을 **시도 환불**하며 큐로 되돌린다. 워커는 SIGTERM 을 받으면 codex
  프로세스 그룹을 죽이고 release 를 보낸 뒤 종료한다. 실측: 40 s 진행 중이던 세션에 SIGTERM → 4 s 안에 `release`, 다음 워커가
  attempt=1 로 다시 집었다. → **재시작 시점을 가릴 필요가 없어졌다.** 스모크 60 checks.
- 0.3.2 (같은 날): detect 항목 쪽 단위 `hint_boxes[]` (PaperMeister 099 §4).

## 추가 — PaperMeister G 수령과 계약 테스트 (같은 날 밤)

PaperMeister `a77bd64` 가 `docs/figure_server_spec_v2.md` + `papermeister/figure_prompts/{detect,link,panels}.{md,schema.json}` 을
넘겼다(wrapper 0.3.2 전송 형식에 맞춰 씀). `wrapper/tests/contract_spec_v2.py` 가 그 **실제 파일**(복사하지 않고 `PM_PROMPTS_DIR`
로 마운트)로 명세 §4–§6 의 요청을 그대로 만들어 제출·claim 하고, 명세의 예시 응답 3종이 워커의 스키마 검사를 통과하는지,
프롬프트를 고치면 `version` 이 바뀌어 dedup 이 풀리는지 본다. **27 checks passed.** 서버·워커 변경 없음 — 맞았다.
실행: `docker run --rm -e FIGURES_WORKER_TOKEN=t -e PM_PROMPTS_DIR=/pm_prompts -v $PWD/wrapper:/app -v $PWD/scripts:/worker_scripts:ro
-v <prompts>:/pm_prompts:ro -w /app honestjung/ocrwrapper:0.3.3 sh -c 'pip install -q requests pillow; python -m tests.contract_spec_v2'`

## 추가 — panels 파일럿 선행 실행 (같은 날 밤, 클라이언트 H 전)

라이브 OCR 코퍼스(PaperMeister 잡)에서 "같은 쪽 캡션에 하위 라벨 4개 이상" 인 큰 Figure 블록 6장을 골라(전부 형태계측·계통 그래프류,
화석 플레이트는 아님) fsis 프롬프트로 panels 잡을 제출, 분리 실행 워커가 5분 간격으로 처리했다. 첫 항목 도중 SIGTERM 으로 release 도 실증.

| 도판 | kind | compound | 패널 | 초 |
|---|---|---|---|---|
| Hughes+ 1999 p8 (계통수) | diagram | **false** — "1–9 는 형질 변화 번호, 패널 아님" | 1 | 70 |
| Álvaro+ 2018 p14 (CVA) ×2 판본 | chart | true | (a)–(e) ×2, 공유 범례 제외 | 81·82 |
| Webster 2011 p27 | mixed | true | 1–5 | 70 |
| Tanabe+ 2015 p4 (relative warp) | chart | true | A–D, TPS 격자 포함 | 70 |
| Hopkins & Webster 2009 p20 (PCA) | chart | true | 1–5 | 88 |

6/6 done, 실패 0, 도판당 70–88 s. 캡션의 하위 라벨 형식(괄호 소문자·대문자·숫자)을 그대로 읽었고 공유 범례를 패널로 안 세는 판단이
프롬프트대로 나온다. 상자·크롭 시트는 Artifact 로 사용자에게 (세션 산출물). 워커 산출물 `/srv/ocrserver/figure_ws/<hash>/panels/…`.

## 추가 — 첫 실제 호출 (2026-09-17 00:20 UTC, PaperMeister H) 와 상한 조정

PaperMeister `papermeister-7355a25d` 가 link 잡 30편을 넣었다. 첫 편 Westergård(46쪽 펼침 스캔, 도판 24·플레이트 12):
**968.6 s**, 입력 417k 토큰(캐시 346k) / 출력 26.8k, 24/24 도판·항목 223·skipped 0. Astra 는 `rg` 1회로 설명 쪽을 찾고 `cat` 3회로
p33–45·p3·4·26·27 을 읽었다. 펼침 스캔의 마주보는 면 설명을 `facing_page` 로, 딧토(") 표기를 펼쳐 항목화, 파서의 `text_as_figure`
의심 둘을 "지층 그림 맞음" 으로 되돌림. 두 번째 편(6쪽) 은 몇 분.

**상한 조정**: 46쪽에 16분이면 100쪽 넘는 모노그래프는 1200 s 상한에 걸린다 → `.env` `FIGURES_SESSION_TIMEOUT_LINK=3600`,
서버 `FIGURES_HEARTBEAT_TIMEOUT=4200`(compose 에 변수 추가, 세션 상한보다 길어야 재큐가 안 난다). 적용은 큐가 도는 중이라
**항목 사이 `sleeping` 창**에서: wrapper `--no-deps --force-recreate`(3 s) + 워커에 SIGTERM(내 계정 프로세스라 sudo 불필요,
systemd `Restart=always` 가 30 s 안에 새 `.env` 로 재기동). 항목 손실 0. 워커 재시작 후 `/proc/<pid>/environ` 으로 값 확인.

관찰: 재시작한 워커는 남은 5분 간격을 건너뛰고 바로 claim 했다 — 간격이 프로세스 메모리에만 있다. 서버가 claim 응답에 주는
`worker.next_call_at` 을 기동 시 존중하면 된다(다음 워커 수정 때).

## 추가 — 6시간 실사용에서 나온 워커 버그 둘 (2026-09-17 08:00 UTC)

link 30편 큐를 7시간 돌린 뒤: done 18 · budget_exhausted 1 · 대기 12. 문제 둘.

1. **재연결 알림을 실패로 오판.** 110쪽 논문 두 시도(각 47분)가 exit 0·`turn.completed`·유효한 답(도판 6, 항목 263/269)이었는데,
   중간의 `{"type":"error","message":"Reconnecting... 2/5 (idle timeout waiting for websocket)"}` 이벤트를 워커가 실패 사유로 세어
   `failed` → 재큐 → 3차 시도 중이었다. **답 두 개(94분·43만 토큰) 를 버린 것.** 1차 답을 내부 API `result` 로 직접 올려 살렸다.
   고침: `turn.failed` 만 실패. `error` 는 "reconnect" 문구면 무시(횟수만 기록), 그 외 `error` 도 `turn.completed` 가 있으면 무시.
2. **스트림 멈춤이 세션 상한(3600 s)을 통째로 태움.** 78쪽 논문 한 시도는 명령 2개 뒤 stdout 이 멈춘 채 3600 s 를 채워
   `budget_exhausted`. 같은 논문의 다른 시도는 6분. 이 망의 웹소켓 stall 이고 모델 탓이 아니다.
   고침: stdout 을 파일로 스트리밍하며 **`FIGURES_IDLE_TIMEOUT=900` s 동안 새 출력이 없으면 kill → `failed`(재시도 가능)**.
   하드 상한은 그대로 `budget_exhausted`.

덤: 재시작 시 남은 간격을 `figure_ws/.next_call_at` 로 기억해 건너뛰지 않게 했다. 가짜 codex 로 4 경로(재연결→done · stall→failed 6 s ·
상한→budget · turn.failed→failed) 검증.

또 하나 관찰: 같은 78쪽 논문이 같은 초에 **세 잡**으로 들어왔다(항목 내용이 달라 dedup 안 걸림). 클라이언트 레인 쪽 중복 제출로
보이며 서버 문제는 아니다 — PaperMeister 에 전달.

## 남은 것

- detect·link 의 **실제 프롬프트**는 PaperMeister G 단계에서 온다. 여기서 쓴 detect 프롬프트는 e2e 용이며 저장소에 두지 않았다.
- `pages_consulted` 는 스키마에 있어야 온다 (클라이언트 계획 §10.3).
