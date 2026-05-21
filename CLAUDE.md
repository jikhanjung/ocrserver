# Project instructions for Claude

## What this project is

**ocrserver** — OCR service for PDFs, built around the
[Chandra2 vision model](https://huggingface.co/datalab-to/chandra-ocr-2)
running on vLLM, with a FastAPI wrapper in front that does job queueing,
deduplication, status/metrics dashboards, and per-page progress tracking.

The base `Dockerfile` (repo root) bakes the model weights into
`honestjung/ocrserver:*` — that's the OCR backend image, distributed via
Docker Hub for use anywhere (local GPUs, RunPod, etc). See `README.md`.

The deployed wrapper service lives under `wrapper/` and produces a separate
image `honestjung/ocrwrapper:*`. It runs alongside one or two `chandra-*`
containers + nginx, all orchestrated by `docker compose`.

### Components

| Service | Image | Role |
|---|---|---|
| `wrapper` | `honestjung/ocrwrapper` | FastAPI: `/ocr` upload, `/api/*`, dashboard at `/`, `/status`, `/metrics` |
| `chandra-a` | `honestjung/ocrserver` | vLLM serving Chandra on GPU 0 (always on) |
| `chandra-b` | `honestjung/ocrserver` | vLLM serving Chandra on GPU 1 (`profiles: [ocr]`) |
| `llm` | `vllm/vllm-openai` | Qwen3-14B general LLM on GPU 1 (`profiles: [llm]`) |
| `nginx` | `nginx:alpine` | Front routing, with per-mode config (`nginx.ocr.conf` / `nginx.llm.conf`) |

### Operating modes

The host is a single RTX 8000 × 2 server. Two GPUs, three operating shapes:

- **OCR 2 GPU** (`mode-ocr.sh`) — chandra-a + chandra-b, no LLM. Max OCR throughput.
- **OCR + LLM** (`mode-llm.sh`) — chandra-a on GPU 0, Qwen3-14B on GPU 1.
- **LLM only** — rare, only if OCR is idle.

`mode-*.sh` swap `nginx.conf` to the right config and toggle compose profiles.
Current mode is visible in `/status` (mode chip) and at `_meta.mode` in
`/api/services`.

### Two source trees

- `/home/jikhanjung/projects/ocrserver/` — **dev tree** (this repo). Edit
  code/configs here, build wrapper image here.
- `/srv/ocrserver/` — **live deploy** (jikhanjung-owned). Where
  `docker compose` runs from. Compose file references prebuilt image tags
  (no `build:` field), so the deploy flow is: edit dev tree → build →
  bump tag in compose → `cp docker-compose.yml /srv/ocrserver/` →
  `docker compose up -d wrapper`.

### Where things are written down

- `README.md` — chandra image + standalone usage (model, env vars, API)
- `HANDOFF.md` — current session state, see below
- `devlog/YYYYMMDD_NNN_*.md` — chronological record of every meaningful
  change; the authoritative "why we did X"
- `docs/` — INSTALL_LOCAL, RUNPOD, ARCHITECTURE, ENDPOINTS, WRAPPER_API
- Auto-memory at `~/.claude/.../memory/` — durable preferences, gotchas,
  references; loaded into context automatically

## Known gotcha — chandra image build

이 호스트(KOPRI 망)에서 `docker build` 로 chandra 이미지(`honestjung/ocrserver`)
를 빌드하면 `snapshot_download(...)` 단계가 **12% (17개 파일 중 2번째)** 부근
에서 정확히 같은 지점에 멈춰서 끝까지 안 간다. 2026-05-21 세션에서 두 번
연속 같은 % 에서 stuck 확인. 호스트의 `curl` 로는 동일 파일 10MB/s 로 잘
받아져서 네트워크 자체 문제는 아니고, buildkit 내부에서 huggingface_hub 의
병렬 fetch 가 특정 청크 이후 hang.

HF_TOKEN 설정해도 다른 호스트는 토큰 없이 잘 빌드되니까 그게 근본 원인은
아닐 듯. KOPRI WAF/SSL MITM 과 buildkit network 의 상호작용이 의심되나
정확한 원인은 미상.

**우회로** (검증된 워크플로): 외부 머신(다른 망의 노트북, RunPod 등) 에서
빌드 → Docker Hub 로 `docker push honestjung/ocrserver:X.Y.Z` → 이 호스트
에서 `docker pull` 후 `docker compose up -d`.

따라서 chandra 이미지 신규 빌드가 필요할 때:
- 일단 이 호스트에서 시도해보고 (cache hit 이면 1분 안 끝남)
- 12% 부근에서 멈추면 즉시 kill 하고 외부 빌드 우회로로 전환
- 외부 빌드가 끝날 때까지 compose 의 chandra 참조는 기존 태그(예: `:0.1.0`)
  유지 — pullable 하지 않은 태그를 commit 하지 말 것
- wrapper 이미지(`honestjung/ocrwrapper`) 는 가벼워서 항상 호스트 빌드 OK

## Session start

**Read `HANDOFF.md` at the start of every session** to see the current dev
state — what was just shipped, what's currently running, what's pending.
That file is maintained per-session as the single source of "where things
stand right now"; without it, you'd have to reconstruct from `git log` +
`docker compose ps` + memory each time.

If `HANDOFF.md` looks stale (last update more than a session ago, or it
contradicts what you observe from `docker compose ps` / `git log`), say so
before acting on it.

## Session end

When a session ships meaningful state (new version, deploy, schema change,
notable decision), update `HANDOFF.md` before wrapping up. Keep the three
sections: 방금 한 작업 / 현재 프로젝트 상태 / 곧 해야 할 작업. Concrete
tags, container names, and dates — that's what makes it useful next time.
