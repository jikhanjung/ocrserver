# 055 — panels 섀도: Astra 호출마다 figsplit ONNX 를 같이 돌려 저장 (2026-10-26)

## 배경

figsplit 세션이 기준 모델 `B-f2l-1024L-v2` 를 fp16 ONNX 로 넘겨줬다. 경로는 `/mnt/disk1/figsplit-data/export/figsplit-onnx-fp16-B-f2l-1024L-v2/` 이고, figsplit 원본의 하드링크라 지우면 안 된다.

- figsplit 이 보고한 수치: v2 검증 whole_fig80 90.8 / 라벨까지 86.1(Astra 답 기준).
- 속도: RTX 8000 에서 도판당 약 0.5 s.
- 제약: 내부용이고, 가중치는 공개하지 않는다.

사용자: "도판 분할 요청이 들어오면 Astra 를 호출하잖아. 그때 이 모델도 같이 호출해봐. 그리고 입력, 출력 값을 모두 저장해줘. 나중에 비교하게."

## 만든 것

- `scripts/panels_shadow.py`(0.1.0): 워커와 wrapper 는 고치지 않았다. 워커가 남기는 panels 작업 폴더만 읽는다.
  - 30 s 마다 `figure_ws/*/panels/<item>/run/a<N>/run.json` 을 찾는다. run.json 이 있으면 codex 시도가 끝났다는 뜻이다.
  - 같은 `figure.png` 로 ONNX 를 두 번 돌린다. 한 번은 힌트 없이, 한 번은 캡션 entries 의 라벨을 힌트로 준다(라벨이 있을 때만).
  - 결과는 `/mnt/disk1/ocrserver/panels_shadow/<YYYYMMDD>/<item_id>/a<N>/` 에 쓴다. 날짜는 run.json 의 mtime(UTC) 기준이다.
    - `figure.png`: 사본. 작업 폴더는 30일 뒤 지워지므로 복사해 둔다.
    - `input.json`: prompt.txt 에서 뽑은 INPUT JSON(요청·캡션·entries·이미지 크기).
    - `astra_response.json`, `astra_run.json`: Astra 의 답과 실행 기록. 실패한 시도에는 답이 없다.
    - `onnx.json`: `no_hint` 와 `hint` 각각의 panels(도판 permille), 디코딩 원문, 토큰 수, 걸린 시간. 이 파일을 마지막에 원자적으로 쓰므로, 이 파일이 있으면 처리가 끝난 것이다.
  - 추론은 figsplit 의 `florence_onnx.Predictor` 를 그대로 쓴다. figsplit venv(onnxruntime-gpu 1.31, transformers)에서 돈다.
  - GPU 1(LLM 옆)에서 돌리고, `gpu_mem_limit` 은 12 GB 다. 실제로는 약 3.3 GB 를 쓴다.
  - 기본값은 시작 이후 시도만 처리하는 것이다. `--since all` 이면 작업 폴더에 남은 과거 시도(약 3천 건)를 채운다.
  - 오류가 나면 `onnx.json` 에 `{"error": …}` 를 쓰고 다음으로 넘어간다.
- `scripts/systemd/ocrserver-panels-shadow.service`: systemd --user 유닛이다(sudo 불필요).
  - 사용자가 설치하고 켰다(10-26 13:29 UTC). 자동 모드가 상주 등록을 막아서 사용자가 직접 했다.
  - linger 가 꺼져 있다. 그래서 재부팅하면 로그인하기 전까지 돌지 않는다.

## 확인

- 10-23 의 마지막 panels 시도 9건으로 `--once` 를 돌렸다. 시험 출력은 지웠다.
  - 9/9 처리, 도판당 0.29–0.44 s. 첫 장은 1.26 s 였다(워밍업).
  - 예(c97df4b7 a1): Astra a `[7,10,482,1000]` b `[525,8,1000,1000]`, ONNX a `[5,8.1,485,998.5]` b `[523,8.1,1000,998.5]`.
- 서비스 기동: 9 s 만에 모델을 올렸다. GPU 1 에서 3.26 GB 를 쓴다(LLM 30.2 GB 와 함께, 합계 약 33.5 / 48 GB).
- 10-23 07:03 이후 도판 요청이 없어서, 실제 요청으로 쌓인 결과는 아직 없다.

## 남은 일

- 쌓이면 Astra 와 비교한다: 패널 수, IoU80, 라벨, 힌트 효과.
- 워커에 정식으로 넣으려면 torch·transformers 없이 numpy + onnxruntime 만으로 다시 짜야 한다(figsplit 메시지). 넣을지는 사용자가 정한다.

## 백필 결과 (10-26 14:07 UTC, 사용자 요청 `--since all`)

작업 폴더에 남은 panels 시도 2,982건(도판 2,977장)을 모두 돌렸다. ONNX 오류는 0건이고, Astra 답이 없는 실패 시도가 5건이다.
힌트 없이 돌린 시간은 도판당 중앙값 0.34 s, p95 0.55 s 다.
로그는 `/mnt/disk1/ocrserver/panels_shadow_backfill.log` 에 있다.

figsplit 은 이 서버의 Astra 답으로 학습했다. 그래서 figsplit 의 논문 단위 분할(`data/splits/papers.json`)로 나눠서 본다.
figsplit `metrics.score_figure` 를 쓰고, 기준은 Astra 답이다. 같은 항목에 시도가 여럿이면 마지막 시도를 쓴다.

| 분할 (힌트 없음) | 도판 | 패널 수 일치 | IoU≥0.8 | 라벨 일치 | 도판 전체(≥0.8) | 도판 전체 + 라벨 |
|---|---:|---:|---:|---:|---:|---:|
| train(학습에 씀) | 2,647 | 99.5% | 99.2% | 99.2% | 99.4% | 98.1% |
| validation | 145 | 95.2% | 96.2% | 97.4% | 91.7% | 84.8% |
| test | 116 | 94.0% | 93.3% | 97.2% | 88.8% | 87.1% |
| unseen(데이터셋 이후 논문, 18편) | 69 | 85.5% | 76.5% | 95.2% | 84.1% | 82.6% |

- 공정하게 비교할 수 있는 것은 test + unseen 185장이고, 도판 전체 + 라벨 일치는 85.4% 다. figsplit 보고(v2 시험 82.4, 검증 86.1)와 비슷하다.
- unseen 은 69장뿐이라 ±5점 안팎의 잡음이 있다.
- 라벨 힌트(캡션 entries 의 라벨)의 효과는 0–1.7점으로 거의 없다.
- 입력 형식: 학습은 216 DPI 흑백 JPEG q90 이고 섀도는 워커의 컬러 PNG 다. test + unseen 을 흑백 JPEG 로 바꿔 다시 돌리면 85.4 → 86.5% 다. 두 장 차이라 잡음으로 보고, 컬러 PNG 를 그대로 쓴다.

unseen 에서 틀린 12장의 유형:
1. **과분할(가장 많음)**: 라벨 붙은 패널 안의 사진 격자나 작은 그래프를 따로 패널로 쪼갠다. 예: Astra 4 → ONNX 26, 6 → 19, 2 → 11, 3 → 8. 캡션 라벨이 2–4개라는 힌트를 줘도 그대로다.
2. **미분할**: Astra 는 라벨 없는 세로 칸 10개로 나눴는데, ONNX 는 도판 전체를 1개로 봤다.
3. **그림 속 글자를 라벨로 읽음**: 데이터셋 이름을 잘못 읽었다(HPA → HPPA, iWildCam → Widaway). 패널 b 를 "Step 1/2" 로 나누기도 했다.
4. 라벨 없는 축 패널 하나를 빠뜨리는 식의 작은 차이.

결론: 학습 밖 도판에서 Astra 와 같은 답을 낸 비율은 약 85% 다. 어긋나는 쪽은 주로 "어디까지를 한 패널로 보나"다. 이것은 Astra 와의 일치이지 정답률이 아니다. 실제 성능은 앞으로 섀도가 쌓는 새 요청으로 다시 잰다.
