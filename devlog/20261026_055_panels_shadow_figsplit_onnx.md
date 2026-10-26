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
