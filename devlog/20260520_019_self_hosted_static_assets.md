# 20260520_019 — wrapper 페이지의 CDN 의존성 제거, 정적 자산 내장 (wrapper 0.1.5)

## 동기

dashboard/status/metrics HTML 이 4개 외부 CDN (jsdelivr) 에 의존해서, 호스트
나가는 길이 막히면(KOPRI 방화벽, jsdelivr 장애, 폐쇄망 데모) UI 가 깨짐.
이미지 안에 직접 내장.

## 번들 파일

`wrapper/static/` 에 3개. 합쳐 ~480KB:

| 파일 | 출처 | 크기 |
|---|---|---|
| `bootstrap.min.css` | bootstrap@5.3.3 | 228K |
| `chart.umd.min.js` | chart.js@4.4.1 | 201K |
| `chartjs-adapter-date-fns.bundle.min.js` | chartjs-adapter-date-fns@3.0.0 | 50K |

기존 metrics.html 이 `date-fns@2.30.0/index.min.js` 도 같이 로드했는데,
어댑터 `.bundle.min.js` 가 date-fns 를 자체 번들로 갖고 있어 standalone
date-fns 는 중복. 빼고 어댑터만 남김 → 한 요청 절감, 변경 후 차트
시간축은 그대로 동작.

## 라우팅

- `wrapper/main.py` 에 `app.mount("/static", StaticFiles(...))` 추가
- `wrapper/Dockerfile` 에 `COPY static ./static` 추가
- HTML 세 파일의 `https://cdn.jsdelivr.net/...` → `/static/...` 치환
- nginx 두 conf (`nginx.ocr.conf`, `nginx.llm.conf`) 에 `location /static/ {
  proxy_pass http://wrapper; }` 추가 — 안 그러면 catch-all `location /` 가
  chandra 업스트림으로 보내서 404.

## 배포 절차

dev tree 와 운영본 모두 `:0.1.5` 로 갱신 + 새 nginx conf 동기화 필요:

```bash
# 1. dev tree → 운영본 sync
cp /home/jikhanjung/projects/ocrserver/docker-compose.yml /srv/ocrserver/
cp /home/jikhanjung/projects/ocrserver/nginx.ocr.conf /srv/ocrserver/
cp /home/jikhanjung/projects/ocrserver/nginx.llm.conf /srv/ocrserver/
cp /srv/ocrserver/nginx.ocr.conf /srv/ocrserver/nginx.conf  # 현재 모드의 conf 도 갱신

# 2. wrapper 교체 + nginx reload
cd /srv/ocrserver && docker compose up -d wrapper \
    && docker compose exec nginx nginx -s reload

# 3. 확인
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/static/bootstrap.min.css
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/static/chart.umd.min.js
```

## 이미지

`honestjung/ocrwrapper:0.1.5` (digest `17d5cfbefbb5...`) — Docker Hub 에
`:0.1.5` + `:latest` 동시 push 완료.
