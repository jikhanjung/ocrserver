# 20260520_017 — OCR 처리량 차트에 활성 구간 평균 표시 (wrapper 0.1.3)

## 변경

`/metrics` OCR 처리량 카드 헤더 우측에 표시 기간 평균을 텍스트로 추가:

```
OCR 처리량 — 페이지/분(좌) · 문서/분(우)        활성 구간 평균 24.3 페이지/분 · 0.42 문서/분
```

[[20260520_015_pages_throughput_metric]] 의 듀얼축 차트만으로는 "이 기간에
백엔드가 평균적으로 얼마나 일했나" 가 한눈에 안 들어와서, 사용자가 텍스트
요약 요청.

## 활성 구간 = 0 인 버킷 제외

첫 구현은 단순히 `총 페이지 / 전체 분` 으로 평균을 냈는데, idle 시간이 길수록
평균이 비현실적으로 작아짐 (서버는 24시간 켜져 있지만 실제 OCR 은 일부 시간만
돈다). 활성 버킷(`count > 0`)만 골라서 평균:

```js
function avgRate(arr) {
  let total = 0, n = 0;
  for (const v of arr) if (v && v > 0) { total += v; n++; }
  return n > 0 ? (total / n) * 60 / step : null;
}
```

`pages_per_step` 과 `jobs_per_step` 각각 독립 카운트 — 페이지가 처리된 버킷
수와 문서가 완료된 버킷 수가 다를 수 있음. 활성 버킷이 하나도 없으면 `—`
표시.

## 배포

이미지: `honestjung/ocrwrapper:0.1.3` (digest `ebc4d672c9fa...`) —
`:0.1.3` + `:latest` 동시 push 완료.

```bash
cd /srv/ocrserver && docker compose up -d wrapper
```

API 변경 없음 — `/api/metrics` 가 주는 데이터 그대로 클라이언트에서 집계.
