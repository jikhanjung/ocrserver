# Project instructions for Claude

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
