# 052 — llm 이미지 honestjung/vllm-openai:0.20.2-p1 (2026-10-23)

## 배경

llm 서비스는 `vllm/vllm-openai:latest`(2026-05-08 빌드, vLLM 0.20.2)를 그대로 썼다.
emSign CA 키 유출(GHSA-6q48-6c7w-9gw2, devlog 049) 점검에서 다음이 나왔다:
- certifi 2026.04.22, pip 25.0.1(내장 certifi 2024.08.30);
- OS 저장소에 emSign ECC Root CA - G3 가 있었다.

새 `vllm/vllm-openai` 태그를 받으면 vLLM 자체가 바뀐다(이미지 ~30 GB). Qwen3-32B-AWQ 설정(`--enforce-eager`, GPU 1 예산제 0.60 등)을 다시 검증해야 하는데,
GPU 가 둘 다 figsplit 학습 중이라 지금은 할 수 없다. 그래서 chandra 0.1.2(devlog 051)처럼 **vLLM 은 그대로 두고 보안 갱신만 얹는 얇은 레이어**로 했다.

## 내용

`Dockerfile.vllm-patch` → `honestjung/vllm-openai:0.20.2-p1`
- 베이스를 `vllm/vllm-openai@sha256:70a098d9…` 로 고정했다. compose 가 쓰던 `:latest` 의 그 이미지다. `:latest` 는 움직이므로 digest 로 고정한다.
- ca-certificates 를 20260816ubuntu0.22.04.1 로 올렸다(G3 없음). G3 가 남으면 빌드가 실패한다.
- pip 26.2.2, certifi 2026.10.19.
- 빌드 마지막 RUN 이 다음을 확인한다:
  - certifi 와 pip 내장 certifi 가 2026.10.19 이상이다.
  - certifi 번들에 G3 가 없다.
  - `importlib.metadata` 로 본 vllm 버전이 0.20.2 다. vllm 은 import 하지 않는다(torch/CUDA 안 건드림).
- ENTRYPOINT `["vllm","serve"]`, WORKDIR 은 그대로다. 31.8 GB 이지만 새 레이어는 작다.

## 배포

- Docker Hub 에 **새 공개 저장소** `honestjung/vllm-openai` 를 만들었다(사용자 결정). digest `2b525d22…`, `imagetools inspect` 로 원격을 확인했다.
- `docker-compose.yml`·`docker-compose.local.yml` 의 llm 이미지를 바꾸고 `/srv/ocrserver` 에 복사했다. llm 은 정지 중이라 아무것도 재시작하지 않았다.
- CLAUDE.md 의 구성 표를 고쳤다.

## 남은 것

- **vLLM 기동은 아직 확인하지 못했다.** LLM 모드 복구 때(`mode-llm.sh`) Qwen3-32B-AWQ 가 뜨고 `/llm/v1/models`·짧은 생성이 되는지 본다.
  문제가 생기면 compose 를 `vllm/vllm-openai:latest`(로컬 이미지 그대로)로 되돌린다.
- vLLM 자체를 올리는 것은 별건이다. 새 태그를 받아 GPU 가 빌 때 검증한다. 그때 이 패치 파일은 새 베이스로 다시 만들거나 버린다.
