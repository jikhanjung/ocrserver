# 048 — 도판 워커 2개 동시 실행 (wrapper 0.3.7 → 0.3.8, worker 0.3.7)

**날짜**: 2026-09-29 · 관련: devlog 047(워커), `docs/FIGURE_LINK_STALLS.md` §4(서버 손잡이)

## 왜

파이프라인 조절은 거의 전부 클라이언트(PaperMeister) 몫이라 서버에 남은 처리량 지렛대는 워커 동시 처리 수뿐이었다
(HANDOFF 검토사항, 09-23). codex 구독 토큰 한도와 맞물려 보류했다가, 09-25~29 에 하루 500+ 호출이 무사고였던 것을 보고
사용자가 2개로 결정.

## 방식 — 프로세스 두 개

한 프로세스 안의 스레드가 아니라 **워커 프로세스를 두 개** 띄운다. PyMuPDF 는 스레드 세이프가 아니고(devlog 044),
워커의 `_current_proc`·`_stop` 같은 전역도 그대로 쓸 수 있다.

- systemd 템플릿 `ocrserver-figures-worker@.service`, 인스턴스 `@1`·`@2`. `FIGURES_WORKER_ID=%H-%i`
  (→ `jikhanserver-1`, `jikhanserver-2`). 옛 단일 유닛 `ocrserver-figures-worker.service` 는 disable.
- 간격 파일을 프로세스별로: `figure_ws/.next_call_at.<worker_id>`. 간격 60 s 는 프로세스마다 따로 → 시간당 호출 약 2배.
- 같은 논문 작업 폴더를 두 워커가 동시에 만들지 않게 `figure_ws/<hash>/<digest>/.lock` 에 `flock`. 프로세스 3개로 시험해 생성 1회.

## 서버 (wrapper 0.3.7)

- `figure_worker_procs` 테이블(워커 프로세스별 state·version·next_call_at·last_seen). `figure_worker`(id=1)는 전역 일시정지와
  마지막 접촉만. `/api/figures` 의 `worker` 는 집계: 일시정지 > running > sleeping > idle, `alive_count`·`running_count`·`workers[]`.
- `/figures` 페이지 배지 "실행 중 n/m", 남은 시간 추정을 살아 있는 워커 수로 나눔.
- **기존 버그 수정 — fatal 일시정지가 바로 풀리고 있었다.** `POST /internal/figures/worker/status` 가 state≠paused 이면
  `paused_reason=NULL` 로 덮었고, 워커는 fatal 결과 직후 `_sleep(POLL_INTERVAL, "idle")` 에서 바로 idle 을 보낸다.
  즉 사용량 한도·로그인 만료에 걸려도 30초마다 다시 claim 했다(시도 횟수는 안 깎여 항목 손실은 없었음). 워커가 둘이면
  서로의 정지를 풀게 된다. 이제 정지는 `POST /figures/worker/resume` 만 푼다. heartbeat·release 도 정지 상태를 덮지 않는다.

## 전환 직후 발견 — claim 경합 (wrapper 0.3.8)

20:39:48 에 두 워커가 동시에 시작해 **같은 항목(panels 4438)을 둘 다 claim** 했다. claim 이 "공평 분배로 고르기(SELECT)
→ processing 으로 표시(UPDATE)" 두 단계라, aiosqlite 한 연결 위에서도 두 요청의 await 사이에 끼어들 수 있었다.
둘 다 codex 를 돌렸고, 먼저 끝난 쪽 결과가 저장, 나중 쪽은 409 로 버려졌다(codex 호출 1회 낭비, 데이터 손상 없음).

고침: `UPDATE … WHERE item_id=? AND status='queued'` 로 표시하고 `rowcount==0` 이면 다시 고른다(최대 8회).
스모크에 병렬 claim 6개가 모두 다른 항목을 받는지 검사를 넣었다 — **옛 코드에서는 6개가 1개 항목**을 받아 실패,
새 코드는 통과(79 checks). 20:42 재배포 뒤 두 워커가 4439·4440 을 각각 잡는 것 확인.

## 운영

- 로그: `journalctl -u 'ocrserver-figures-worker@*' -f`
- 하나로 되돌리기: `sudo systemctl disable --now ocrserver-figures-worker@2`
- 사용량 한도에 걸리면 두 워커가 함께 멈춘다. 원인 해결 뒤 `POST /figures/worker/resume`.
- 옛 워커 id `jikhanserver` 행은 `figure_worker_procs` 에 남지만 3분 뒤 alive 에서 빠지고 24시간 뒤 목록에서도 빠진다.
- 이미지: `ocrwrapper:0.3.8`(Hub `0.3.8`+`latest`), 라이브 워커 스크립트 0.3.7(`/srv/ocrserver/scripts/figures_worker.py`).
