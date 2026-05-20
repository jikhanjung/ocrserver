# 20260520_014 — 이미지 버전 핀(0.1.0 시작) + wrapper 네임스페이스 정리

## 결정 사항

지금까지 compose 가 우리 이미지 둘을 다 `:latest` 로 잡고 있어서 어떤
빌드본이 떠있는지 추적 불가. 0.1.0 부터 시맨틱 태그를 붙이기 시작.

| 이미지 | 변경 전 | 변경 후 |
|---|---|---|
| wrapper | `ocrserver-wrapper:latest` | `honestjung/ocrwrapper:0.1.0` |
| chandra | `honestjung/ocrserver:latest` | `honestjung/ocrserver:0.1.0` |
| vllm | `vllm/vllm-openai:latest` | (유지, 우리 거 아님) |
| nginx | `nginx:alpine` | (유지) |

wrapper 네임스페이스도 `ocrserver-wrapper` → `honestjung/ocrwrapper` 로 변경.
chandra (`honestjung/ocrserver`) 와 같은 prefix 라 일관성 ↑, 나중에 Docker Hub
에 push 할 때도 그대로 사용 가능.

## compose 정책

`/srv/ocrserver/docker-compose.yml` 은 `image:` 만 갖고 `build:` 는 없음
(dev tree 에서 빌드 → tag → 배포본은 prebuilt 참조). 따라서 새 버전 릴리스
절차는:

```bash
# 1. dev tree 에서 빌드 + 두 태그 동시 부여
docker build \
    -t honestjung/ocrwrapper:0.2.0 \
    -t honestjung/ocrwrapper:latest \
    /home/jikhanjung/projects/ocrserver/wrapper

# 2. 배포본 compose 의 image: 태그를 0.2.0 으로 변경, commit

# 3. 재기동 (태그가 바뀌었으므로 --force-recreate 불필요)
cd /srv/ocrserver && docker compose up -d wrapper
```

`:latest` 도 같이 태그하는 이유는 `docker-compose.local.yml` 의 `build:` 가
이미지명 매핑 없이 돌 때 fallback 으로 잡히게 하기 위함.

## 안 한 것

- Docker Hub push: `honestjung/ocrserver:0.1.0` / `honestjung/ocrwrapper:0.1.0`
  둘 다 로컬에만 있음. 다른 호스트에서 `docker pull` 할 일이 생기면 그때 push.
- 기존 `ocrserver-wrapper:{latest,0.1.0}` 로컬 태그는 안전을 위해 안 지움
  (`docker rmi ocrserver-wrapper:latest ocrserver-wrapper:0.1.0` 로 정리 가능,
  동일 digest 라 다른 태그가 살아있으면 layer 안 지워짐).
- 운영 중 wrapper 컨테이너는 아직 `ocrserver-wrapper:latest` 이름 참조 — 동일
  digest 라 동작에는 차이 없음. 다음 `docker compose up -d wrapper` 에서
  새 태그로 자동 전환.

## 참고

이 변경은 [[20260520_013_host_metrics]] 작업 직후, devlog 013 에서 `docker
compose build wrapper` 절차를 새 빌드 명령으로 갱신하면서 같이 정리.
