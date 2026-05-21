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
를 빌드하면 `snapshot_download(...)` 단계가 어느 % 에서 멈춰서 끝까지 안
가는 적이 있었다. 외부(다른 망의 머신, RunPod 등)에서 빌드해서 Docker Hub
로 push → 이 호스트에서 `docker pull` 하는 우회로를 사용한 적 있음.

따라서 chandra 이미지 신규 빌드가 필요할 때:
- 먼저 이 호스트에서 시도해보고 (cache hit 으로 1분 안에 끝나는 경우도 있음)
- 진행이 멈추거나 비현실적으로 느리면 외부 빌드 + Hub push 우회로로 전환
- wrapper 이미지(`honestjung/ocrwrapper`) 는 가벼워서 항상 호스트 빌드 OK

wrapper 와 달리 chandra Dockerfile 은 `RUN python3 -c "snapshot_download(...)"`
로 모델 가중치(~10GB) 를 build-time 에 받아서 image layer 에 굽는 구조 —
huggingface 트래픽이 buildkit 컨테이너 내부에서 일정 시점 이후 progress 가
안 나가는 패턴이 있다.

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
