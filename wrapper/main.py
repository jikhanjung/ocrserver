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
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

DB_PATH = os.getenv("DB_PATH", "/data/ocrserver.db")
PDF_DIR = os.getenv("PDF_DIR", "/data/pdfs")
VLLM_URL = os.getenv("VLLM_URL", "http://nginx:80")
VLLM_MODEL = os.getenv("VLLM_MODEL", "chandra")
CONCURRENCY = int(os.getenv("OCR_CONCURRENCY", "12"))
DPI = int(os.getenv("OCR_DPI", "150"))
MAX_PAGE_PX = int(os.getenv("OCR_MAX_PAGE_PX", "2200"))  # cap longest side; chandra-ocr-2 max_model_len=12384
_RETRY_DELAYS = [5, 15, 30, 60]

_jobs: dict[str, dict] = {}
_sem: asyncio.Semaphore
_db: aiosqlite.Connection
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
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_file_hash ON jobs(file_hash)")
    await _db.commit()


async def db_find_done_by_hash(file_hash: str) -> dict | None:
    async with _db.execute(
        "SELECT job_id FROM jobs WHERE file_hash=? AND status='done' ORDER BY completed_at DESC LIMIT 1",
        (file_hash,),
    ) as c:
        row = await c.fetchone()
    return await db_get_job(row["job_id"]) if row else None


async def db_find_done_by_filename(filename: str, total_pages: int, file_hash: str) -> dict | None:
    async with _db.execute(
        "SELECT job_id FROM jobs WHERE filename=? AND total_pages=? AND file_hash IS NULL AND status='done' ORDER BY completed_at DESC LIMIT 1",
        (filename, total_pages),
    ) as c:
        row = await c.fetchone()
    if not row:
        return None
    await _db.execute("UPDATE jobs SET file_hash=? WHERE job_id=?", (file_hash, row["job_id"]))
    await _db.commit()
    return await db_get_job(row["job_id"])


async def db_create_job(job_id: str, filename: str, file_hash: str, submitted_at: float) -> None:
    await _db.execute(
        "INSERT INTO jobs (job_id, filename, file_hash, status, submitted_at) VALUES (?,?,?,'queued',?)",
        (job_id, filename, file_hash, submitted_at),
    )
    await _db.commit()


async def db_update_job(job_id: str, **kw) -> None:
    sets = ", ".join(f"{k}=?" for k in kw)
    await _db.execute(f"UPDATE jobs SET {sets} WHERE job_id=?", [*kw.values(), job_id])
    await _db.commit()


async def db_upsert_page(job_id: str, page_num: int, status: str,
                         duration_ms: int, markdown: str = None, error: str = None) -> None:
    await _db.execute(
        "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?)",
        (job_id, page_num, status, duration_ms, markdown, error),
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


async def db_list_jobs() -> list[dict]:
    async with _db.execute(
        "SELECT job_id,filename,status,submitted_at,completed_at,"
        "total_pages,done_pages,failed_pages,error "
        "FROM jobs ORDER BY submitted_at DESC"
    ) as c:
        rows = await c.fetchall()
    return [dict(r) for r in rows]


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
    yield
    await _db.close()


app = FastAPI(lifespan=lifespan)

_DASHBOARD = open(os.path.join(os.path.dirname(__file__), "dashboard.html")).read()
_STATUS_PAGE = open(os.path.join(os.path.dirname(__file__), "status.html")).read()

LLM_URL = os.getenv("LLM_URL", "")  # optional override; falls back to VLLM_URL/llm/


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _DASHBOARD


@app.get("/status", response_class=HTMLResponse)
async def status_page():
    return _STATUS_PAGE


@app.get("/api/services")
async def api_services():
    llm_base = LLM_URL.rstrip("/") if LLM_URL else f"{VLLM_URL.rstrip('/')}/llm"
    checks = {
        "chandra": f"{VLLM_URL.rstrip('/')}/health",
        "llm": f"{llm_base}/health",
    }
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=5) as client:
        for name, url in checks.items():
            try:
                r = await client.get(url)
                results[name] = {
                    "status": "ok" if r.status_code == 200 else "error",
                    "http_status": r.status_code,
                }
            except Exception as e:
                results[name] = {"status": "down", "error": str(e)}
    results["_meta"] = {
        "chandra_url": checks["chandra"],
        "llm_url": checks["llm"],
        "concurrency": CONCURRENCY,
        "uptime_s": int(time.time() - _start_time),
    }
    return results


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
    return {
        "counts": counts,
        "uptime_s": int(time.time() - _start_time),
        "concurrency": CONCURRENCY,
        "vllm_url": VLLM_URL,
    }


@app.post("/ocr")
async def submit(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
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
        await db_find_done_by_hash(file_hash) or
        await db_find_done_by_filename(file.filename, total_pages, file_hash)
    )
    if existing:
        return {"job_id": existing["job_id"], "cached": True}

    job_id = str(uuid.uuid4())
    now = time.time()
    _jobs[job_id] = {
        "job_id": job_id,
        "filename": file.filename,
        "file_hash": file_hash,
        "status": "queued",
        "submitted_at": now,
        "total_pages": 0,
        "done_pages": 0,
        "failed_pages": 0,
        "pages": [],
    }
    await db_create_job(job_id, file.filename, file_hash, now)
    background_tasks.add_task(_run, job_id, pdf_bytes)
    return {"job_id": job_id, "cached": False}


@app.get("/ocr")
async def list_jobs():
    db_jobs = await db_list_jobs()
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

async def _run(job_id: str, pdf_bytes: bytes) -> None:
    job = _jobs[job_id]
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        msg = f"failed to open PDF: {e}"
        job.update(status="failed", error=msg)
        await db_update_job(job_id, status="failed", error=msg, completed_at=time.time())
        return

    n = len(doc)
    job.update(total_pages=n, status="processing", pages=[None] * n)
    await db_update_job(job_id, status="processing", total_pages=n)

    pages_b64 = []
    for i in range(n):
        page = doc[i]
        long_pt = max(page.rect.width, page.rect.height)
        dpi = min(DPI, MAX_PAGE_PX * 72 / long_pt) if long_pt > 0 else DPI
        zoom = dpi / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        pages_b64.append(base64.b64encode(pix.tobytes("jpeg")).decode())
    doc.close()

    async with httpx.AsyncClient(timeout=3000) as client:
        await asyncio.gather(*[_ocr_page(job, i, b64, client) for i, b64 in enumerate(pages_b64)])

    completed_at = time.time()
    status = "done" if job["failed_pages"] == 0 else "done_with_errors"
    job["status"] = status
    await db_update_job(
        job_id, status=status,
        done_pages=job["done_pages"], failed_pages=job["failed_pages"],
        completed_at=completed_at,
    )


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
