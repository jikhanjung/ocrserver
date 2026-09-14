# 도판 분할 실측 비교 및 Astra CLI bbox 실행기

2026-09-08. GPU 서버에서 FigEx 전량 추출, API/구독 CLI 비교를 완료하고 재사용 가능한 Astra CLI 스크립트를 추가했다.

## 구현

- `scripts/figex_compat_inference.py`: 공식 FigEx 소스/가중치를 보존하면서 tokenizer BOS/EOS와 cached hidden states 연결을 보정하는 실험용 실행기. 원본 실행은 빈 결과 또는 인덱스 오류였고 호환 처리 후 검출 동작 확인.
- `scripts/astra_panels.py`: Astra 기본값을 유지하며 Sol/Terra/Luna 선택 옵션과 개별 API 시간 기록 추가. 모델명이 검토 페이지에 표시되도록 수정.
- `scripts/figex_batch.py`: 검토 HTML에 FigEx 생성 캡션 및 이미지 lazy loading 추가.
- `scripts/astra_cli_bbox.py`: figure 이미지 + 텍스트/JSON 캡션 + 선택 subcaption 목록 → ChatGPT 로그인 Codex CLI Astra high → 검증된 원본 픽셀 bbox JSON. 원응답·프롬프트·시간·usage를 `.run` 디렉터리에 보존. 기존 결과 덮어쓰기, API 키 로그인 우회, 실패 응답의 성공 출력 방지. timeout/중단 시 하위 프로세스 그룹 종료.
- 사용자가 명시한 평가 원칙 반영: 표본이 서로 끼인 도판에서는 이웃 일부 포함을 허용하며 대상 표본과 라벨을 자르지 않는 것이 우선이다.

## 실험 결과

FigEx: dolfinid에서 받은 20편·207장 입력과 해시 검증 후 GPU 1에서 전체 추출. 루프 2:10:08, 205장 검출·2장 빈 결과, 1,114 PNG 전량 검증, crop 오류 0. GPU 1 반환. 물리 환경 및 호환 보정은 `docs/figex_local_setup.md` 참조.

같은 목적 표본 10장의 API 비교:

| 모델 | 패널 수 | 평균 초 | 10장 표준 요금 추정 USD |
|---|---:|---:|---:|
| Astra | 79 | 30.74 | 1.079175 |
| Sol | 79 | 24.37 | 0.430610 |
| Terra | 78 | 12.98 | 0.188791 |
| Luna | 78 | 21.83 | 0.0280483 |

Astra는 기존 1 + 신규 9, 나머지는 각각 신규 10건. 78/79 차이는 범례 전용 이미지를 0개/1개로 보존하는 정책 차이다. 숫자만으로 품질을 판단하지 않았다. Sol은 Astra에 가까웠지만 복잡한 도판에서 일부 잘림이 있었고 Terra/Luna는 경계 오차가 더 컸다. 표본 선택 편향·단회 측정·수동 좌표 초안의 한계는 보고서에 명시했다.

Claude Code: Sonnet 1장 smoke 성공 후 사용자 교정으로 Opus 5.0을 선택. 진행 중 Sonnet 배치를 중단하고 **Opus 10장 모두 새로** 실행했다. 79패널, 평균 CLI 20.99초. 27패널과 산점도에서 잘림 확인. Max 로그인 경로이며 CLI 비용 카운터를 실제 추가 청구액으로 해석하지 않았다.

Astra Codex CLI: 사용자 범위 축소에 따라 **15866 한 장만** 수행. high 설정, 27패널, 129.73초. API Astra와 유사한 보존 품질. CLI 초기화/서비스 대기 포함 시간이므로 순수 API 지연과 동일 지표는 아니다. 이후 재사용 스크립트로 15470 도판 실측: 8개 라벨·유효 bbox, 93.63초.

## 문서 및 산출물

- 사용법: `docs/astra_cli_bbox.md`
- API 비교: `docs/astra_figex_comparison_20260908.md`, `docs/figure_model_comparison_20260908.md`
- CLI 비교: `docs/claude_opus5_figure_comparison_20260908.md`, `docs/astra_cli_15866_20260908.md`
- 실험 계획 원안: `docs/fsis_figure_splitting_experiment_plan.md`; 별도 검토: `docs/fsis_figure_splitting_experiment_review_20260908.md`
- 수치 정본: `docs/experiments/`의 비교 JSON. 원응답·이미지·가중치·가상환경은 로컬 캐시/데이터 디스크에 유지.
- 웹 비교 서버: `http://172.16.112.150:8765/`, 하위 `models/`, `opus/`, `codex-astra/`. 일시적 로컬 서비스이며 애플리케이션 배포가 아니다.
- 검토 결론: 대표 표본에서 Astra/Sol 검증 및 경계 보정을 먼저 하고, 대규모 학습·융합은 처리량/보정 데이터가 충분해진 뒤 판단한다.

## 검증

`python -m unittest scripts.test_astra_cli_bbox scripts.test_astra_panels scripts.test_figex_batch` — 26개 통과.
CLI 테스트는 캡션 형식, 픽셀 좌표/메타데이터, 범위 밖 bbox 거부, 기존 결과 보존, 구독 로그인 확인, timeout 실패 기록을 검증한다. 신규 API 30건의 PNG 235개, Opus PNG 79개, Astra CLI PNG 27개도 좌표 크기 및 파일 무결성 검증. 비교 HTML/PNG HTTP 200 확인. `git diff --check` 통과.

DB 스키마·웹앱 배포 버전은 변경하지 않았다.
