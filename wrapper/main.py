import asyncio
import base64
import hashlib
import os
import time
import uuid
from contextlib import asynccontextmanager

import aiosqlite
import fitz
import httpx
import yaml
from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

DB_PATH = os.getenv("DB_PATH", "/data/ocrserver.db")
METRICS_DB_PATH = os.getenv("METRICS_DB_PATH", "/data/metrics.db")
PDF_DIR = os.getenv("PDF_DIR", "/data/pdfs")
VLLM_URL = os.getenv("VLLM_URL", "http://nginx:80")
VLLM_MODEL = os.getenv("VLLM_MODEL", "chandra")
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
_db: aiosqlite.Connection
_metrics_db: aiosqlite.Connection | None = None
_metrics_db_lock = asyncio.Lock()
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


async def db_find_done_by_hash(file_hash: str, client_id: str | None) -> dict | None:
    async with _db.execute(
        "SELECT job_id FROM jobs WHERE file_hash=? AND client_id IS ? AND status='done' "
        "ORDER BY completed_at DESC LIMIT 1",
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


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sem, _db
    _sem = asyncio.Semaphore(CONCURRENCY)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(PDF_DIR, exist_ok=True)
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await db_init()
    await _resume_processing_jobs()
    yield
    await _db.close()
    if _metrics_db is not None:
        await _metrics_db.close()


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
_compose_cache: dict = {"at": 0.0, "images": {}}


def _read_compose_images() -> dict[str, str]:
    if time.time() - _compose_cache["at"] < _COMPOSE_TTL:
        return _compose_cache["images"]
    images: dict[str, str] = {}
    try:
        with open(COMPOSE_PATH) as f:
            data = yaml.safe_load(f) or {}
        for name, svc in (data.get("services") or {}).items():
            img = svc.get("image")
            if img:
                images[name] = img
    except Exception:
        pass
    _compose_cache.update(at=time.time(), images=images)
    return images


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


def _mode_from_probes(cache: dict) -> str:
    """Operational mode label inferred from which services are alive."""
    n = cache["ocr_alive"]
    llm_ok = bool(cache["llm"] and cache["llm"]["status"] == "ok")
    if n == 0:
        return "llm" if llm_ok else "down"
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
            "mode": _mode_from_probes(cache),
            "probe_age_s": round(time.time() - cache["at"], 1),
            "uptime_s": int(time.time() - _start_time),
            "images": _read_compose_images(),
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
        "mode": _mode_from_probes(cache),
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
    x_client_id: str | None = Header(None),
):
    if _mode_switching:
        raise HTTPException(status_code=503, detail="mode switch in progress, please retry shortly")
    if client_id is None:
        client_id = x_client_id
    pdf_bytes = await file.read()
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        total_pages = len(doc)
        doc.close()
    except Exception:
        total_pages = 0

    pdf_path = os.path.join(PDF_DIR, f"{file_hash}.pdf")
    if not os.path.exists(pdf_path):
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

    existing = (
        await db_find_done_by_hash(file_hash, client_id) or
        await db_find_done_by_filename(file.filename, total_pages, file_hash, client_id)
    )
    if existing:
        return {"job_id": existing["job_id"], "cached": True}

    job_id = str(uuid.uuid4())
    now = time.time()
    _jobs[job_id] = {
        "job_id": job_id,
        "filename": file.filename,
        "file_hash": file_hash,
        "client_id": client_id,
        "status": "queued",
        "submitted_at": now,
        "total_pages": 0,
        "done_pages": 0,
        "failed_pages": 0,
        "pages": [],
    }
    await db_create_job(job_id, file.filename, file_hash, client_id, now)
    background_tasks.add_task(_run, job_id, pdf_bytes)
    return {"job_id": job_id, "cached": False}


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


# ── Background processing ─────────────────────────────────────────────────────

async def _run(job_id: str, pdf_bytes: bytes, skip_pages: set[int] | None = None) -> None:
    """Process all pages of a PDF. If skip_pages is given, those page indices
    are not re-rendered or re-submitted (used by lifespan resume)."""
    skip_pages = skip_pages or set()
    job = _jobs[job_id]
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        msg = f"failed to open PDF: {e}"
        job.update(status="failed", error=msg)
        await db_update_job(job_id, status="failed", error=msg, completed_at=time.time())
        return

    n = len(doc)
    if not skip_pages:
        # fresh job: initialize page array & total_pages in DB
        job.update(total_pages=n, status="processing", pages=[None] * n)
        await db_update_job(job_id, status="processing", total_pages=n)

    todo: list[tuple[int, str]] = []
    for i in range(n):
        if i in skip_pages:
            continue
        page = doc[i]
        long_pt = max(page.rect.width, page.rect.height)
        dpi = min(DPI, MAX_PAGE_PX * 72 / long_pt) if long_pt > 0 else DPI
        zoom = dpi / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        todo.append((i, base64.b64encode(pix.tobytes("jpeg")).decode()))
    doc.close()

    async with httpx.AsyncClient(timeout=3000) as client:
        await asyncio.gather(*[_ocr_page(job, i, b64, client) for i, b64 in todo])

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


async def _ocr_page(job: dict, page_num: int, b64: str, client: httpx.AsyncClient) -> None:
    async with _sem:
        t0 = time.time()
        last_error = ""
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
