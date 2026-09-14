# 도판(figure) → 패널(subfigure) 분할 — Astra / Codex CLI

fsis2026 저장소(커밋 `79a9081`, 2026-09-14 시점)에서 가져온 도판 분할 도구와
그 문서 묶음. 도판 이미지 하나와 (이미 항목별로 쪼개진) 캡션을 `gpt-6-astra` 에
보내 패널별 픽셀 bbox 를 JSON 으로 받는다. 모델 호출은 **Codex CLI 의 ChatGPT
로그인 경로**라 API 키가 필요 없다.

이 호스트 기준 상태 (2026-09-14): `codex` 0.154.0 이 `/usr/local/bin/codex` 에
있고 ChatGPT 로그인돼 있음. Pillow 12.2 / requests 2.34 시스템 파이썬에 설치됨.

## 무엇이 어디에 있나

```
scripts/subfigure/
  astra_cli_bbox.py        ← 진입점. 이미지 + 캡션 → bbox JSON (Codex CLI 호출)
  astra_panels.py          ← 프롬프트·JSON 스키마·검증·픽셀 변환. OpenAI Responses API
                              직접 호출 러너이기도 함 (실험용, 키 필요)
  test_astra_cli_bbox.py   ← 위 둘의 단위 테스트 (CLI/로그인 mock). 16 passed
  test_astra_panels.py
  reference/
    astra_subfigures.py    ← fsis2026 운영 cron 레인. Django ORM(kprdb) 에 묶여 있어
    figure_panels.py          여기서는 안 돎. 후보 선정·216dpi 렌더·소스 키·시도 횟수
                              규칙을 볼 때 참고
docs/subfigure/
  astra_cli_bbox.md                          ← CLI 단건 실행 사용법 (가장 먼저 볼 것)
  astra_panel_extraction.md                  ← 2026-09-08 API 직접 호출 실험 기록 (운영 경로 아님)
  fsis2026_devlog_252_figure_models_and_astra_cli.md  ← FigEx/Astra/Sol/Terra/Luna 비교, CLI 러너 도입
  fsis2026_devlog_253_subfigure_split_pipeline.md     ← 파이프라인 편입 기록, 운영 cron 라인
  fsis2026_P37_subfigure_split_pipeline.md            ← 편입 계획서
```

`docs/subfigure/*.md` 는 **원문 그대로**다. 본문의 `scripts/astra_cli_bbox.py`,
`~/venv/fsis2026`, `.cache/...`, `kprdb.*`, `sync_reference_*` 같은 경로·이름은 모두
fsis2026 쪽 것이다. 이 저장소에서는 `scripts/subfigure/astra_cli_bbox.py` 로 읽으면 된다.

## 여기서 돌리기

```bash
cd ~/projects/ocrserver/scripts/subfigure
codex login status            # "Logged in using ChatGPT" 여야 함

python3 astra_cli_bbox.py \
  --image  figure.png \
  --caption caption.json \
  --output out/bbox.json      # 기존 파일·out/bbox.json.run/ 이 있으면 거부 (새 경로 사용)
```

- `caption.json` 형식은 `docs/subfigure/astra_cli_bbox.md` §캡션 JSON. 캡션이 없으면
  빈 `.txt` 도 받지만, 이 도구의 전제는 **캡션이 이미 항목별로 나뉘어 있다**는 것이다
  (모델은 캡션을 만들지 않고 보이는 라벨과 캡션 인덱스만 맞춘다).
- 결과 `panels[].bbox` 는 원본 이미지 픽셀 `[left, top, right, bottom]`,
  우/하 exclusive (Pillow `crop` 그대로).
- 실행 로그(`prompt.txt`, `response.json`, `events.jsonl`, `run.json`)는
  `<output>.run/` 에 남는다. 실패해도 `.run` 은 보존되고 `.json` 만 안 생긴다.
- 1장에 90~130초(2026-09-09 실측). 구독 한도가 걸리므로 자동 재시도는 없다.
- 테스트: `python3 -m unittest test_astra_cli_bbox test_astra_panels`

## ocrserver 와의 관계

ocrserver 는 PDF → 페이지 단위 OCR(마크다운/JSON) 까지만 한다. 도판의 위치(page·bbox)
와 캡션 분할은 클라이언트(PaperMeister, fsis2026) 몫이고, 이 도구는 그 다음 단계 —
잘라낸 도판 이미지를 다시 패널로 나누는 것 — 다. 서비스에 편입돼 있지 않으며
현재는 수동 CLI 도구로만 둔다.
