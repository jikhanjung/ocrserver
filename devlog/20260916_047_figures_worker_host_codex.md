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

## 남은 것

- detect·link 의 **실제 프롬프트**는 PaperMeister G 단계에서 온다. 여기서 쓴 detect 프롬프트는 e2e 용이며 저장소에 두지 않았다.
- `pages_consulted` 는 스키마에 있어야 온다 (클라이언트 계획 §10.3).
