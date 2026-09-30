# link 구간 형식 A/B 실험

도판 link 답의 출력 토큰을 줄이는 실험(`docs/FIGURE_LINK_STALLS.md` §6). 모델이 캡션을 **조각으로 한 번만** 쓰고
(`x` / `h1` / `h2` / `e <labels>` / `t`), 스크립트가 원래 link 스키마(`caption` + `entries`)로 복원한다.
운영 코드가 아니다 — 프롬프트·스키마는 PaperMeister 가 요청에 실어 보내므로, 채택한다면 클라이언트 쪽 변경이다.

| 파일 | 하는 일 |
|---|---|
| `seg_format_v1.md` · `_v2.md` · `_v3.md` | 원래 link 지시문 끝에 덧붙이는 형식 설명 (버전별) |
| `build.py` | 원래 실행(`…/items/<id>/run/aN`)의 `prompt.txt`·`schema.json` → 구간 형식 프롬프트·스키마 |
| `run_ab.py` | 운영 워커의 `run_codex` 로 실행 (같은 codex 명령·idle 감시·상한). 결과가 있는 항목은 건너뜀 |
| `expand.py` | v2·v3 답 → 원래 스키마. `expand_v1.py` 는 v1 (`p=` 필드) 용 |
| `compare.py` | 운영 답(A)과 비교: 시간·출력 토큰·최종 JSON 토큰·스키마·항목·라벨·표본번호·설명/캡션 유사도 |
| `tok.py` | o200k 토크나이저를 로컬 파일에서 읽음 (아래) |
| `items_20260929.json` | 시험 대상 10건 (출력 1.5k–27k 토큰, 모두 다른 논문) |

## 실행

```
# 대상 작업 폴더는 figure_ws 7일 TTL 로 지워진다 — 오래된 목록이면 새로 골라야 한다
cd scripts/experiments/link_segments
AB_VERSION=v3 AB_PARALLEL=3 ~/venv/ocrserver/bin/python3 run_ab.py      # 결과: ~/.cache/ocrserver-ab/v3/<item>/
AB_VERSION=v3 <tiktoken 이 있는 python> compare.py
```

- `run_ab.py` 는 운영 워커와 **같은 codex 계정 한도**를 쓴다. 3건 병렬이면 워커 2개와 합쳐 동시 세션 5개.
- A/B 쪽 codex 를 수동으로 죽여야 할 때 **명령줄로 고르지 말 것** — 운영 워커의 codex 와 명령줄이 같다(2026-09-30 에 운영 세션을
  잘못 끊은 적 있음). `run_ab.py` 가 시작할 때 찍는 pid 의 자식만, 또는 작업 디렉터리(`/proc/<pid>/cwd`)로 가린다.
- `compare.py` 는 `tiktoken` 이 필요하고, `figures_worker` 의 `schema_errors` 를 쓰려고 `~/venv/ocrserver` 의 site-packages(fitz)를 덧붙인다.
  tiktoken 의 인코딩 다운로드는 이 호스트에서 실패하므로(KOPRI TLS 가로채기 + Python 3.14 엄격 인증서 검사) 한 번 받아 둔다:

```
curl -s -o ~/.cache/ocrserver-ab/o200k_base.tiktoken \
    https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken
```
