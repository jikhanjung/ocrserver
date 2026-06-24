import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from contextlib import asynccontextmanager

import aiosqlite
import fitz
import httpx
import yaml
from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

# Role selects which side of the wrapper is active. Same image, two containers:
#   ocr  → dashboard, OCR job API, OCR worker, resume on startup. LLM DB read-only for /api/llm/*.
#   llm  → /v1/* proxy to vLLM with request/response logging. No OCR side at all.
WRAPPER_ROLE = os.getenv("WRAPPER_ROLE", "ocr")

DB_PATH = os.getenv("DB_PATH", "/data/ocrserver.db")
METRICS_DB_PATH = os.getenv("METRICS_DB_PATH", "/data/metrics.db")
LLM_DB_PATH = os.getenv("LLM_DB_PATH", "/data/llmserver.db")
PDF_DIR = os.getenv("PDF_DIR", "/data/pdfs")
VLLM_URL = os.getenv("VLLM_URL", "http://nginx:80")
VLLM_MODEL = os.getenv("VLLM_MODEL", "chandra")
LLM_UPSTREAM = os.getenv("LLM_UPSTREAM", "http://llm:8000")  # vLLM (model per compose), role=llm only
LLM_LOG_MAX_BYTES = int(os.getenv("LLM_LOG_MAX_BYTES", "65536"))  # truncate request/response text on insert
CONCURRENCY = int(os.getenv("OCR_CONCURRENCY", "12"))
DPI = int(os.getenv("OCR_DPI", "150"))
MAX_PAGE_PX = int(os.getenv("OCR_MAX_PAGE_PX", "2200"))  # cap longest side; chandra-ocr-2 max_model_len=12384
OCR_BACKENDS = [s.strip() for s in os.getenv("OCR_BACKENDS", "chandra-a,chandra-b").split(",") if s.strip()]
OCR_BACKEND_PORT = int(os.getenv("OCR_BACKEND_PORT", "8000"))
OCR_PER_BACKEND_CONCURRENCY = int(os.getenv("OCR_PER_BACKEND_CONCURRENCY", "6"))
COMPOSE_PATH = os.getenv("COMPOSE_PATH", "/etc/ocrserver-compose.yml")
MODE_TOKEN = os.getenv("MODE_TOKEN", "")  # empty disables /api/mode entirely
MODE_REQUEST_PATH = os.getenv("MODE_REQUEST_PATH", "/data/mode_request")
_RETRY_DELAYS = [5, 15, 30, 60]

# When True, the host-side switcher is about to recreate this wrapper container.
# /ocr POST rejects new jobs while this is set so we don't accept work that the
# fresh wrapper would have to resume mid-transition.
_mode_switching = False

_jobs: dict[str, dict] = {}
_sem: asyncio.Semaphore
_db: aiosqlite.Connection | None = None  # OCR DB (role=ocr only)
_metrics_db: aiosqlite.Connection | None = None
_metrics_db_lock = asyncio.Lock()
_llm_db: aiosqlite.Connection | None = None      # LLM DB RW (role=llm)
_llm_db_ro: aiosqlite.Connection | None = None   # LLM DB RO (role=ocr, for /api/llm/* read)
_start_time = time.time()


# ── DB helpers ────────────────────────────────────────────────────────────────

async def db_init() -> None:
    await _db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id       TEXT PRIMARY KEY,
            filename     TEXT,
            file_hash    TEXT,
            status       TEXT DEFAULT 'queued',
            submitted_at REAL,
            completed_at REAL,
            total_pages  INTEGER DEFAULT 0,
            done_pages   INTEGER DEFAULT 0,
            failed_pages INTEGER DEFAULT 0,
            error        TEXT
        );
        CREATE TABLE IF NOT EXISTS pages (
            job_id      TEXT,
            page_num    INTEGER,
            status      TEXT,
            duration_ms INTEGER,
            markdown    TEXT,
            error       TEXT,
            PRIMARY KEY (job_id, page_num),
            FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        );
    """)
    async with _db.execute("PRAGMA table_info(jobs)") as c:
        cols = {row[1] for row in await c.fetchall()}
    if "file_hash" not in cols:
        await _db.execute("ALTER TABLE jobs ADD COLUMN file_hash TEXT")
    if "client_id" not in cols:
        await _db.execute("ALTER TABLE jobs ADD COLUMN client_id TEXT")
    async with _db.execute("PRAGMA table_info(pages)") as c:
        page_cols = {row[1] for row in await c.fetchall()}
    if "completed_at" not in page_cols:
        await _db.execute("ALTER TABLE pages ADD COLUMN completed_at REAL")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_file_hash ON jobs(file_hash)")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_client_id ON jobs(client_id)")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_pages_completed_at ON pages(completed_at)")
    await _db.commit()


async def db_find_existing_by_hash(file_hash: str, client_id: str | None) -> dict | None:
    """Dedup hit for resubmission of the same file by the same client. Matches
    in-flight (queued/processing) as well as done — a client that lost its
    job_id (restart, page reload) shouldn't create a parallel duplicate of an
    already-running long job. Failed jobs are intentionally NOT matched so
    the client can retry. Done-jobs win when both exist."""
    async with _db.execute(
        "SELECT job_id FROM jobs WHERE file_hash=? AND client_id IS ? "
        "AND status IN ('done','processing','queued') "
        "ORDER BY (status='done') DESC, submitted_at DESC LIMIT 1",
        (file_hash, client_id),
    ) as c:
        row = await c.fetchone()
    return await db_get_job(row["job_id"]) if row else None


async def db_find_done_by_filename(filename: str, total_pages: int, file_hash: str,
                                   client_id: str | None) -> dict | None:
    async with _db.execute(
        "SELECT job_id FROM jobs WHERE filename=? AND total_pages=? AND file_hash IS NULL "
        "AND client_id IS ? AND status='done' ORDER BY completed_at DESC LIMIT 1",
        (filename, total_pages, client_id),
    ) as c:
        row = await c.fetchone()
    if not row:
        return None
    await _db.execute("UPDATE jobs SET file_hash=? WHERE job_id=?", (file_hash, row["job_id"]))
    await _db.commit()
    return await db_get_job(row["job_id"])


async def db_create_job(job_id: str, filename: str, file_hash: str, client_id: str | None,
                        submitted_at: float) -> None:
    await _db.execute(
        "INSERT INTO jobs (job_id, filename, file_hash, client_id, status, submitted_at) "
        "VALUES (?,?,?,?,'queued',?)",
        (job_id, filename, file_hash, client_id, submitted_at),
    )
    await _db.commit()


async def db_update_job(job_id: str, **kw) -> None:
    sets = ", ".join(f"{k}=?" for k in kw)
    await _db.execute(f"UPDATE jobs SET {sets} WHERE job_id=?", [*kw.values(), job_id])
    await _db.commit()


async def db_upsert_page(job_id: str, page_num: int, status: str,
                         duration_ms: int, markdown: str = None, error: str = None) -> None:
    await _db.execute(
        "INSERT OR REPLACE INTO pages "
        "(job_id, page_num, status, duration_ms, markdown, error, completed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, page_num, status, duration_ms, markdown, error, time.time()),
    )
    await _db.commit()


async def db_get_job(job_id: str) -> dict | None:
    async with _db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)) as c:
        row = await c.fetchone()
    if not row:
        return None
    job = dict(row)
    async with _db.execute(
        "SELECT * FROM pages WHERE job_id=? ORDER BY page_num", (job_id,)
    ) as c:
        prows = await c.fetchall()
    pages: list = [None] * (job["total_pages"] or 0)
    for pr in prows:
        p = dict(pr)
        n = p.pop("page_num")
        p["page"] = n
        if n < len(pages):
            pages[n] = p
    job["pages"] = pages
    return job


async def db_list_jobs(client_id: str | None = None) -> list[dict]:
    sql = ("SELECT job_id,filename,client_id,status,submitted_at,completed_at,"
           "total_pages,done_pages,failed_pages,error FROM jobs")
    params: tuple = ()
    if client_id is not None:
        sql += " WHERE client_id=?"
        params = (client_id,)
    sql += " ORDER BY submitted_at DESC"
    async with _db.execute(sql, params) as c:
        rows = await c.fetchall()
    return [dict(r) for r in rows]


async def db_page_jobs(client_id: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    where = "" if client_id is None else " WHERE client_id=?"
    base_params: tuple = () if client_id is None else (client_id,)
    async with _db.execute(f"SELECT COUNT(*) FROM jobs{where}", base_params) as c:
        total = (await c.fetchone())[0]
    sql = ("SELECT job_id,filename,client_id,status,submitted_at,completed_at,"
           f"total_pages,done_pages,failed_pages,error FROM jobs{where} "
           "ORDER BY submitted_at DESC LIMIT ? OFFSET ?")
    async with _db.execute(sql, base_params + (limit, offset)) as c:
        rows = await c.fetchall()
    return [dict(r) for r in rows], total


# ── LLM DB helpers ────────────────────────────────────────────────────────────
# llmserver.db is written by the WRAPPER_ROLE=llm container and read RO by the
# WRAPPER_ROLE=ocr container (so the dashboard can show recent LLM activity).

async def db_llm_init() -> None:
    # WAL + busy_timeout so a concurrent RO reader (the ocr wrapper) doesn't
    # ever block our writes. We're the sole writer, so contention should be ~0.
    await _llm_db.execute("PRAGMA journal_mode=WAL")
    await _llm_db.execute("PRAGMA busy_timeout=5000")
    await _llm_db.executescript("""
        CREATE TABLE IF NOT EXISTS llm_requests (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            submitted_at      REAL,
            completed_at      REAL,
            model             TEXT,
            endpoint          TEXT,
            client_ip         TEXT,
            request_json      TEXT,
            response_text     TEXT,
            prompt_tokens     INTEGER,
            completion_tokens INTEGER,
            total_tokens      INTEGER,
            latency_ms        INTEGER,
            http_status       INTEGER,
            status            TEXT,
            error             TEXT,
            streamed          INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_llm_submitted ON llm_requests(submitted_at);
    """)
    await _llm_db.commit()


def _truncate(s: str | None, limit: int = LLM_LOG_MAX_BYTES) -> str | None:
    if s is None:
        return None
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…[truncated {len(s) - limit} bytes]"


async def db_llm_insert(
    *,
    submitted_at: float, completed_at: float,
    model: str | None, endpoint: str, client_ip: str,
    request_json: str, response_text: str,
    prompt_tokens: int | None, completion_tokens: int | None, total_tokens: int | None,
    latency_ms: int, http_status: int, status: str,
    error: str | None, streamed: int,
) -> None:
    await _llm_db.execute(
        "INSERT INTO llm_requests "
        "(submitted_at, completed_at, model, endpoint, client_ip, request_json, response_text, "
        " prompt_tokens, completion_tokens, total_tokens, latency_ms, http_status, status, error, streamed) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (submitted_at, completed_at, model, endpoint, client_ip,
         _truncate(request_json), _truncate(response_text),
         prompt_tokens, completion_tokens, total_tokens,
         latency_ms, http_status, status, error, streamed),
    )
    await _llm_db.commit()


async def _get_llm_db_ro() -> aiosqlite.Connection | None:
    """Lazy RO open of llmserver.db. ocrwrapper may start before llmwrapper
    has created the file, and we don't want that race to leave /api/llm/*
    permanently empty."""
    global _llm_db_ro
    if _llm_db_ro is not None:
        return _llm_db_ro
    if not os.path.exists(LLM_DB_PATH):
        return None
    try:
        _llm_db_ro = await aiosqlite.connect(
            f"file:{LLM_DB_PATH}?mode=ro", uri=True
        )
        _llm_db_ro.row_factory = aiosqlite.Row
    except Exception as e:
        print(f"[llm-db-ro] open failed: {e}", flush=True)
        return None
    return _llm_db_ro


async def db_llm_recent(conn: aiosqlite.Connection, limit: int) -> list[dict]:
    async with conn.execute(
        "SELECT id, submitted_at, completed_at, model, endpoint, "
        "prompt_tokens, completion_tokens, total_tokens, latency_ms, "
        "http_status, status, error, streamed "
        "FROM llm_requests ORDER BY id DESC LIMIT ?",
        (limit,),
    ) as c:
        rows = await c.fetchall()
    return [dict(r) for r in rows]


async def db_llm_stats(conn: aiosqlite.Connection, since: float) -> dict:
    async with conn.execute(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_n, "
        "SUM(CASE WHEN status NOT IN ('ok','client_abort') THEN 1 ELSE 0 END) AS err_n, "
        "SUM(prompt_tokens) AS pt, SUM(completion_tokens) AS ct, SUM(total_tokens) AS tt, "
        "AVG(latency_ms) AS avg_latency_ms "
        "FROM llm_requests WHERE submitted_at >= ?",
        (since,),
    ) as c:
        row = await c.fetchone()
    return dict(row) if row else {}


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sem, _db, _llm_db, _llm_db_ro
    if WRAPPER_ROLE == "llm":
        # LLM proxy mode: only need llmserver.db RW. No OCR worker, no resume.
        os.makedirs(os.path.dirname(LLM_DB_PATH), exist_ok=True)
        _llm_db = await aiosqlite.connect(LLM_DB_PATH)
        _llm_db.row_factory = aiosqlite.Row
        await db_llm_init()
        print(f"[lifespan] role=llm, llm_db={LLM_DB_PATH}, upstream={LLM_UPSTREAM}", flush=True)
        yield
        await _llm_db.close()
        return

    # OCR mode (default): full OCR pipeline + read-only LLM DB for dashboard.
    _sem = asyncio.Semaphore(CONCURRENCY)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(PDF_DIR, exist_ok=True)
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await db_init()
    await _resume_processing_jobs()
    # Best-effort RO open of llmserver.db. Missing file just means the llm
    # wrapper hasn't run yet — /api/llm/* will report empty stats.
    if os.path.exists(LLM_DB_PATH):
        try:
            _llm_db_ro = await aiosqlite.connect(
                f"file:{LLM_DB_PATH}?mode=ro", uri=True
            )
            _llm_db_ro.row_factory = aiosqlite.Row
        except Exception as e:
            print(f"[lifespan] llm_db RO open failed: {e}", flush=True)
            _llm_db_ro = None
    yield
    await _db.close()
    if _metrics_db is not None:
        await _metrics_db.close()
    if _llm_db_ro is not None:
        await _llm_db_ro.close()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def _no_store_api(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)

_DASHBOARD = open(os.path.join(os.path.dirname(__file__), "dashboard.html")).read()
_STATUS_PAGE = open(os.path.join(os.path.dirname(__file__), "status.html")).read()
_METRICS_PAGE = open(os.path.join(os.path.dirname(__file__), "metrics.html")).read()

LLM_URL = os.getenv("LLM_URL", "")  # optional override; falls back to VLLM_URL/llm/


# ── Host metrics (collected externally by scripts/metrics_collector.py) ───────

_RANGE_MAP = {
    "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "3h": 10800, "6h": 21600, "12h": 43200,
    "24h": 86400, "1d": 86400,
    "3d": 259200, "7d": 604800, "30d": 2592000,
}
_STEP_LADDER = [60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400]
_METRIC_COLS = [
    "load1", "load5", "load15", "mem_used_mb", "mem_total_mb",
    "gpu0_util", "gpu0_temp", "gpu0_mem_used_mb", "gpu0_mem_total_mb",
    "gpu1_util", "gpu1_temp", "gpu1_mem_used_mb", "gpu1_mem_total_mb",
]


def _auto_step(span_secs: int, target_points: int = 360) -> int:
    raw = max(60, span_secs // max(target_points, 1))
    for s in _STEP_LADDER:
        if raw <= s:
            return s
    return _STEP_LADDER[-1]


async def _get_metrics_db() -> aiosqlite.Connection | None:
    """Lazy-open a read-only handle to metrics.db. Returns None if the file
    doesn't exist yet (collector hasn't fired)."""
    global _metrics_db
    if _metrics_db is not None:
        return _metrics_db
    if not os.path.exists(METRICS_DB_PATH):
        return None
    async with _metrics_db_lock:
        if _metrics_db is None and os.path.exists(METRICS_DB_PATH):
            _metrics_db = await aiosqlite.connect(METRICS_DB_PATH)
            _metrics_db.row_factory = aiosqlite.Row
    return _metrics_db


@app.get("/api/metrics")
async def api_metrics(
    range_: str = Query("1h", alias="range"),
    step: int | None = None,
):
    now = int(time.time())
    range_secs = _RANGE_MAP.get(range_)
    if range_ == "all":
        range_secs = None
    mdb = await _get_metrics_db()

    if range_secs is None:
        frm = now - 3600
        if mdb:
            async with mdb.execute("SELECT MIN(ts) AS m FROM metrics") as c:
                row = await c.fetchone()
            if row and row["m"]:
                frm = int(row["m"])
    else:
        frm = now - range_secs

    if step is None or step < 60:
        step = _auto_step(now - frm)

    ts_grid = list(range(frm - frm % step, now + 1, step))

    series: dict[str, list] = {"ts": ts_grid}
    for col in _METRIC_COLS:
        series[col] = [None] * len(ts_grid)
    series["jobs_per_step"] = [0] * len(ts_grid)
    series["pages_per_step"] = [0] * len(ts_grid)
    series["step"] = step

    if mdb:
        avg_cols = ", ".join(f"AVG({c}) AS {c}" for c in _METRIC_COLS)
        sql = (
            f"SELECT (ts/?)*? AS bucket, {avg_cols} "
            "FROM metrics WHERE ts >= ? AND ts <= ? "
            "GROUP BY bucket ORDER BY bucket"
        )
        async with mdb.execute(sql, (step, step, frm, now)) as c:
            rows = await c.fetchall()
        by_bucket = {int(r["bucket"]): r for r in rows}
        for i, t in enumerate(ts_grid):
            r = by_bucket.get(t)
            if not r:
                continue
            for col in _METRIC_COLS:
                v = r[col]
                if v is None:
                    continue
                series[col][i] = round(v, 2) if isinstance(v, float) else v

    # jobs/step: count completions per bucket from ocrserver.db
    sql = (
        "SELECT (CAST(completed_at AS INTEGER)/?)*? AS bucket, COUNT(*) AS n "
        "FROM jobs WHERE completed_at >= ? AND completed_at <= ? "
        "AND status IN ('done','done_with_errors') "
        "GROUP BY bucket"
    )
    async with _db.execute(sql, (step, step, frm, now)) as c:
        jrows = await c.fetchall()
    jobs_by_bucket = {int(r["bucket"]): int(r["n"]) for r in jrows}
    for i, t in enumerate(ts_grid):
        if t in jobs_by_bucket:
            series["jobs_per_step"][i] = jobs_by_bucket[t]

    # pages/step: count successful page completions per bucket
    sql = (
        "SELECT (CAST(completed_at AS INTEGER)/?)*? AS bucket, COUNT(*) AS n "
        "FROM pages WHERE completed_at >= ? AND completed_at <= ? "
        "AND status='ok' "
        "GROUP BY bucket"
    )
    async with _db.execute(sql, (step, step, frm, now)) as c:
        prows = await c.fetchall()
    pages_by_bucket = {int(r["bucket"]): int(r["n"]) for r in prows}
    for i, t in enumerate(ts_grid):
        if t in pages_by_bucket:
            series["pages_per_step"][i] = pages_by_bucket[t]

    return {
        "from": frm, "to": now, "step": step,
        "range": range_,
        "available": mdb is not None,
        "series": series,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _DASHBOARD


@app.get("/status", response_class=HTMLResponse)
async def status_page():
    return _STATUS_PAGE


@app.get("/metrics", response_class=HTMLResponse)
async def metrics_page():
    return _METRICS_PAGE


async def _probe_url(client: httpx.AsyncClient, url: str) -> dict:
    try:
        r = await client.get(url)
        return {"status": "ok" if r.status_code == 200 else "error",
                "http_status": r.status_code}
    except Exception as e:
        return {"status": "down", "error": str(e)}


# Cached snapshot of backend health, refreshed on demand at most every TTL seconds.
# Shared by /api/stats (hot polled) and /api/services (detail view) so we don't
# probe on every request.
_PROBE_TTL = 5.0
_probe_cache = {"at": 0.0, "ocr": {}, "ocr_alive": 0, "llm": None, "chandra": None}
_probe_lock = asyncio.Lock()

# compose images cache — read /srv/ocrserver/docker-compose.yml (mounted RO)
# to surface configured image tags per service on the status page.
_COMPOSE_TTL = 60.0
_compose_cache: dict = {"at": 0.0, "images": {}, "llm_model": None, "llm_gpus": 1}


def _parse_llm_gpus(llm_svc: dict, toks: list) -> int:
    """How many GPUs the llm service is configured for. Primary signal is the
    --tensor-parallel-size flag in its command; fallback is the count of
    reserved device_ids in its deploy block. Defaults to 1."""
    for i, t in enumerate(toks):
        if not isinstance(t, str):
            continue
        if t in ("--tensor-parallel-size", "-tp") and i + 1 < len(toks):
            try:
                return max(1, int(toks[i + 1]))
            except (ValueError, TypeError):
                pass
        if t.startswith("--tensor-parallel-size="):
            try:
                return max(1, int(t.split("=", 1)[1]))
            except (ValueError, TypeError):
                pass
    try:
        devices = ((llm_svc.get("deploy") or {}).get("resources") or {}) \
            .get("reservations", {}).get("devices") or []
        n = sum(len(d.get("device_ids") or []) for d in devices)
        if n:
            return n
    except (AttributeError, TypeError):
        pass
    return 1


def _refresh_compose_cache() -> dict:
    """Parse compose once per TTL: per-service image tags + the llm service's
    configured model (first non-flag token of its command) + its GPU count."""
    if time.time() - _compose_cache["at"] < _COMPOSE_TTL:
        return _compose_cache
    images: dict[str, str] = {}
    llm_model = None
    llm_gpus = 1
    try:
        with open(COMPOSE_PATH) as f:
            data = yaml.safe_load(f) or {}
        services = data.get("services") or {}
        for name, svc in services.items():
            img = svc.get("image")
            if img:
                images[name] = img
        llm_svc = services.get("llm") or {}
        cmd = llm_svc.get("command")
        toks = cmd.split() if isinstance(cmd, str) else (cmd or [])
        for t in toks:
            if isinstance(t, str) and t and not t.startswith("-"):
                llm_model = t
                break
        llm_gpus = _parse_llm_gpus(llm_svc, toks)
    except Exception:
        pass
    _compose_cache.update(at=time.time(), images=images,
                          llm_model=llm_model, llm_gpus=llm_gpus)
    return _compose_cache


def _read_compose_images() -> dict[str, str]:
    return _refresh_compose_cache()["images"]


def _read_compose_llm_model() -> str | None:
    return _refresh_compose_cache()["llm_model"]


def _read_compose_llm_gpus() -> int:
    return _refresh_compose_cache()["llm_gpus"]


async def _refresh_probe_cache() -> dict:
    if time.time() - _probe_cache["at"] < _PROBE_TTL:
        return _probe_cache
    async with _probe_lock:
        if time.time() - _probe_cache["at"] < _PROBE_TTL:
            return _probe_cache
        llm_base = LLM_URL.rstrip("/") if LLM_URL else f"{VLLM_URL.rstrip('/')}/llm"
        chandra_url = f"{VLLM_URL.rstrip('/')}/health"
        llm_url = f"{llm_base}/health"
        async with httpx.AsyncClient(timeout=2) as client:
            chandra_res = await _probe_url(client, chandra_url)
            llm_res = await _probe_url(client, llm_url)
            ocr_results = {}
            alive = 0
            for name in OCR_BACKENDS:
                res = await _probe_url(client, f"http://{name}:{OCR_BACKEND_PORT}/health")
                ocr_results[name] = res
                if res["status"] == "ok":
                    alive += 1
        _probe_cache.update(at=time.time(), ocr=ocr_results, ocr_alive=alive,
                            llm=llm_res, chandra=chandra_res,
                            chandra_url=chandra_url, llm_url=llm_url)
    return _probe_cache


def _mode_from_probes(cache: dict, llm_gpus: int = 1) -> str:
    """Operational mode label inferred from which services are alive. llm_gpus
    (from compose) distinguishes single-GPU LLM-only ('llm') from a 2-GPU
    tensor-parallel LLM occupying both GPUs ('llmx2')."""
    n = cache["ocr_alive"]
    llm_ok = bool(cache["llm"] and cache["llm"]["status"] == "ok")
    if n == 0:
        if not llm_ok:
            return "down"
        return "llmx2" if llm_gpus >= 2 else "llm"
    ocr_part = "ocr" if (n == 1 and llm_ok) else f"{n}ocr"
    return f"llm+{ocr_part}" if llm_ok else ocr_part


@app.get("/api/services")
async def api_services():
    cache = await _refresh_probe_cache()
    return {
        "chandra": cache["chandra"],
        "llm": cache["llm"],
        "ocr_backends": {
            "alive": cache["ocr_alive"],
            "total": len(OCR_BACKENDS),
            "per_backend_concurrency": OCR_PER_BACKEND_CONCURRENCY,
            "recommended_concurrency": cache["ocr_alive"] * OCR_PER_BACKEND_CONCURRENCY,
            "per_backend": cache["ocr"],
        },
        "_meta": {
            "chandra_url": cache.get("chandra_url"),
            "llm_url": cache.get("llm_url"),
            "concurrency": CONCURRENCY,
            "mode": _mode_from_probes(cache, _read_compose_llm_gpus()),
            "probe_age_s": round(time.time() - cache["at"], 1),
            "uptime_s": int(time.time() - _start_time),
            "images": _read_compose_images(),
            "llm_model": _read_compose_llm_model(),
            "llm_gpus": _read_compose_llm_gpus(),
        },
    }


@app.get("/api/jobs")
async def api_jobs(
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    client_id: str | None = Query(None),
):
    offset = (page - 1) * per_page
    rows, total = await db_page_jobs(client_id, per_page, offset)
    # Overlay in-memory state for queued/processing jobs so progress feels live.
    items = []
    for j in rows:
        jid = j["job_id"]
        if jid in _jobs and _jobs[jid]["status"] in ("queued", "processing"):
            items.append({k: v for k, v in _jobs[jid].items() if k != "pages"})
        else:
            items.append(j)
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if total else 0,
    }


@app.get("/api/stats")
async def api_stats():
    jobs = await db_list_jobs()
    counts: dict[str, int] = {
        "total": len(jobs), "queued": 0, "processing": 0,
        "done": 0, "done_with_errors": 0, "failed": 0,
    }
    for j in jobs:
        s = j["status"]
        if s in counts:
            counts[s] += 1
    cache = await _refresh_probe_cache()
    return {
        "counts": counts,
        "ocr_backends_alive": cache["ocr_alive"],
        "ocr_backends_total": len(OCR_BACKENDS),
        "recommended_concurrency": cache["ocr_alive"] * OCR_PER_BACKEND_CONCURRENCY,
        "mode": _mode_from_probes(cache, _read_compose_llm_gpus()),
        "uptime_s": int(time.time() - _start_time),
        "concurrency": CONCURRENCY,
        "vllm_url": VLLM_URL,
    }


@app.post("/api/mode")
async def api_mode(
    payload: dict,
    x_mode_token: str | None = Header(None, alias="X-Mode-Token"),
):
    """Request a host-side mode switch (ocr | llm). The host's systemd path
    unit watches MODE_REQUEST_PATH and runs mode-{ocr,llm}.sh which itself
    recreates the wrapper container. We set _mode_switching so that /ocr
    submits get 503 until the recreate."""
    global _mode_switching
    if not MODE_TOKEN:
        raise HTTPException(status_code=503, detail="mode switching disabled (no MODE_TOKEN)")
    if x_mode_token != MODE_TOKEN:
        raise HTTPException(status_code=403, detail="invalid token")
    mode = (payload or {}).get("mode")
    if mode not in ("ocr", "llm"):
        raise HTTPException(status_code=400, detail="mode must be 'ocr' or 'llm'")
    os.makedirs(os.path.dirname(MODE_REQUEST_PATH), exist_ok=True)
    with open(MODE_REQUEST_PATH, "w") as f:
        f.write(f"{mode}\n")
    _mode_switching = True
    return {"requested": mode, "switching": True}


@app.get("/api/mode")
async def api_mode_status():
    return {
        "enabled": bool(MODE_TOKEN),
        "switching": _mode_switching,
    }


@app.post("/ocr")
async def submit(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    client_id: str | None = Form(None),
    total_pages: int | None = Form(None),
    force: bool = Form(False),
    x_client_id: str | None = Header(None),
):
    if _mode_switching:
        raise HTTPException(status_code=503, detail="mode switch in progress, please retry shortly")
    if client_id is None:
        client_id = x_client_id
    pdf_bytes = await file.read()
    # Move sync CPU/IO out of the event loop — for a 500MB PDF the hash
    # (~1-2s), the optional fitz parse (~1-2s), and the disk write (~2-5s)
    # otherwise serialize the loop and freeze the dashboard during upload.
    file_hash = await asyncio.to_thread(
        lambda: hashlib.sha256(pdf_bytes).hexdigest())

    # Trust the client's page-count hint when provided so it can size its
    # in-flight queue from the first poll (PaperMeister sends this). When
    # absent, parse the PDF here. Either way, _run() later overwrites the
    # value with what _render_pdf() actually sees, so a wrong hint self-
    # heals within seconds.
    if total_pages is None or total_pages <= 0:
        def _count_pages(b: bytes) -> int:
            try:
                doc = fitz.open(stream=b, filetype="pdf")
                try:
                    return len(doc)
                finally:
                    doc.close()
            except Exception:
                return 0
        total_pages = await asyncio.to_thread(_count_pages, pdf_bytes)

    pdf_path = os.path.join(PDF_DIR, f"{file_hash}.pdf")
    if not os.path.exists(pdf_path):
        def _write(path: str, data: bytes) -> None:
            with open(path, "wb") as f:
                f.write(data)
        await asyncio.to_thread(_write, pdf_path, pdf_bytes)

    # force=true: skip dedup completely and re-OCR the file under a fresh
    # job_id. Use case: a previous result has blank/whitespace-only pages
    # that the client wants regenerated. Keeps the old job row (and its
    # markdown) untouched — newer job wins subsequent dedup lookups.
    existing = None if force else (
        await db_find_existing_by_hash(file_hash, client_id) or
        await db_find_done_by_filename(file.filename, total_pages, file_hash, client_id)
    )
    if existing:
        return {
            "job_id": existing["job_id"],
            "cached": True,
            "in_progress": existing.get("status") != "done",
            "total_pages": existing.get("total_pages", 0),
        }

    job_id = str(uuid.uuid4())
    now = time.time()
    _jobs[job_id] = {
        "job_id": job_id,
        "filename": file.filename,
        "file_hash": file_hash,
        "client_id": client_id,
        "status": "queued",
        "submitted_at": now,
        "total_pages": total_pages,
        "done_pages": 0,
        "failed_pages": 0,
        "pages": [],
    }
    await db_create_job(job_id, file.filename, file_hash, client_id, now)
    if total_pages:
        await db_update_job(job_id, total_pages=total_pages)
    background_tasks.add_task(_run, job_id, pdf_bytes)
    return {"job_id": job_id, "cached": False, "forced": force, "total_pages": total_pages}


@app.get("/ocr")
async def list_jobs(client_id: str | None = Query(None)):
    db_jobs = await db_list_jobs(client_id=client_id)
    result = []
    for j in db_jobs:
        jid = j["job_id"]
        if jid in _jobs and _jobs[jid]["status"] in ("queued", "processing"):
            result.append({k: v for k, v in _jobs[jid].items() if k != "pages"})
        else:
            result.append(j)
    return result


@app.get("/ocr/{job_id}")
async def get_job(job_id: str):
    if job_id in _jobs:
        return _jobs[job_id]
    job = await db_get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/health")
async def health():
    return {}


# ── LLM read API (role=ocr, reads llmserver.db RO) ────────────────────────────

if WRAPPER_ROLE == "ocr":

    @app.get("/api/llm/recent")
    async def api_llm_recent(limit: int = Query(20, ge=1, le=200)):
        conn = await _get_llm_db_ro()
        if conn is None:
            return {"items": [], "available": False}
        items = await db_llm_recent(conn, limit)
        return {"items": items, "available": True}

    @app.get("/api/llm/stats")
    async def api_llm_stats(range_: str = Query("24h", alias="range")):
        conn = await _get_llm_db_ro()
        if conn is None:
            return {"available": False, "range": range_, "counts": {}, "tokens": {}}
        span = _RANGE_MAP.get(range_, 86400)
        since = time.time() - span
        s = await db_llm_stats(conn, since)
        n = s.get("n") or 0
        ok_n = s.get("ok_n") or 0
        err_n = s.get("err_n") or 0
        return {
            "available": True,
            "range": range_,
            "since": since,
            "counts": {
                "total": n,
                "ok": ok_n,
                "error": err_n,
                "error_rate": (err_n / n) if n else 0.0,
            },
            "tokens": {
                "prompt": s.get("pt") or 0,
                "completion": s.get("ct") or 0,
                "total": s.get("tt") or 0,
            },
            "avg_latency_ms": int(s.get("avg_latency_ms") or 0),
        }


# ── LLM proxy (role=llm) ──────────────────────────────────────────────────────
# Forwards /v1/* to LLM_UPSTREAM and logs each request to llm_requests. SSE
# streams are passed through line-by-line while content/usage is accumulated
# in the background for the final DB row.

def _extract_completion_text(resp_json: dict) -> str:
    """Best-effort: chat/completions → choices[0].message.content,
    legacy completions → choices[0].text, else empty."""
    choices = resp_json.get("choices") or []
    if not choices:
        return ""
    c0 = choices[0]
    msg = c0.get("message") or {}
    if isinstance(msg.get("content"), str):
        return msg["content"]
    if isinstance(c0.get("text"), str):
        return c0["text"]
    return ""


def _log_llm_safe(coro_kwargs: dict) -> None:
    """Schedule a DB insert without awaiting — used from streaming generators
    where awaiting in a finally-block during client-disconnect can race with
    the event loop shutdown path."""
    async def _go():
        try:
            await db_llm_insert(**coro_kwargs)
        except Exception as e:
            print(f"[llm-log] insert failed: {e}", flush=True)
    asyncio.create_task(_go())


if WRAPPER_ROLE == "llm":

    @app.api_route("/v1/{path:path}", methods=["GET", "POST"])
    async def llm_proxy(path: str, request: Request):
        upstream_url = f"{LLM_UPSTREAM.rstrip('/')}/v1/{path}"
        endpoint = f"/v1/{path}"
        client_ip = request.client.host if request.client else ""
        method = request.method

        # GET (e.g. /v1/models): simple passthrough, not logged.
        if method == "GET":
            async with httpx.AsyncClient(timeout=30) as client:
                try:
                    r = await client.get(upstream_url, params=request.query_params)
                except Exception as e:
                    raise HTTPException(status_code=502, detail=f"upstream: {e}")
            return Response(
                content=r.content, status_code=r.status_code,
                media_type=r.headers.get("Content-Type", "application/json"),
            )

        # POST: read body, decide streaming, forward.
        body = await request.body()
        try:
            req_obj = json.loads(body) if body else {}
        except Exception:
            req_obj = {}
        model = req_obj.get("model") if isinstance(req_obj, dict) else None
        stream = bool(isinstance(req_obj, dict) and req_obj.get("stream"))

        # Ask vLLM to include token usage in the final streamed chunk so we
        # can record prompt/completion_tokens even for streamed responses.
        if stream and isinstance(req_obj, dict) and "stream_options" not in req_obj:
            req_obj["stream_options"] = {"include_usage": True}
            body = json.dumps(req_obj).encode()

        submitted_at = time.time()
        req_text = body.decode("utf-8", errors="replace")
        upstream_headers = {"Content-Type": "application/json"}

        if not stream:
            async with httpx.AsyncClient(timeout=600) as client:
                try:
                    r = await client.post(upstream_url, content=body, headers=upstream_headers)
                except Exception as e:
                    await db_llm_insert(
                        submitted_at=submitted_at, completed_at=time.time(),
                        model=model, endpoint=endpoint, client_ip=client_ip,
                        request_json=req_text, response_text="",
                        prompt_tokens=None, completion_tokens=None, total_tokens=None,
                        latency_ms=int((time.time() - submitted_at) * 1000),
                        http_status=0, status="error", error=str(e), streamed=0,
                    )
                    raise HTTPException(status_code=502, detail=f"upstream: {e}")
            latency_ms = int((time.time() - submitted_at) * 1000)
            usage: dict = {}
            content = ""
            try:
                resp_json = r.json()
                if isinstance(resp_json, dict):
                    usage = resp_json.get("usage") or {}
                    content = _extract_completion_text(resp_json)
            except Exception:
                content = r.text
            await db_llm_insert(
                submitted_at=submitted_at, completed_at=time.time(),
                model=model, endpoint=endpoint, client_ip=client_ip,
                request_json=req_text, response_text=content,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                latency_ms=latency_ms, http_status=r.status_code,
                status="ok" if r.is_success else "error",
                error=None if r.is_success else r.text[:500],
                streamed=0,
            )
            return Response(
                content=r.content, status_code=r.status_code,
                media_type=r.headers.get("Content-Type", "application/json"),
            )

        # Streaming: tee SSE lines downstream while accumulating content + usage.
        async def gen():
            accumulated: list[str] = []
            usage: dict = {}
            upstream_status = 0
            error_msg: str | None = None
            ended_normally = False
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "POST", upstream_url, content=body, headers=upstream_headers
                    ) as r:
                        upstream_status = r.status_code
                        if not r.is_success:
                            chunk = await r.aread()
                            yield chunk
                            return
                        async for line in r.aiter_lines():
                            # SSE: "data: {...}" lines separated by blank lines.
                            # aiter_lines() strips the trailing \n, so we add
                            # back \n + the SSE blank-line separator.
                            yield (line + "\n").encode()
                            if line.startswith("data: "):
                                payload = line[6:]
                                if payload.strip() == "[DONE]":
                                    continue
                                try:
                                    obj = json.loads(payload)
                                except Exception:
                                    continue
                                if isinstance(obj, dict):
                                    if obj.get("usage"):
                                        usage = obj["usage"]
                                    for choice in obj.get("choices") or []:
                                        delta = choice.get("delta") or {}
                                        if isinstance(delta.get("content"), str):
                                            accumulated.append(delta["content"])
                                        elif isinstance(choice.get("text"), str):
                                            accumulated.append(choice["text"])
                ended_normally = True
            except (asyncio.CancelledError, GeneratorExit):
                error_msg = "client_abort"
                raise
            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
            finally:
                if error_msg == "client_abort":
                    status = "client_abort"
                elif error_msg:
                    status = "error"
                elif upstream_status and upstream_status >= 400:
                    status = "error"
                elif ended_normally:
                    status = "ok"
                else:
                    status = "error"
                _log_llm_safe(dict(
                    submitted_at=submitted_at, completed_at=time.time(),
                    model=model, endpoint=endpoint, client_ip=client_ip,
                    request_json=req_text,
                    response_text="".join(accumulated),
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                    latency_ms=int((time.time() - submitted_at) * 1000),
                    http_status=upstream_status, status=status,
                    error=error_msg, streamed=1,
                ))

        return StreamingResponse(gen(), media_type="text/event-stream")


# ── Background processing ─────────────────────────────────────────────────────

def _pdf_page_count(pdf_bytes: bytes) -> int:
    """Open the PDF just long enough to read page count. Cheap (~ms even for
    big PDFs) but still wrapped in to_thread when called so we don't pay even
    that on the event loop."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return len(doc)
    finally:
        doc.close()


def _render_one_page(pdf_bytes: bytes, page_num: int) -> str:
    """Render a single page to base64-JPEG. Called from inside _ocr_page so
    render concurrency naturally caps at the OCR semaphore (CONCURRENCY) —
    no separate render burst, no need for a render-only semaphore."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[page_num]
        long_pt = max(page.rect.width, page.rect.height)
        dpi = min(DPI, MAX_PAGE_PX * 72 / long_pt) if long_pt > 0 else DPI
        zoom = dpi / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        return base64.b64encode(pix.tobytes("jpeg")).decode()
    finally:
        doc.close()


async def _run(job_id: str, pdf_bytes: bytes, skip_pages: set[int] | None = None) -> None:
    """Process all pages of a PDF. If skip_pages is given, those page indices
    are not re-submitted (used by lifespan resume). Rendering happens inside
    each _ocr_page worker so it's paced by the OCR semaphore — the event loop
    stays responsive even for 1000+ page books."""
    skip_pages = skip_pages or set()
    job = _jobs[job_id]
    try:
        n = await asyncio.to_thread(_pdf_page_count, pdf_bytes)
    except Exception as e:
        msg = f"failed to open PDF: {e}"
        job.update(status="failed", error=msg)
        await db_update_job(job_id, status="failed", error=msg, completed_at=time.time())
        return

    if not skip_pages:
        # fresh job: initialize page array & total_pages in DB
        job.update(total_pages=n, status="processing", pages=[None] * n)
        await db_update_job(job_id, status="processing", total_pages=n)

    todo = [i for i in range(n) if i not in skip_pages]
    async with httpx.AsyncClient(timeout=3000) as client:
        await asyncio.gather(*[_ocr_page(job, i, pdf_bytes, client) for i in todo])

    completed_at = time.time()
    status = "done" if job["failed_pages"] == 0 else "done_with_errors"
    job["status"] = status
    await db_update_job(
        job_id, status=status,
        done_pages=job["done_pages"], failed_pages=job["failed_pages"],
        completed_at=completed_at,
    )


async def _resume_processing_jobs() -> None:
    """On wrapper startup, re-spawn _run for any DB row stuck in 'processing'.
    Pages already marked 'ok' are skipped; failed/missing pages are re-rendered."""
    async with _db.execute(
        "SELECT job_id, filename, file_hash, client_id, total_pages, submitted_at "
        "FROM jobs WHERE status='processing'"
    ) as c:
        rows = [dict(r) for r in await c.fetchall()]

    for row in rows:
        jid = row["job_id"]
        fh = row["file_hash"]
        if not fh:
            await db_update_job(jid, status="failed",
                                error="resume failed: no file_hash",
                                completed_at=time.time())
            continue
        path = os.path.join(PDF_DIR, f"{fh}.pdf")
        if not os.path.exists(path):
            await db_update_job(jid, status="failed",
                                error=f"resume failed: missing {path}",
                                completed_at=time.time())
            continue

        # Restore page-level state from DB
        async with _db.execute(
            "SELECT page_num, status, duration_ms, markdown, error FROM pages WHERE job_id=?",
            (jid,),
        ) as c2:
            page_rows = [dict(r) for r in await c2.fetchall()]
        done_set = {p["page_num"] for p in page_rows if p["status"] == "ok"}

        n = row["total_pages"] or 0
        pages = [None] * n
        for p in page_rows:
            i = p["page_num"]
            if i >= n:
                continue
            entry = {"page": i, "status": p["status"], "duration_ms": p["duration_ms"]}
            if p["status"] == "ok":
                entry["markdown"] = p["markdown"]
            else:
                entry["error"] = p["error"]
            # Only keep 'ok' entries; failed slots stay None so _ocr_page will refill
            pages[i] = entry if p["status"] == "ok" else None

        with open(path, "rb") as f:
            pdf_bytes = f.read()

        _jobs[jid] = {
            "job_id": jid,
            "filename": row["filename"],
            "file_hash": fh,
            "client_id": row.get("client_id"),
            "status": "processing",
            "submitted_at": row.get("submitted_at") or time.time(),
            "total_pages": n,
            "done_pages": len(done_set),
            "failed_pages": 0,
            "pages": pages,
        }
        # Reset failed_pages count in DB to 0 since we're re-attempting them
        await db_update_job(jid, done_pages=len(done_set), failed_pages=0)
        asyncio.create_task(_run(jid, pdf_bytes, skip_pages=done_set))

    if rows:
        print(f"[resume] re-spawned {len(rows)} 'processing' job(s)", flush=True)


async def _ocr_page(job: dict, page_num: int, pdf_bytes: bytes,
                    client: httpx.AsyncClient) -> None:
    async with _sem:
        t0 = time.time()
        last_error = ""
        try:
            b64 = await asyncio.to_thread(_render_one_page, pdf_bytes, page_num)
        except Exception as e:
            duration_ms = int((time.time() - t0) * 1000)
            err = f"render failed: {e}"
            job["pages"][page_num] = {"page": page_num, "error": err,
                                       "duration_ms": duration_ms, "status": "failed"}
            job["failed_pages"] += 1
            await db_upsert_page(job["job_id"], page_num, "failed", duration_ms, error=err)
            await db_update_job(job["job_id"], failed_pages=job["failed_pages"])
            return
        for attempt, delay in enumerate([0] + _RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                resp = await client.post(
                    f"{VLLM_URL}/v1/chat/completions",
                    json={
                        "model": VLLM_MODEL,
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": "Extract all text from this page in markdown."},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        ]}],
                        "max_tokens": 8192,
                    },
                )
                if resp.status_code in (502, 503):
                    last_error = f"HTTP {resp.status_code}"
                    continue
                resp.raise_for_status()
                markdown = resp.json()["choices"][0]["message"]["content"]
                duration_ms = int((time.time() - t0) * 1000)
                job["pages"][page_num] = {"page": page_num, "markdown": markdown,
                                           "duration_ms": duration_ms, "status": "ok"}
                job["done_pages"] += 1
                await db_upsert_page(job["job_id"], page_num, "ok", duration_ms, markdown=markdown)
                await db_update_job(job["job_id"], done_pages=job["done_pages"])
                return
            except httpx.HTTPStatusError as e:
                last_error = str(e)
                break
            except Exception as e:
                last_error = str(e)
                continue

        duration_ms = int((time.time() - t0) * 1000)
        job["pages"][page_num] = {"page": page_num, "error": last_error,
                                   "duration_ms": duration_ms, "status": "failed"}
        job["failed_pages"] += 1
        await db_upsert_page(job["job_id"], page_num, "failed", duration_ms, error=last_error)
        await db_update_job(job["job_id"], failed_pages=job["failed_pages"])
