# 20260521_022 — 웹에서 모드 전환 + 재시작 페이지 (wrapper 0.1.7)

## 목표

지금까지는 `ssh ops; /srv/ocrserver/mode-{ocr,llm}.sh` 직접 실행해야 OCR↔LLM
모드 전환 가능. 상태 페이지에서 버튼으로 트리거되도록 + 전환 중 nginx 가
"서버 재시작 중" 안내 페이지로 자동 새로고침.

## 아키텍처 (docker socket 노출 X)

```
       brower                wrapper                     host
  ┌──────────────┐      ┌──────────────┐         ┌───────────────────┐
  │  /status     │      │              │         │ systemd .path     │
  │  → 전환 클릭 │ POST │ /api/mode    │ write   │ watch             │
  │              │ ───► │              │ ──────► │ data/mode_request │
  └──────────────┘      │ _mode_       │         │                   │
                        │  switching=T │         │ ↓ fires           │
                        │              │         │ .service          │
                        │ /ocr POST    │         │   = mode_switcher │
                        │  → 503       │         │     .sh           │
                        └──────────────┘         │     → mode-*.sh   │
                              ▲                  │       → wrapper   │
                              │                  │         recreate  │
                              └──── 새 wrapper ──┘                   │
                                                 └───────────────────┘
```

## wrapper (0.1.7) 변경

- env: `MODE_TOKEN` — 비어있으면 `/api/mode` 가 503 (disabled 사실상). 운영
  적용 시 `/srv/ocrserver/.env` 에 `MODE_TOKEN=<secret>` 추가 필요.
- `POST /api/mode {"mode":"ocr"|"llm"}` — 헤더 `X-Mode-Token` 검증, request
  파일 `/data/mode_request` (= 호스트의 `/srv/ocrserver/data/mode_request`)
  에 한 줄 쓰고 `_mode_switching=True` 설정.
- `GET /api/mode` — `{enabled, switching}` 상태 노출.
- `POST /ocr` — `_mode_switching` 이면 503 "mode switch in progress" 반환.
  사용자 요구: "모드 전환 요청 이후 일상 작업 요청 모두 거부".
- 빌드된 wrapper recreate 가 mode-*.sh 끝부분에 있어 새 인스턴스는 깨끗한
  상태 — `_mode_switching` 플래그도 자연 리셋.

## 호스트 측

```
scripts/mode_switcher.sh                         # 짧은 dispatcher
scripts/systemd/ocrserver-mode-switch.path       # data/mode_request 감시
scripts/systemd/ocrserver-mode-switch.service    # .path 에 의해 트리거
```

설치 (root):

```bash
sudo install -m 0755 /home/jikhanjung/projects/ocrserver/scripts/mode_switcher.sh \
    /srv/ocrserver/scripts/mode_switcher.sh
sudo install -m 0644 /home/jikhanjung/projects/ocrserver/scripts/systemd/ocrserver-mode-switch.path \
    /etc/systemd/system/
sudo install -m 0644 /home/jikhanjung/projects/ocrserver/scripts/systemd/ocrserver-mode-switch.service \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ocrserver-mode-switch.path
```

`.path` 가 `PathExists=` 로 파일 존재만 감지 → `.service` 한 번 실행 → 스크립트
가 파일 삭제 → 다음 요청 대기. 같은 모드로 재요청해도 `rm -f` 후 다시 fire.

## nginx 재시작 안내 페이지

`nginx-errors/restarting.html` (스피너 + meta refresh 3초) 를 nginx 컨테이너에
RO 마운트. 두 nginx conf (`nginx.ocr.conf` / `nginx.llm.conf`) 의 wrapper
proxy location 들에 `proxy_intercept_errors on; error_page 502 503 504 = /__restarting;`
추가.

API 라우트(`/ocr`, `/api/`) 는 의도된 503 응답(예: mode switching, validation)
이 클라이언트에 그대로 가야 하므로 `error_page 502 504` 만 (503 제외). HTML
라우트(`/`, `/status`, `/metrics`, `/static/`) 는 502 503 504 모두 → 친절한
재시작 페이지.

## docker-compose 변경

```yaml
nginx:
  volumes:
    - ./nginx-errors:/etc/nginx/errors:ro       # 추가
wrapper:
  image: honestjung/ocrwrapper:0.1.7
  environment:
    MODE_TOKEN: "${MODE_TOKEN:-}"               # 추가, .env 에서 주입
```

## /status UI

모드 chip 옆에 작은 버튼 그룹 `→ OCR` `→ LLM`. `/api/mode.enabled` 가 false
면 숨김. 클릭 → `processing N건` 안내 + 확인 다이얼로그 → 토큰 prompt
(localStorage 캐싱) → POST. 403 받으면 토큰 캐시 비움. 성공 시 alert.

## 배포 순서 (운영 호스트)

```bash
# 1. dev tree → 운영본 sync
cp /home/jikhanjung/projects/ocrserver/docker-compose.yml /srv/ocrserver/
cp /home/jikhanjung/projects/ocrserver/nginx.ocr.conf /srv/ocrserver/
cp /home/jikhanjung/projects/ocrserver/nginx.llm.conf /srv/ocrserver/
cp /srv/ocrserver/nginx.ocr.conf /srv/ocrserver/nginx.conf   # 현재 모드

# 2. nginx-errors/ 디렉터리 동기화 (새 RO 마운트 대상)
sudo mkdir -p /srv/ocrserver/nginx-errors
sudo install -m 0644 /home/jikhanjung/projects/ocrserver/nginx-errors/restarting.html \
    /srv/ocrserver/nginx-errors/restarting.html

# 3. .env 에 토큰 추가
echo "MODE_TOKEN=<secret>" | sudo tee -a /srv/ocrserver/.env

# 4. 호스트 systemd 설치 (위 "설치" 블록)

# 5. wrapper + nginx 재기동
cd /srv/ocrserver && docker compose up -d wrapper \
    && docker compose exec nginx nginx -s reload

# 6. 동작 확인
curl -s http://localhost:8080/api/mode    # {"enabled":true,"switching":false}
```

## 이미지

`honestjung/ocrwrapper:0.1.7` (digest `600a9d7213f1...`) — 빌드만, push 와
deploy 는 사용자 확인 후.
