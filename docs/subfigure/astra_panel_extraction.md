# Astra로 figure 패널 분할 (2026-09-08 실험 기록 — **운영 경로 아님**)

> ⚠️ **이 문서는 공식 Responses API 로 부르던 실험 기록이다.** 운영 레인은 그 뒤
> **Codex CLI(ChatGPT 로그인)** 경로로 정해졌다(P37, devlog 253). 지금 도는 것은
> `scripts/astra_subfigures.py` → `scripts/astra_cli_bbox.py` 이고, 호출 방법과
> **선행 조건(캡션 매칭·분할이 먼저)** 은 `docs/astra_cli_bbox.md` 를 볼 것.
> 아래 "실제 Astra 추론은 미실행" 은 2026-09-08 시점 서술이다.


2026-09-08. 대상은 `pilot-whole-papers`: 우리 논문 20편의 figure 전량 207장,
기존 subfigure 라벨·설명 1,082개. FigEx와 같은 원본으로 비교한다.

## 현재 상태

`scripts/astra_panels.py` 구현 및 로컬 테스트 10개 통과.
**실제 Astra 추론은 미실행**: 현재 환경과 프로젝트 `.env`에 OPENAI_API_KEY가 없다.
모델 접근 권한·실제 API 호환성·응답 지연·정확도·비용은 아직 확인하지 않았다.
테스트는 합성 이미지와 모의 응답을 사용하며 모델 성능 검증이 아니다.

## 실행

기존 프로젝트 venv의 requests/Pillow/python-decouple을 사용한다. OpenAI SDK 추가 설치나
GPU는 필요 없다. `gpt-6-astra`를 공식 Responses API로 호출하며 다른 모델로 대체하지 않는다.
키는 환경변수 또는 `.env`에 설정한다. CLI 인자에 키 자체를 넣거나 채팅에 보내지 않는다.

```bash
source ~/venv/fsis2026/bin/activate

# 지도·27패널 도판·영문라벨 도판·도표 4건으로 실제 입출력 smoke
python scripts/astra_panels.py \
  --bundle .cache/figex/pilot-whole-papers \
  --output .cache/astra/pilot-whole-papers --env-file .env \
  --figure-ids 15465 15866 15727 15469 --workers 2

# smoke crop 확인 후 전체 실행. 완료된 4장은 재호출하지 않음.
python scripts/astra_panels.py \
  --bundle .cache/figex/pilot-whole-papers \
  --output .cache/astra/pilot-whole-papers --env-file .env --workers 4

# 오류 원인 해결 후 실패 건만 재시도 (완료 건 재호출 없음)
python scripts/astra_panels.py \
  --bundle .cache/figex/pilot-whole-papers \
  --output .cache/astra/pilot-whole-papers --env-file .env --retry-errors

# 저장된 응답으로 crop·HTML 재생성, API 키/호출 불필요
python scripts/astra_panels.py \
  --bundle .cache/figex/pilot-whole-papers \
  --output .cache/astra/pilot-whole-papers --review-only
```

기본 reasoning effort는 high, 이미지 detail은 high, 출력 상한은 16,000 tokens.
`--effort` 또는 입력/프롬프트가 달라지면 별도 output 디렉터리를 쓴다.
`--limit`/`--figure-ids`는 smoke용이며 HTML inventory에는 전체 207장의 처리 상태가 남는다.
인증/모델 접근 오류(401/403/404)는 이후 대기 작업의 호출을 중단한다.
429/서버 오류만 제한적으로 재시도하고, timeout은 자동 재호출하지 않는다.

## 입출력 원칙

- 전체 figure와 원문 캡션, 기존 subfigure별 라벨·설명을 한 요청에 전달.
- 모델은 figure 내부 0~1000 xyxy 좌표, 보이는 라벨, 기존 캡션 index만 반환.
  기존 과학적 설명을 새로 작성하지 않는다. 기존 설명이 틀릴 수 있으므로 패널 개수·연결을 강제하지 않는다.
- 숫자·혼합 라벨 및 26개 초과 도판 지원. 단일 figure는 전체 영역 1개로 유지하도록 지시.
- 화석 해부학적 가장자리 보존, 패널별 라벨·스케일바 포함을 우선하고 이웃 표본 혼입은 피하도록 지시.
- crop은 항상 원본 PNG에서 수행한다. 모델이 그림을 재생성하지 않는다.
- 미연결 캡션, 개수 불일치, 중복 라벨, 모델이 불확실하다고 답한 패널은 경고로 남긴다.
  모델 confidence는 교정된 통계적 확률이 아니다.
- JSON schema 적합성과 좌표 범위, 캡션 index 유효성 검사. 거부·불완전 응답을 성공으로 처리하지 않는다.
- 한 번의 모델 추론 + 코드 검증 단계이다. crop을 모델에 다시 보내는 시각적 재검수는 아직 구현하지 않았다.

결과 디렉터리:

```text
run.json               # 모델/effort/프롬프트/입력 manifest 해시
raw/<stem>.json        # 원본 API 응답, 사용량 (키·요청 이미지 제외)
predictions/<stem>.json # 완료/오류, 좌표와 캡션 index, 시간, response ID
crops/<stem>/          # 개별 panel PNG와 박스 overview
panels.json            # 전체 처리현황, 캡션 원문 연결, token 사용량
index.html             # 논문별 figure·crop·캡션·경고 검토
```

원본 DB, FigEx 입력 묶음, 기존 캡션은 변경하지 않는다. 출력 상태가 completed여도
사람의 검수를 통과했다는 뜻은 아니다. API 응답 raw/prediction을 그림별 저장하므로 중단 후 재개 가능.

공식 API 근거:
[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra),
[이미지 입력](https://developers.openai.com/api/docs/guides/images-vision),
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
