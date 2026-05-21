# 20260521_020 — 상태 페이지에 컨테이너 이미지 태그 노출 (wrapper 0.1.6)

## 동기

운영 중인 wrapper / chandra-a / chandra-b / llm 이 각각 어떤 이미지 태그로
떠있는지 `/status` 만 보고는 알 수 없어서, 매번 `docker compose ps` 를
호스트에서 직접 쳐야 했다. 0.1.X 가 빠르게 늘어나면서 이게 더 답답해짐.

## 구현

운영본 `docker-compose.yml` 을 wrapper 컨테이너에 read-only 마운트해서
`image:` 값을 추출하는 방식. docker socket 노출 없이도 desired state 가
보임 (운영 중인 실제 digest 와 다를 수 있지만, 그건 chandra-b 처럼
운영자가 같은 digest 라 굳이 recreate 안 한 케이스 — 의도가 더 중요).

### 변경 사항

- `wrapper/Dockerfile`: pip install 에 `pyyaml` 추가
- `wrapper/main.py`:
  - `COMPOSE_PATH = os.getenv("COMPOSE_PATH", "/etc/ocrserver-compose.yml")`
  - `_read_compose_images()` — yaml.safe_load + 60초 TTL 캐시
  - `/api/services._meta.images = {wrapper: tag, chandra-a: tag, ...}`
- `docker-compose.yml` / `docker-compose.local.yml`: wrapper volumes 에
  `./docker-compose{,.local}.yml:/etc/ocrserver-compose.yml:ro` 추가
- `wrapper/status.html`: 모드 카드 하단에 컨테이너 이미지 뱃지 4개 표시
  순서 고정 `wrapper, chandra-a, chandra-b, llm`. nginx 는 거의 안 바뀌므로 제외

### 표시 예

```
[OCR 2 GPU]   가동시간 ...   동시성 ...   처리중 ...
───────────────────────────────────────────────────────
컨테이너 이미지 (compose 설정 기준)
[wrapper 0.1.6] [chandra-a 0.1.1] [chandra-b 0.1.1] [llm latest]
```

뱃지의 `title` 속성에 full image string (`honestjung/ocrwrapper:0.1.6`)
들어가서 hover 시 보임.

## 배포

이미지: `honestjung/ocrwrapper:0.1.6` (digest `308861f8931a...`).
이번 swap 은 wrapper image 만 바뀌는 게 아니라 **volume 마운트도 추가** 되니
compose 파일 sync 가 필수:

```bash
cp /home/jikhanjung/projects/ocrserver/docker-compose.yml /srv/ocrserver/
cd /srv/ocrserver && docker compose up -d wrapper
```

compose 파일을 wrapper 컨테이너가 마운트로 읽으므로, `/srv/ocrserver/` 의
파일이 sync 안 돼있으면 wrapper 가 옛 태그를 표시하거나 `_read_compose_images`
가 빈 dict 를 돌려줘서 "compose 파일을 읽지 못했습니다" 라고 뜸.
