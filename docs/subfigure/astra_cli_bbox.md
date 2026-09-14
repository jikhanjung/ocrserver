# Figure + caption → bbox (Astra / Codex CLI)

`scripts/astra_cli_bbox.py`는 도판 이미지와 캡션을 받아 `gpt-6-astra`의 bbox를 원본 픽셀 좌표로 저장한다. Codex CLI의 ChatGPT 로그인 경로를 사용한다. API 키는 필요하지 않으며 하위 실행의 OPENAI_API_KEY/CODEX_API_KEY는 제거한다. 구독 한도와 계정의 모델 접근권한은 적용된다.

## 선행 조건 — 캡션 매칭·분할이 **먼저** 끝나 있어야 한다

이 스크립트는 **이미지 + 이미 쪼개진 캡션**을 받는다. 캡션을 만들거나 어느 도판의 것인지
찾아주지 않는다. 그 일은 앞 단계에서 끝나 있어야 한다.

```
[추출]  논문 원문 → ReferenceFigure.caption + subfigures(항목별로 쪼갠 캡션)
   ↓
[도판 정규화]  sync_reference_figures — 도판 행·bbox 확정
   ↓
[플레이트 설명 연결]  sync_plate_captions / sync_plate_links
   설명이 다른 쪽에 인쇄된 도판(플레이트)은 여기서 subfigures 가 채워진다
   ↓
[패널 분할]  astra_subfigures.py → astra_cli_bbox.extract()  ← **이 문서**
   ↓
[반영]  sync_reference_subfigures — ReferenceSubfigure 행 + 패널 설명 복사
```

- **분할 대상 조건은 `subfigures` 항목이 2개 이상**이다(`figure_panels.candidate_figures`).
  캡션이 한 항목이면 자를 근거가 없어 후보가 되지 않는다.
- **패널 설명은 분할 시점이 아니라 반영 시점에 `subfigures` 에서 복사된다.** 그래서
  설명 연결이 분할보다 **먼저** 돌아야 한다. 일일 체인 순서가
  `도판 → 플레이트 설명 → 논문 단위 연결 → 패널` 인 이유다(`docs/reference_pipeline.md` §11).
  순서가 뒤집히면 패널은 잘려 있는데 설명이 비거나 옛 캡션이 붙는다.
- 캡션이 나중에 바뀌면 **소스 키가 바뀌어 옛 결과가 자동으로 무효**가 된다(§소스 키).
  설명을 고친 뒤에는 `sync_panels(force=True)` 로 패널 설명도 다시 맞춘다 — 안 하면
  소패널 목록과 패널이 어긋난다.

⚠️ **캡션 자리에 캡션이 아닌 것이 들어 있을 수 있다.** 이미지 쪽에 캡션이 없는 플레이트는
추출 단계가 **그림을 보고 지어낸 묘사**를 `subfigures` 에 넣어 뒀다(P38). 그 상태로 분할하면
패널마다 지어낸 설명이 붙는다. 출처가 기록된 것(`caption_sync`)만 믿을 수 있다.

⚠️ **도판 행의 좌표가 그림이 아닐 수 있다.** 본문 텍스트 블록이 도판으로 잡힌 행에 플레이트
캡션이 잘못 연결되면, 이 스크립트는 글자만 있는 영역을 받아 **패널 0 개**로 답한다.
그건 분할 실패가 아니라 앞 단계의 좌표·연결 문제다(devlog 261, 운영 14 건).

## 실행

```bash
source ~/venv/fsis2026/bin/activate
codex login

python scripts/astra_cli_bbox.py \
  --image figure.png \
  --caption caption.txt \
  --output result/bbox.json
```

`codex`가 PATH에 없다면 `--codex /usr/local/bin/codex`를 지정한다. Python 환경에는 Pillow와 requests가 필요하다(공통 프롬프트/validator를 `astra_panels.py`에서 재사용). 현재 프로젝트 가상환경에 설치되어 있다. Codex CLI에는 `exec --image`, `--output-schema`, `--ignore-user-config`, `--ephemeral` 옵션이 필요하다.

- `--image`: PNG/JPEG/WebP 원본 도판. PDF 페이지가 아닌 도판 이미지.
- `--caption`: UTF-8 텍스트 또는 `.json` 파일. 캡션이 없으면 빈 텍스트 파일 사용 가능.
- `--subcaptions`: 선택 사항. 별도 JSON 배열의 라벨별 캡션. 지정 시 caption JSON의 목록 대신 사용.
- `--output`: 새 JSON 파일 경로. 기존 결과 또는 같은 이름의 `.run` 디렉터리가 있으면 재호출 없이 거부한다.
- `--effort`: 기본 `high`; low/medium/high/xhigh/max.
- `--timeout`: CLI 실행 제한 초, 기본 600. 로그인 확인은 별도 최대 30초. 타임아웃에는 호출 프로세스 그룹을 종료하고 오류를 기록한다.

현재 서버에서 재현 가능한 입력 예시:

```bash
python scripts/astra_cli_bbox.py \
  --image .cache/figex/pilot-whole-papers/images/ref_2275_fig_15470.png \
  --caption .cache/codex/script-smoke/caption.json \
  --output .cache/codex/my-bbox.json
```

## 캡션 JSON

```json
{
  "caption": "Figure 3. Fossil specimens ...",
  "subfigures": [
    {"label": "1", "description": "Cranidium, dorsal view ..."},
    {"label": "2", "description": "Pygidium, dorsal view ..."}
  ]
}
```

`original_caption` / `existing_subfigures` 키도 허용한다. 별도 `--subcaptions` 파일은 위 `subfigures` 배열 자체다. 라벨은 숫자도 `"1"`처럼 문자열로 입력한다. 설명은 `description`, `text`, `caption` 형태를 받을 수 있다. 캡션 목록은 참고 정보이며 보이는 패널 수와 무조건 일치시키지 않는다.

## 출력

```json
{
  "schema_version": 1,
  "model": "gpt-6-astra",
  "method": "codex_cli",
  "image_width": 2000,
  "image_height": 1500,
  "bbox_coordinate_system": "original_image_xyxy_pixels",
  "panels": [
    {
      "label": "1",
      "bbox": [10, 19, 700, 800],
      "bbox_normalized_1000": {"x0": 5, "y0": 13, "x1": 350, "y1": 533},
      "caption_indices": [0],
      "confidence": "high"
    }
  ]
}
```

위는 필드 형태를 보여주는 예시다. 실제 결과에는 elapsed_seconds, 원본 경로/해시, 캡션 목록, is_compound, figure_kind, notes, CLI usage도 저장된다. `bbox`는 `[left, top, right, bottom]`, 오른쪽/아래쪽은 Pillow crop의 exclusive 경계다. 정규화 좌표의 좌상단을 floor, 우하단을 ceil하여 픽셀로 변환한다. `caption_indices`는 입력 subfigure 배열의 0부터 시작하는 인덱스이며, 무라벨 패널의 label은 빈 문자열일 수 있다. confidence는 모델 자체 판단이며 자동 검수 통과 확률이 아니다.

결과를 사용해 crop하려면:

```python
import json
from PIL import Image

result = json.load(open("result/bbox.json", encoding="utf-8"))
with Image.open(result["image"]) as image:
    for i, panel in enumerate(result["panels"], 1):
        image.crop(panel["bbox"]).save(f"panel_{i:03d}.png")
```

`bbox.json.run/`에는 prompt.txt, schema.json, response.json, events.jsonl, stderr.log, run.json을 남긴다. 성공한 응답만 bbox.json으로 게시한다. 실패한 실행의 `.run` 폴더는 보존하며 재시도는 새 output 경로를 사용한다. 자동 재시도로 구독 사용량을 추가 소비하지 않는다.

## 분할 정책

대상 표본과 인쇄 라벨 보존을 우선한다. 배치 때문에 사각형에 이웃 표본 일부가 들어가는 것은 허용하며, 이웃을 제거하려고 대상 표본을 자르지 않도록 명시했다. 공유 스케일바는 모든 패널에 억지로 포함하지 않고 notes에 기록한다. 원본에서 이미 잘린 내용은 복원하지 않는다. 이는 이전 비교 프롬프트에 보존 우선 지침을 추가한 버전이다.

검증: CLI/로그인 mock 기반 좌표 변환, 잘못된 bbox 거부, 덮어쓰기 방지, 로그인 제한, timeout 오류 기록, 캡션 형식 테스트를 포함한 관련 테스트 26개 통과. 실제 호출 결과는 `.cache/codex/script-smoke/`에 보존한다.

실제 실행 검증 완료: 15470 도판에서 라벨 1–8, 8개 유효 픽셀 bbox, CLI 93.63초.
