# 051 — chandra 이미지 0.1.2: 얇은 레이어로 emSign·pypdfium2 반영 (2026-10-23)

## 왜 얇은 레이어인가

`honestjung/ocrserver:0.1.1` 에는 emSign ECC Root CA - G3 가 있었다(GHSA-6q48-6c7w-9gw2, devlog 049).
OS 저장소에 G3 가 있었고, certifi 2026.04.22, pip 26.1.1(내장 2026.02.25)이었다.
`batch_ocr.py` 는 PyMuPDF 판이었다(devlog 050 에서 저장소만 pypdfium2 로 바꿨다).

루트 `Dockerfile` 로 전체를 다시 빌드하면 모델 10.6 GB 를 다시 받는다. 이 호스트에서는 그 단계가 12% 에서 멈춘다(CLAUDE.md).
그래서 0.1.1 위에 고칠 것만 얹는 `Dockerfile.patch` 를 새로 만들었다. 모델 레이어는 그대로 재사용한다.

## 내용

`docker build -f Dockerfile.patch --build-arg BASE=honestjung/ocrserver:0.1.1 -t honestjung/ocrserver:0.1.2 .`

- ca-certificates 갱신. Ubuntu 22.04 의 20260816ubuntu0.22.04.1 은 G3 를 이미 뺐다.
  `ca-certificates.conf` 의 `!` 처리는 줄이 남은 베이스를 대비한 것이고, 빌드는 G3 가 남으면 실패한다.
- `pip uninstall PyMuPDF`, 그리고 pip 26.2.2, certifi 2026.10.19, pypdfium2 5.14.0 으로 갱신했다. Pillow 는 있던 것을 쓴다.
- `COPY batch_ocr.py`(pypdfium2 판).
- 빌드 마지막 RUN 이 다음을 확인한다:
  - certifi 와 pip 내장 certifi 가 2026.10.19 이상이다.
  - certifi 번들에 G3 가 없다.
  - `batch_ocr.render_page` 가 빈 PDF 를 렌더한다.
  - torch/CUDA 는 건드리지 않는다. 이미지를 `docker run` 하지 않았다(GPU 가 figsplit 학습 중이다).
- 이미지는 0.1.1 보다 약 0.1 GB 크다. ENTRYPOINT·ENV 는 그대로다.

## 배포

- Hub push 를 했다(digest `9fbb4908…`, 새 레이어만 올라감). `imagetools inspect` 로 원격 태그를 확인했다.
- `docker-compose.yml`(chandra-a·chandra-b)과 `docker-compose.local.yml` 을 0.1.2 로 바꾸고 `/srv/ocrserver` 에 복사했다.
  chandra 는 정지 중이라 아무것도 재시작하지 않았다.

## 남은 것

- **vLLM 기동과 OCR 회귀를 아직 확인하지 못했다.** OCR 복구 때 `mode-llm.sh`(또는 `mode-ocr.sh`)로 chandra 가 0.1.2 로 뜨면
  `kruskal1964.pdf` 15/15 를 확인한다. 문제가 생기면 compose 를 0.1.1 로 되돌린다(이미지는 로컬과 Hub 에 있다).
- 이미지 안의 옛 `/usr/local/bin/uv`(vllm 베이스 것)는 런타임에 쓰지 않아 그대로 두었다.
- 다음에 외부 머신에서 루트 `Dockerfile` 로 전체를 빌드하면 이 내용이 다 들어가므로 `Dockerfile.patch` 는 필요 없어진다.
- `vllm/vllm-openai:latest`(llm 서비스)는 아직 emSign 미대응이다. OCR+LLM 복구 때 같은 방식으로 감싸거나 새 태그로 받는다.
