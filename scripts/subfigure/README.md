# subfigure — 도판을 패널로 분할 (Astra / Codex CLI)

fsis2026 `scripts/astra_cli_bbox.py`, `scripts/astra_panels.py` 와 테스트를 그대로
가져온 것 (fsis2026 커밋 `79a9081`). 테스트의 `scripts.astra_*` import 경로만
이 디렉터리 기준으로 바꿨다.

- 사용법·문서 인덱스: **`../../docs/SUBFIGURE_SPLIT.md`**
- CLI 상세: `../../docs/subfigure/astra_cli_bbox.md`
- `reference/` 는 Django(kprdb) 에 묶인 fsis2026 운영 레인 — 여기서는 실행 불가, 참고용.

```bash
codex login status
python3 astra_cli_bbox.py --image figure.png --caption caption.json --output out/bbox.json
python3 -m unittest test_astra_cli_bbox test_astra_panels
```
