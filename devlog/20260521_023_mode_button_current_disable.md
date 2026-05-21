# 20260521_023 — 현재 모드 버튼 비활성화 + 라벨/`.env` 보존 수정 (wrapper 0.1.8)

## 변경 사항

직전 020/022 작업 후속 다듬기. 세 개의 작은 픽스를 한 묶음으로:

### 1) 현재 모드 버튼 누를 수 없게

`/status` 의 모드 전환 버튼이 항상 두 개 다 enabled 였음. 현재 모드와 같은
버튼을 눌러도 mode-*.sh 가 그대로 실행되면서 wrapper 가 무의미하게 recreate
됨. 사용자 요청: 현재 모드 버튼 자체를 클릭 불가 + 시각적으로 현재 모드 표시.

구현: `currentScriptTarget(mode)` 가 현재 mode probe 값을 `'ocr'` / `'llm'` /
`null` 로 매핑 (mode 에 `'llm'` 포함되면 `'llm'`, 아니면 `'ocr'`). 그 결과에
해당하는 버튼은 `disabled` + Bootstrap `btn-secondary` (filled) + `active`
class. 다른 버튼은 outline 으로 enabled. `refresh()` cycle 마다 갱신.

### 2) 버튼 라벨

`→ OCR` / `→ LLM` 가 모호하다는 피드백. mode-ocr.sh / mode-llm.sh 가 실제로
하는 일을 그대로 적은 라벨:

- **OCR×2** — mode-ocr.sh, GPU 0 + GPU 1 둘 다 chandra
- **OCR+LLM** — mode-llm.sh, GPU 0 chandra + GPU 1 Qwen3-14B

### 3) `.env` 통째로 덮어쓰던 mode-*.sh 수정

```bash
# before
echo "OCR_CONCURRENCY=12" > .env
# after
touch .env
if grep -q "^OCR_CONCURRENCY=" .env; then
    sed -i "s/^OCR_CONCURRENCY=.*/OCR_CONCURRENCY=12/" .env
else
    echo "OCR_CONCURRENCY=12" >> .env
fi
```

기존 `>` 가 .env 를 통째로 덮어쓰면서 직전 022 에서 추가한 `MODE_TOKEN=...`
줄을 mode 전환마다 wipe 하던 버그. sed in-place 갱신 + 줄 없으면 append 로
다른 env vars 안전 보존. mode-llm.sh 도 동일 패턴 (값만 6).

## 배포

```bash
cp /home/jikhanjung/projects/ocrserver/docker-compose.yml /srv/ocrserver/
cp /home/jikhanjung/projects/ocrserver/mode-{ocr,llm}.sh /srv/ocrserver/
chmod +x /srv/ocrserver/mode-{ocr,llm}.sh
cd /srv/ocrserver && docker compose up -d wrapper
```

이미지: `honestjung/ocrwrapper:0.1.8` (digest `f258af407925...`) — Docker Hub
`:0.1.8` + `:latest` push + 운영 swap 완료. mode-*.sh 도 sync 완료.
