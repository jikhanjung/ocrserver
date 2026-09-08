# devlog 043 — 정지된 chandra-b의 IP를 wrapper가 물려받아 nginx가 OCR 페이지를 wrapper에게 되돌려준 사고 (→ upstream `resolve`)

날짜: 2026-09-08
태그: 장애 1건 (05:40~07:53 UTC, 페이지 3,765건 404 / job 13건 done_with_errors), nginx 설정 변경
선행: [devlog 042](20260828_042_fair_share_scheduler_per_client.md)

## 요약

GPU 1을 다른 작업에 쓰려고 `chandra-b`만 정지한 뒤 wrapper를 재생성했더니,
새 wrapper가 chandra-b가 놓고 간 IP(`172.18.0.3`)를 받았다. nginx의 정적
`upstream chandra { server chandra-a; server chandra-b; }` 는 기동 시 한 번
해석한 IP를 계속 들고 있으므로, **"chandra-b"라고 믿는 주소로 보낸 OCR
요청이 wrapper에게 갔다**. wrapper에는 `/v1/chat/completions` 경로가 없어
404를 돌려주고, nginx는 404를 정상 응답으로 취급해 재시도하지 않는다.
`least_conn`은 0.4초 만에 끝나는(=늘 한가한) 쪽을 선호해서 페이지의 **약
97%** 가 그리로 몰렸다. 결과: 229쪽 PDF에 성공 3쪽, 나머지 226쪽 실패 식의
`done_with_errors` 가 2시간 동안 13건.

고침: `upstream chandra` 를 `zone` + `server ... resolve` 로 바꿔 이름을
런타임에 재해석하게 했다 (open-source nginx 1.27.3+; 현재 `nginx:alpine` =
1.29.8). 사라진 이름은 풀에서 빠지고, 돌아오면 다시 붙고, 기동 시 해석이
안 돼도 nginx가 뜬다.

## 1. 타임라인 (UTC)

| 시각 | 사건 |
|---|---|
| 03:51 | 호스트 정상 재부팅 (`systemd: Shutting down`, MCE 아님). 03:52 전 컨테이너 기동, chandra-b 포함. nginx가 `chandra-b` → `172.18.0.3` 으로 고정 해석 |
| 03:54 | chandra cold start 중 페이지 12건 `HTTP 502` (Krebs and Davies, 466쪽 중 12쪽). 이번 건과 무관, 예상 범위 |
| 05:38 | chandra-b 정지 (exit 0, 의도적). GPU 1은 별도 python 프로세스(28.8GB, 99%)가 사용. `.env` `OCR_CONCURRENCY=6` |
| 05:40 | wrapper force-recreate → **IP `172.18.0.3` 배정** (chandra-b가 반납한 주소) |
| 05:40:55 | 첫 404. 이후 페이지 404가 초당 수 건 |
| 05:43 | `nginx.ocr.conf` → `nginx.conf` 복사 흔적. 2서버 conf 는 정지된 chandra-b 이름 해석 실패로 reload 불가 → 옛 설정 유지. 게다가 이 복사가 새 inode 를 만들어 컨테이너는 08-28자 파일에 고정됨 |
| 07:48 | 조사 시작. 누적 404 페이지 3,765, 대기열 592 |
| 07:52 | `nginx -s reload` 시도 → `[emerg] host not found in upstream "chandra-b:8000"` (컨테이너가 옛 inode 의 2서버 conf 를 읽음) |
| 07:53 | chandra-a 만 있는 conf 로 `up -d --no-deps --force-recreate nginx`. 이후 404 0건 |
| 09:40 | `resolve` 기반 conf 배포 + reload. chandra-b 부재 상태에서 정상 기동·서빙 확인 |

## 2. 증거

wrapper 로그 — nginx(172.18.0.6)가 wrapper 에게 OCR 요청을 보내고 있다:

```
INFO:     172.18.0.6:54302 - "POST /v1/chat/completions HTTP/1.1" 404 Not Found
```

pages 테이블, job `89df8071` (McCall 2006, 229쪽):

```
('failed', 226, avg 410 ms)   ← 404, 즉시
('ok',       3, avg 34536 ms) ← chandra-a 실제 OCR
```

`/api/services` 는 chandra-b `down`, `alive 1/2`, mode `1ocr` 로 **정확히**
표시하고 있었다. wrapper 의 백엔드 상태와 nginx 의 라우팅 표는 서로 독립
— wrapper 는 항상 `http://nginx/v1/chat/completions` 한 곳으로만 보낸다.

## 3. 왜 이렇게까지 나빴나

1. **404 는 재시도 대상이 아니다.** `proxy_next_upstream` 기본값은
   `error timeout`. 연결 거부였다면 nginx 가 chandra-a 로 넘겼을 것.
   살아 있는 엉뚱한 HTTP 서버가 그 IP 에 앉은 것이 핵심.
2. **least_conn 이 실패를 편애한다.** 연결 수가 적은 백엔드를 고르는데,
   0.4초 만에 끝나는 쪽은 늘 0 에 가깝다. 정상 백엔드가 34초 걸리는
   동안 실패 쪽이 80건을 처리한다.
3. **reload 로 못 고친다.** Docker DNS 는 정지된 컨테이너 이름을 지우므로
   2서버 conf 는 `nginx -t` 부터 실패한다. 전체 재시작도 chandra-b 없이
   하면 nginx 가 크래시 루프에 빠져 8080 전체가 죽는다.
4. **단일 파일 bind mount inode 고정** (기존 메모리
   `project_docker_bind_mount_inode`) 이 겹쳐 07:52 의 reload 시도가
   새 conf 를 읽지도 못했다. `cp` 는 in-place 라 괜찮지만 에디터 저장이나
   `mv` 는 새 inode 를 만든다.

## 4. 고침 — `server ... resolve`

```nginx
upstream chandra {
    zone chandra 64k;          # resolve 는 공유 메모리 zone 필수
    least_conn;
    server chandra-a:8000 resolve;
    server chandra-b:8000 resolve;
    keepalive 16;
}
resolver 127.0.0.11 valid=10s ipv6=off;   # 이미 있었음 (042)
```

`nginx.ocr.conf`, `nginx.llm.conf` 둘 다 적용. `zone` 없이 `resolve` 만
쓰면 `[emerg] resolving names at run time requires upstream "chandra" ...
to be in shared memory` 로 기동 실패.

### 검증 (throwaway `nginx:alpine` + busybox httpd 를 `--network-alias chandra-b` 로)

- chandra-b 이름이 없는 상태에서 **nginx 기동 OK**, `nginx -t` OK,
  요청 전부 chandra-a 로 200. 에러 로그에 30초마다
  `chandra-b could not be resolved (2: Server failure)` — 무해한 노이즈.
- alias 컨테이너 기동 → **9초 안에** 트래픽이 붙음 (`FAKE-B` 와 chandra-a
  응답이 least_conn 으로 번갈아 옴).
- alias 컨테이너 제거 → 다음 재해석(≤30초)까지 한 번 `upstream timed out
  ... 172.18.0.7:8000` 후 chandra-a 로 failover, 이후 전부 chandra-a.
  운영 conf 는 `proxy_connect_timeout 10s` 라 그 한 번이 10초 지연으로
  끝난다 (실패 아님).
- 운영 배포 후 chandra-b 부재 상태에서 reload 성공, 페이지 실패 0.

## 5. 교훈

- **"chandra-b 만 끄면 nginx 가 알아서 failover 한다" 는 IP 가 재사용되지
  않는 동안만 참이었다.** 메모리 `feedback_stop_chandra_b_minimal` 정정.
- 이제 chandra 컨테이너를 껐다 켜는 데 nginx reload 가 필요 없다.
  `mode-*.sh` 의 reload 는 `/llm/` location 유무 때문에 여전히 필요하고,
  그 자체는 문제 없다.
- 컨테이너 재생성 후 이상하면 **IP 가 누구 것이었는지** 먼저 본다.
  `docker inspect` 의 `IPAddress` + nginx 가 실제로 어디로 보내는지
  (여기서는 wrapper 로그에 찍힌 nginx 발 요청) 가 결정적 증거였다.

## 6. 후속

- `done_with_errors` 13건 (+ 고치는 중 24쪽 실패한 Hardy 1건) 은
  PaperMeister 에서 `force` 로 재제출 필요. 서버 쪽 자동 재처리는 없음.
- 03:54 의 cold-start 502 12쪽은 별건 — wrapper 재시도 창(110s) 이
  chandra 기동 시간보다 짧다는 기존 이슈 (`project_wrapper_retry_and_zombies`).
