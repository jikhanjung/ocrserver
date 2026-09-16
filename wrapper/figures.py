"""Figure-split job API (P02, wrapper 0.3.0).

Three job kinds — detect / link / panels — all executed by a host-side worker
that runs the Codex CLI (gpt-6-astra). The wrapper is the only DB writer:
it accepts jobs, hands items to the worker over /internal/figures/*, stores
results, and serves them back to clients. The wrapper never renders pages or
calls the model for these jobs.

Design references: devlog/20260916_P02_figure_split_service_design.md and
PaperMeister P16 §6 / docs/figure_pipeline_client_plan.md. Key contracts:
  * prompt (instructions + JSON schema) travels *in the request* (D5); the
    server is domain-agnostic and stores whatever validated JSON comes back.
  * page numbers are 0-based everywhere.
  * text for the per-paper workspace is uploaded by the client, keyed by
    file_hash|ocr_digest — the wrapper's own OCR rows may be missing or stale
    for that hash (RunPod-era papers, 2026-09-08 fragment jobs).
  * one call per FIGURES_MIN_INTERVAL seconds (D8) is enforced by the worker;
    the value is only advertised here.
"""
import asyncio
import hashlib
import json
import os
import time
import uuid

import aiosqlite
from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

router = APIRouter()

KINDS = ("detect", "link", "panels")
ITEM_TERMINAL = ("done", "failed", "budget_exhausted")

WORKER_TOKEN = os.getenv("FIGURES_WORKER_TOKEN", "")
MIN_INTERVAL = int(os.getenv("FIGURES_MIN_INTERVAL", "300"))
MAX_ATTEMPTS = int(os.getenv("FIGURES_MAX_ATTEMPTS", "3"))
# A claimed item whose worker stopped heart-beating for this long goes back to
# the queue (worker crashed mid-session). Sessions can legitimately run 10-20
# min, so this must be longer than the worker's session cap.
HEARTBEAT_TIMEOUT = int(os.getenv("FIGURES_HEARTBEAT_TIMEOUT", "1800"))
RESULT_TTL_DAYS = int(os.getenv("FIGURES_RESULT_TTL_DAYS", "30"))
WORKSPACE_TTL_DAYS = int(os.getenv("FIGURES_WORKSPACE_TTL_DAYS", "7"))
MAX_ITEMS_PER_JOB = int(os.getenv("FIGURES_MAX_ITEMS_PER_JOB", "500"))

_db_getter = None
_pdf_dir = "/data/pdfs"


def configure(db_getter, pdf_dir: str) -> None:
    global _db_getter, _pdf_dir
    _db_getter = db_getter
    _pdf_dir = pdf_dir


def _db() -> aiosqlite.Connection:
    return _db_getter()


def _pdf_path(file_hash: str) -> str:
    return os.path.join(_pdf_dir, f"{file_hash}.pdf")


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _is_hash(s) -> bool:
    return isinstance(s, str) and len(s) == 64 and all(c in "0123456789abcdef" for c in s)


# ── schema ────────────────────────────────────────────────────────────────────

async def db_init(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS figure_workspaces (
            file_hash    TEXT NOT NULL,
            ocr_digest   TEXT NOT NULL,
            client_id    TEXT,
            page_count   INTEGER,
            pages_json   TEXT,
            created_at   REAL,
            PRIMARY KEY (file_hash, ocr_digest)
        );
        CREATE TABLE IF NOT EXISTS figure_jobs (
            job_id         TEXT PRIMARY KEY,
            kind           TEXT NOT NULL,
            client_id      TEXT,
            file_hash      TEXT,
            ocr_digest     TEXT,
            prompt_version TEXT,
            prompt_json    TEXT,
            options_json   TEXT,
            status         TEXT DEFAULT 'queued',
            submitted_at   REAL,
            completed_at   REAL,
            total          INTEGER DEFAULT 0,
            done           INTEGER DEFAULT 0,
            failed         INTEGER DEFAULT 0,
            cached         INTEGER DEFAULT 0,
            error          TEXT
        );
        CREATE TABLE IF NOT EXISTS figure_items (
            item_id      TEXT PRIMARY KEY,
            job_id       TEXT NOT NULL,
            key          TEXT NOT NULL,
            kind         TEXT NOT NULL,
            client_id    TEXT,
            file_hash    TEXT,
            ocr_digest   TEXT,
            dedup_key    TEXT,
            status       TEXT DEFAULT 'queued',
            attempts     INTEGER DEFAULT 0,
            request_json TEXT,
            result_json  TEXT,
            error        TEXT,
            claimed_by   TEXT,
            claimed_at   REAL,
            heartbeat_at REAL,
            completed_at REAL,
            elapsed_s    REAL,
            usage_json   TEXT,
            model        TEXT,
            FOREIGN KEY (job_id) REFERENCES figure_jobs(job_id)
        );
        CREATE TABLE IF NOT EXISTS figure_calls (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            at             REAL,
            kind           TEXT,
            client_id      TEXT,
            item_id        TEXT,
            model          TEXT,
            prompt_version TEXT,
            status         TEXT,
            elapsed_s      REAL,
            usage_json     TEXT,
            error          TEXT
        );
        CREATE TABLE IF NOT EXISTS figure_worker (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            worker_id     TEXT,
            version       TEXT,
            state         TEXT,
            paused_reason TEXT,
            paused_at     REAL,
            next_call_at  REAL,
            last_seen     REAL
        );
        CREATE INDEX IF NOT EXISTS idx_fitems_job ON figure_items(job_id);
        CREATE INDEX IF NOT EXISTS idx_fitems_status ON figure_items(status);
        CREATE INDEX IF NOT EXISTS idx_fitems_dedup ON figure_items(dedup_key, client_id, status);
        CREATE INDEX IF NOT EXISTS idx_fjobs_client ON figure_jobs(client_id, submitted_at);
        CREATE INDEX IF NOT EXISTS idx_fcalls_at ON figure_calls(at);
    """)
    await db.execute(
        "INSERT OR IGNORE INTO figure_worker (id, state) VALUES (1, 'unknown')")
    await db.commit()


async def cleanup(db: aiosqlite.Connection) -> dict:
    """Drop old finished jobs and unreferenced workspaces. Results are a cache;
    the client DB is the source of truth (P16 §6.5)."""
    now = time.time()
    cutoff_jobs = now - RESULT_TTL_DAYS * 86400
    async with db.execute(
        "SELECT job_id FROM figure_jobs WHERE completed_at IS NOT NULL AND completed_at < ?",
        (cutoff_jobs,),
    ) as c:
        old = [r[0] for r in await c.fetchall()]
    for jid in old:
        await db.execute("DELETE FROM figure_items WHERE job_id=?", (jid,))
        await db.execute("DELETE FROM figure_jobs WHERE job_id=?", (jid,))
    cutoff_ws = now - WORKSPACE_TTL_DAYS * 86400
    cur = await db.execute(
        "DELETE FROM figure_workspaces WHERE created_at < ? AND NOT EXISTS ("
        "  SELECT 1 FROM figure_items i WHERE i.file_hash=figure_workspaces.file_hash "
        "  AND i.ocr_digest=figure_workspaces.ocr_digest AND i.status IN ('queued','processing'))",
        (cutoff_ws,),
    )
    await db.commit()
    return {"jobs": len(old), "workspaces": cur.rowcount}


# ── helpers ───────────────────────────────────────────────────────────────────

async def _worker_row() -> dict:
    async with _db().execute("SELECT * FROM figure_worker WHERE id=1") as c:
        row = await c.fetchone()
    return dict(row) if row else {"state": "unknown"}


def _worker_public(row: dict) -> dict:
    last = row.get("last_seen")
    return {
        "state": row.get("state") or "unknown",
        "worker_id": row.get("worker_id"),
        "version": row.get("version"),
        "paused_reason": row.get("paused_reason"),
        "paused_at": row.get("paused_at"),
        "next_call_at": row.get("next_call_at"),
        "last_seen": last,
        "alive": bool(last and time.time() - last < 180),
        "min_interval_s": MIN_INTERVAL,
    }


async def _workspace_exists(file_hash: str, ocr_digest: str) -> bool:
    async with _db().execute(
        "SELECT 1 FROM figure_workspaces WHERE file_hash=? AND ocr_digest=?",
        (file_hash, ocr_digest),
    ) as c:
        return await c.fetchone() is not None


def _bbox_ok(b) -> bool:
    return (isinstance(b, list) and len(b) == 4
            and all(isinstance(v, int) and 0 <= v <= 1000 for v in b)
            and b[0] < b[2] and b[1] < b[3])


def _validate_item(kind: str, item) -> str | None:
    """Structural checks only — the domain lives in the client's prompt."""
    if not isinstance(item, dict):
        return "item must be an object"
    if not isinstance(item.get("key"), str) or not item["key"]:
        return "item.key must be a non-empty string"
    if kind == "detect":
        # One item per PAGE (PaperMeister devlog 099 §4): several hint boxes
        # (the rule's figures on that page), or none for a page-level doubt.
        if not isinstance(item.get("page"), int) or item["page"] < 0:
            return "detect item needs page (0-based int)"
        hbs = item.get("hint_boxes")
        if hbs is None and item.get("hint_bbox_page_1000") is not None:
            hbs = [item["hint_bbox_page_1000"]]  # legacy single-box form
        if hbs is not None:
            if not isinstance(hbs, list) or not all(_bbox_ok(b) for b in hbs):
                return "hint_boxes must be a list of [x0,y0,x1,y1] ints 0..1000"
            if len(hbs) > 200:
                return "at most 200 hint boxes per item"
        fks = item.get("figure_keys")
        if fks is not None and (not isinstance(fks, list) or not all(isinstance(k, str) for k in fks)):
            return "figure_keys must be a list of strings"
    elif kind == "link":
        figs = item.get("figures")
        if not isinstance(figs, list) or not figs:
            return "link item needs a non-empty figures list"
        for f in figs:
            if not isinstance(f, dict) or not isinstance(f.get("figure_id"), str):
                return "each figure needs figure_id"
            if not isinstance(f.get("page"), int) or f["page"] < 0:
                return f"figure {f.get('figure_id')}: page must be a 0-based int"
            if not _bbox_ok(f.get("bbox_page_1000")):
                return f"figure {f.get('figure_id')}: bbox_page_1000 invalid"
    elif kind == "panels":
        if not isinstance(item.get("page"), int) or item["page"] < 0:
            return "panels item needs page (0-based int)"
        if not _bbox_ok(item.get("bbox_page_1000")):
            return "panels item needs bbox_page_1000 [x0,y0,x1,y1] ints 0..1000"
        if not isinstance(item.get("caption", ""), str):
            return "caption must be a string"
        if not isinstance(item.get("entries", []), list):
            return "entries must be a list"
    return None


def _validate_prompt(p) -> str | None:
    if not isinstance(p, dict):
        return "prompt must be an object {version, instructions, schema}"
    if not isinstance(p.get("version"), str) or not p["version"]:
        return "prompt.version must be a non-empty string"
    if not isinstance(p.get("instructions"), str) or not p["instructions"].strip():
        return "prompt.instructions must be a non-empty string"
    if not isinstance(p.get("schema"), dict):
        return "prompt.schema must be a JSON schema object"
    return None


async def _job_counts(job_id: str) -> dict:
    async with _db().execute(
        "SELECT status, COUNT(*) FROM figure_items WHERE job_id=? GROUP BY status", (job_id,)
    ) as c:
        rows = await c.fetchall()
    counts = {r[0]: r[1] for r in rows}
    return counts


async def _refresh_job_status(job_id: str) -> None:
    counts = await _job_counts(job_id)
    total = sum(counts.values())
    done = counts.get("done", 0)
    failed = counts.get("failed", 0) + counts.get("budget_exhausted", 0)
    pending = counts.get("queued", 0) + counts.get("processing", 0)
    if pending:
        status = "processing" if (done or failed or counts.get("processing")) else "queued"
        completed_at = None
    elif failed and not done:
        status, completed_at = "failed", time.time()
    elif failed:
        status, completed_at = "done_with_errors", time.time()
    else:
        status, completed_at = "done", time.time()
    await _db().execute(
        "UPDATE figure_jobs SET status=?, done=?, failed=?, total=?, "
        "completed_at=COALESCE(completed_at, ?) WHERE job_id=?",
        (status, done, failed, total, completed_at, job_id),
    )
    if pending:
        await _db().execute("UPDATE figure_jobs SET completed_at=NULL WHERE job_id=?", (job_id,))


def _item_public(row: dict, with_result: bool = True) -> dict:
    out = {
        "key": row["key"],
        "status": row["status"],
        "attempts": row["attempts"],
        "error": row.get("error"),
        "elapsed_s": row.get("elapsed_s"),
        "model": row.get("model"),
        "completed_at": row.get("completed_at"),
    }
    if with_result:
        out["result"] = json.loads(row["result_json"]) if row.get("result_json") else None
        out["usage"] = json.loads(row["usage_json"]) if row.get("usage_json") else None
    return out


async def _job_public(job_id: str, with_items: bool = True) -> dict | None:
    async with _db().execute("SELECT * FROM figure_jobs WHERE job_id=?", (job_id,)) as c:
        row = await c.fetchone()
    if not row:
        return None
    job = dict(row)
    out = {k: job[k] for k in ("job_id", "kind", "client_id", "file_hash", "ocr_digest",
                               "prompt_version", "status", "submitted_at", "completed_at",
                               "total", "done", "failed", "cached", "error")}
    out["options"] = json.loads(job["options_json"]) if job.get("options_json") else {}
    if with_items:
        async with _db().execute(
            "SELECT * FROM figure_items WHERE job_id=? ORDER BY rowid", (job_id,)
        ) as c:
            items = [dict(r) for r in await c.fetchall()]
        out["items"] = [_item_public(i) for i in items]
    out["worker"] = _worker_public(await _worker_row())
    return out


# ── PDF store ─────────────────────────────────────────────────────────────────

@router.api_route("/pdfs/{file_hash}", methods=["GET", "HEAD"])
async def pdf_head(file_hash: str, request: Request):
    if not _is_hash(file_hash):
        raise HTTPException(status_code=400, detail="file_hash must be a sha256 hex")
    path = _pdf_path(file_hash)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="pdf not stored")
    if request.method == "HEAD":
        return Response(status_code=200, headers={"Content-Length": str(os.path.getsize(path))})
    return {"file_hash": file_hash, "size": os.path.getsize(path),
            "stored_at": os.path.getmtime(path)}


@router.post("/pdfs", status_code=201)
async def pdf_upload(
    file: UploadFile = File(...),
    client_id: str | None = Form(None),
    x_client_id: str | None = Header(None),
):
    """Store a PDF under its sha256 without running OCR. For papers whose OCR
    came from elsewhere (RunPod era) so figure jobs can render from it."""
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="not a PDF")
    file_hash = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())
    path = _pdf_path(file_hash)
    existed = os.path.exists(path)
    if not existed:
        def _write():
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        await asyncio.to_thread(_write)
    return {"file_hash": file_hash, "existed": existed, "size": len(data),
            "client_id": client_id or x_client_id}


# ── workspace (client-uploaded page text) ─────────────────────────────────────

@router.post("/figures/workspace", status_code=201)
async def workspace_upload(payload: dict, x_client_id: str | None = Header(None)):
    file_hash = payload.get("file_hash")
    ocr_digest = payload.get("ocr_digest")
    pages = payload.get("pages")
    client_id = payload.get("client_id") or x_client_id
    if not _is_hash(file_hash):
        raise HTTPException(status_code=400, detail="file_hash must be a sha256 hex")
    if not isinstance(ocr_digest, str) or not (8 <= len(ocr_digest) <= 64):
        raise HTTPException(status_code=400, detail="ocr_digest must be an 8..64 char string")
    if not isinstance(pages, list) or not pages:
        raise HTTPException(status_code=400, detail="pages must be a non-empty list of {page, markdown}")
    seen = set()
    for p in pages:
        if (not isinstance(p, dict) or not isinstance(p.get("page"), int) or p["page"] < 0
                or not isinstance(p.get("markdown"), str)):
            raise HTTPException(status_code=400, detail="each page needs page (0-based int) and markdown (str)")
        if p["page"] in seen:
            raise HTTPException(status_code=400, detail=f"duplicate page {p['page']}")
        seen.add(p["page"])
    if not os.path.exists(_pdf_path(file_hash)):
        raise HTTPException(status_code=404, detail="pdf_missing: upload it via POST /pdfs first")
    existed = await _workspace_exists(file_hash, ocr_digest)
    slim = [{"page": p["page"], "markdown": p["markdown"]} for p in pages]
    await _db().execute(
        "INSERT OR REPLACE INTO figure_workspaces "
        "(file_hash, ocr_digest, client_id, page_count, pages_json, created_at) VALUES (?,?,?,?,?,?)",
        (file_hash, ocr_digest, client_id, len(slim), json.dumps(slim, ensure_ascii=False), time.time()),
    )
    await _db().commit()
    return {"file_hash": file_hash, "ocr_digest": ocr_digest, "pages": len(slim), "existed": existed}


@router.api_route("/figures/workspace/{file_hash}/{ocr_digest}", methods=["GET", "HEAD"])
async def workspace_head(file_hash: str, ocr_digest: str, request: Request):
    async with _db().execute(
        "SELECT page_count, created_at, client_id FROM figure_workspaces WHERE file_hash=? AND ocr_digest=?",
        (file_hash, ocr_digest),
    ) as c:
        row = await c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="workspace_missing")
    if request.method == "HEAD":
        return Response(status_code=200)
    return {"file_hash": file_hash, "ocr_digest": ocr_digest, "pages": row[0],
            "created_at": row[1], "client_id": row[2]}


# ── jobs ──────────────────────────────────────────────────────────────────────

@router.post("/figures/{kind}", status_code=202)
async def submit_job(kind: str, payload: dict, x_client_id: str | None = Header(None)):
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail=f"unknown kind {kind!r}; one of {KINDS}")
    client_id = payload.get("client_id") or x_client_id
    file_hash = payload.get("file_hash")
    ocr_digest = payload.get("ocr_digest")
    items = payload.get("items")
    prompt = payload.get("prompt")
    options = payload.get("options") or {}
    force = bool(payload.get("force"))

    if not _is_hash(file_hash):
        raise HTTPException(status_code=400, detail="file_hash must be a sha256 hex")
    if not os.path.exists(_pdf_path(file_hash)):
        raise HTTPException(status_code=404, detail="pdf_missing: upload it via POST /pdfs first")
    if kind in ("detect", "link"):
        if not isinstance(ocr_digest, str) or not ocr_digest:
            raise HTTPException(status_code=400, detail=f"{kind} needs ocr_digest")
        if not await _workspace_exists(file_hash, ocr_digest):
            raise HTTPException(status_code=404, detail="workspace_missing: POST /figures/workspace first")
    else:
        ocr_digest = ocr_digest or ""
    err = _validate_prompt(prompt)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="items must be a non-empty list")
    if len(items) > MAX_ITEMS_PER_JOB:
        raise HTTPException(status_code=400, detail=f"at most {MAX_ITEMS_PER_JOB} items per job")
    if not isinstance(options, dict):
        raise HTTPException(status_code=400, detail="options must be an object")
    keys = set()
    for it in items:
        err = _validate_item(kind, it)
        if err:
            raise HTTPException(status_code=400, detail=err)
        if it["key"] in keys:
            raise HTTPException(status_code=400, detail=f"duplicate item key {it['key']!r}")
        keys.add(it["key"])

    prompt_digest = _sha(_canon(prompt))
    options_digest = _sha(_canon(options))
    job_id = str(uuid.uuid4())
    now = time.time()
    db = _db()
    await db.execute(
        "INSERT INTO figure_jobs (job_id, kind, client_id, file_hash, ocr_digest, prompt_version, "
        "prompt_json, options_json, status, submitted_at, total) VALUES (?,?,?,?,?,?,?,?,'queued',?,?)",
        (job_id, kind, client_id, file_hash, ocr_digest, prompt["version"],
         _canon(prompt), _canon(options), now, len(items)),
    )
    cached = 0
    for it in items:
        body = {k: v for k, v in it.items() if k != "key"}
        dedup_key = _sha(f"{kind}|{file_hash}|{ocr_digest}|{_canon(body)}|{prompt_digest}|{options_digest}")[:32]
        prior = None
        if not force:
            async with db.execute(
                "SELECT result_json, elapsed_s, usage_json, model FROM figure_items "
                "WHERE dedup_key=? AND client_id IS ? AND status='done' ORDER BY completed_at DESC LIMIT 1",
                (dedup_key, client_id),
            ) as c:
                prior = await c.fetchone()
        item_id = str(uuid.uuid4())
        if prior:
            cached += 1
            await db.execute(
                "INSERT INTO figure_items (item_id, job_id, key, kind, client_id, file_hash, ocr_digest, "
                "dedup_key, status, attempts, request_json, result_json, elapsed_s, usage_json, model, completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,'done',0,?,?,?,?,?,?)",
                (item_id, job_id, it["key"], kind, client_id, file_hash, ocr_digest, dedup_key,
                 _canon(body), prior[0], prior[1], prior[2], prior[3], now),
            )
        else:
            await db.execute(
                "INSERT INTO figure_items (item_id, job_id, key, kind, client_id, file_hash, ocr_digest, "
                "dedup_key, status, request_json) VALUES (?,?,?,?,?,?,?,?,'queued',?)",
                (item_id, job_id, it["key"], kind, client_id, file_hash, ocr_digest, dedup_key, _canon(body)),
            )
    await db.execute("UPDATE figure_jobs SET cached=? WHERE job_id=?", (cached, job_id))
    await _refresh_job_status(job_id)
    await db.commit()
    return {"job_id": job_id, "kind": kind, "total": len(items), "cached": cached,
            "queued": len(items) - cached}


@router.get("/figures/jobs")
async def list_jobs(
    client_id: str | None = Query(None),
    kind: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
    where, params = [], []
    if client_id is not None:
        where.append("client_id=?"); params.append(client_id)
    if kind is not None:
        where.append("kind=?"); params.append(kind)
    if status is not None:
        where.append("status=?"); params.append(status)
    sql = ("SELECT job_id, kind, client_id, file_hash, ocr_digest, prompt_version, status, "
           "submitted_at, completed_at, total, done, failed, cached, error FROM figure_jobs")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY submitted_at DESC LIMIT ?"
    params.append(limit)
    async with _db().execute(sql, params) as c:
        rows = [dict(r) for r in await c.fetchall()]
    return {"items": rows, "worker": _worker_public(await _worker_row())}


@router.get("/figures/{kind}/{job_id}")
async def get_job(kind: str, job_id: str):
    job = await _job_public(job_id)
    if not job or job["kind"] != kind:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.post("/figures/{kind}/{job_id}/resume")
async def resume_job(kind: str, job_id: str, retry_errors: bool = Query(False)):
    """Re-queue this job's failed / budget-exhausted items. Without
    retry_errors only items with attempts left are re-queued; with it the
    attempt counter is reset (operator's explicit decision — fsis rule)."""
    job = await _job_public(job_id, with_items=False)
    if not job or job["kind"] != kind:
        raise HTTPException(status_code=404, detail="job not found")
    db = _db()
    if retry_errors:
        cur = await db.execute(
            "UPDATE figure_items SET status='queued', attempts=0, error=NULL, claimed_by=NULL "
            "WHERE job_id=? AND status IN ('failed','budget_exhausted')", (job_id,))
    else:
        cur = await db.execute(
            "UPDATE figure_items SET status='queued', error=NULL, claimed_by=NULL "
            "WHERE job_id=? AND status IN ('failed','budget_exhausted') AND attempts < ?",
            (job_id, MAX_ATTEMPTS))
    await _refresh_job_status(job_id)
    await db.commit()
    return {"job_id": job_id, "requeued": cur.rowcount}


@router.post("/figures/worker/resume")
async def worker_resume():
    """Clear a fatal pause (login expired, usage limit). The operator fixes
    the cause on the host first (e.g. `codex login`), then calls this."""
    db = _db()
    await db.execute(
        "UPDATE figure_worker SET state='idle', paused_reason=NULL, paused_at=NULL WHERE id=1")
    await db.commit()
    return _worker_public(await _worker_row())


# ── dashboard summary ─────────────────────────────────────────────────────────

@router.get("/api/figures")
async def api_figures():
    db = _db()
    async with db.execute(
        "SELECT kind, status, COUNT(*) FROM figure_items GROUP BY kind, status") as c:
        rows = await c.fetchall()
    items: dict = {k: {} for k in KINDS}
    for kind, status, n in rows:
        items.setdefault(kind, {})[status] = n
    async with db.execute(
        "SELECT status, COUNT(*) FROM figure_jobs GROUP BY status") as c:
        jobs = {r[0]: r[1] for r in await c.fetchall()}
    day_ago = time.time() - 86400
    async with db.execute(
        "SELECT COUNT(*), SUM(status='done'), AVG(elapsed_s) FROM figure_calls WHERE at >= ?",
        (day_ago,)) as c:
        n, ok, avg = await c.fetchone()
    async with db.execute(
        "SELECT at, kind, status, error FROM figure_calls WHERE status!='done' "
        "ORDER BY at DESC LIMIT 1") as c:
        last_err = await c.fetchone()
    async with db.execute(
        "SELECT COUNT(*) FROM figure_workspaces") as c:
        ws = (await c.fetchone())[0]
    return {
        "available": True,
        "worker": _worker_public(await _worker_row()),
        "worker_api_enabled": bool(WORKER_TOKEN),
        "jobs": jobs,
        "items": items,
        "workspaces": ws,
        "calls_24h": {"total": n or 0, "ok": ok or 0,
                      "avg_elapsed_s": round(avg, 1) if avg else None},
        "last_error": ({"at": last_err[0], "kind": last_err[1], "status": last_err[2],
                        "error": last_err[3]} if last_err else None),
        "limits": {"min_interval_s": MIN_INTERVAL, "max_attempts": MAX_ATTEMPTS,
                   "heartbeat_timeout_s": HEARTBEAT_TIMEOUT},
    }


# ── internal API (host worker) ────────────────────────────────────────────────

def _auth(token: str | None) -> None:
    if not WORKER_TOKEN:
        raise HTTPException(status_code=503, detail="worker API disabled (FIGURES_WORKER_TOKEN unset)")
    if token != WORKER_TOKEN:
        raise HTTPException(status_code=403, detail="bad worker token")


async def _requeue_stale() -> int:
    cur = await _db().execute(
        "UPDATE figure_items SET status='queued', claimed_by=NULL "
        "WHERE status='processing' AND heartbeat_at < ?",
        (time.time() - HEARTBEAT_TIMEOUT,))
    return cur.rowcount


@router.post("/internal/figures/worker/status")
async def worker_status(payload: dict, x_worker_token: str | None = Header(None)):
    _auth(x_worker_token)
    state = payload.get("state")
    if state not in ("idle", "running", "sleeping", "paused"):
        raise HTTPException(status_code=400, detail="state must be idle|running|sleeping|paused")
    db = _db()
    now = time.time()
    if state == "paused":
        await db.execute(
            "UPDATE figure_worker SET worker_id=?, version=?, state='paused', paused_reason=?, "
            "paused_at=COALESCE(paused_at, ?), next_call_at=?, last_seen=? WHERE id=1",
            (payload.get("worker_id"), payload.get("version"), payload.get("paused_reason"),
             now, payload.get("next_call_at"), now))
    else:
        await db.execute(
            "UPDATE figure_worker SET worker_id=?, version=?, state=?, paused_reason=NULL, "
            "paused_at=NULL, next_call_at=?, last_seen=? WHERE id=1",
            (payload.get("worker_id"), payload.get("version"), state,
             payload.get("next_call_at"), now))
    await db.commit()
    return _worker_public(await _worker_row())


@router.post("/internal/figures/claim")
async def claim(payload: dict, x_worker_token: str | None = Header(None)):
    """Hand the worker one queued item. Fair across clients: the client whose
    last completed item is oldest goes first (round robin by completion),
    then the oldest job within that client."""
    _auth(x_worker_token)
    db = _db()
    worker_id = payload.get("worker_id") or "worker"
    now = time.time()
    await _requeue_stale()
    row = await _worker_row()
    await db.execute("UPDATE figure_worker SET worker_id=?, last_seen=? WHERE id=1", (worker_id, now))
    if row.get("state") == "paused":
        await db.commit()
        return {"item": None, "worker": _worker_public(await _worker_row()),
                "min_interval_s": MIN_INTERVAL}
    async with db.execute(
        "SELECT i.client_id, MIN(j.submitted_at) AS first_sub, "
        "  (SELECT MAX(completed_at) FROM figure_items d WHERE d.client_id IS i.client_id "
        "   AND d.completed_at IS NOT NULL) AS last_done "
        "FROM figure_items i JOIN figure_jobs j ON j.job_id=i.job_id "
        "WHERE i.status='queued' GROUP BY i.client_id "
        "ORDER BY COALESCE(last_done, 0) ASC, first_sub ASC LIMIT 1"
    ) as c:
        pick = await c.fetchone()
    if not pick:
        await db.commit()
        return {"item": None, "worker": _worker_public(await _worker_row()),
                "min_interval_s": MIN_INTERVAL}
    client_id = pick[0]
    async with db.execute(
        "SELECT i.*, j.kind AS job_kind, j.prompt_json, j.options_json, j.prompt_version "
        "FROM figure_items i JOIN figure_jobs j ON j.job_id=i.job_id "
        "WHERE i.status='queued' AND i.client_id IS ? "
        "ORDER BY j.submitted_at ASC, i.rowid ASC LIMIT 1",
        (client_id,),
    ) as c:
        item = await c.fetchone()
    item = dict(item)
    await db.execute(
        "UPDATE figure_items SET status='processing', attempts=attempts+1, claimed_by=?, "
        "claimed_at=?, heartbeat_at=? WHERE item_id=?",
        (worker_id, now, now, item["item_id"]))
    await _refresh_job_status(item["job_id"])
    await db.commit()
    return {
        "item": {
            "item_id": item["item_id"],
            "job_id": item["job_id"],
            "key": item["key"],
            "kind": item["kind"],
            "client_id": item["client_id"],
            "file_hash": item["file_hash"],
            "ocr_digest": item["ocr_digest"],
            "attempt": item["attempts"] + 1,
            "max_attempts": MAX_ATTEMPTS,
            "request": json.loads(item["request_json"]),
            "prompt": json.loads(item["prompt_json"]),
            "options": json.loads(item["options_json"] or "{}"),
            # no pdf_path: it would be the container's path — the worker resolves
            # PDF_DIR/{file_hash}.pdf on its own side (2026-09-16 e2e lesson).
        },
        "worker": _worker_public(await _worker_row()),
        "min_interval_s": MIN_INTERVAL,
    }


@router.get("/internal/figures/workspace/{file_hash}/{ocr_digest}")
async def internal_workspace(file_hash: str, ocr_digest: str,
                             x_worker_token: str | None = Header(None)):
    _auth(x_worker_token)
    async with _db().execute(
        "SELECT page_count, pages_json FROM figure_workspaces WHERE file_hash=? AND ocr_digest=?",
        (file_hash, ocr_digest),
    ) as c:
        row = await c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="workspace_missing")
    return {"file_hash": file_hash, "ocr_digest": ocr_digest, "page_count": row[0],
            "pages": json.loads(row[1])}


@router.post("/internal/figures/items/{item_id}/heartbeat")
async def item_heartbeat(item_id: str, x_worker_token: str | None = Header(None)):
    _auth(x_worker_token)
    db = _db()
    now = time.time()
    cur = await db.execute(
        "UPDATE figure_items SET heartbeat_at=? WHERE item_id=? AND status='processing'",
        (now, item_id))
    await db.execute("UPDATE figure_worker SET last_seen=?, state='running' WHERE id=1", (now,))
    await db.commit()
    if not cur.rowcount:
        raise HTTPException(status_code=409, detail="item is not processing (requeued or finished)")
    return {"ok": True}


@router.post("/internal/figures/items/{item_id}/release")
async def item_release(item_id: str, payload: dict | None = None,
                       x_worker_token: str | None = Header(None)):
    """Worker is shutting down mid-session (systemctl stop/restart): put the
    item straight back in the queue without spending an attempt or waiting
    for the heartbeat timeout."""
    _auth(x_worker_token)
    db = _db()
    cur = await db.execute(
        "UPDATE figure_items SET status='queued', attempts=MAX(0, attempts-1), claimed_by=NULL, "
        "error=? WHERE item_id=? AND status='processing'",
        (((payload or {}).get("reason") or "released by worker"), item_id))
    if not cur.rowcount:
        await db.commit()
        raise HTTPException(status_code=409, detail="item is not processing")
    async with db.execute("SELECT job_id FROM figure_items WHERE item_id=?", (item_id,)) as c:
        job_id = (await c.fetchone())[0]
    await _refresh_job_status(job_id)
    await db.execute("UPDATE figure_worker SET state='idle', last_seen=? WHERE id=1", (time.time(),))
    await db.commit()
    return {"item_id": item_id, "status": "queued"}


@router.post("/internal/figures/items/{item_id}/result")
async def item_result(item_id: str, payload: dict, x_worker_token: str | None = Header(None)):
    """Worker reports one finished attempt.

    status: done | failed | budget_exhausted | fatal
      done             result (object) required — stored verbatim
      failed           this attempt failed; re-queued while attempts < max
      budget_exhausted session hit the time/turn cap — terminal, distinct from failed
      fatal            login expired / CLI missing / usage limit: NOT an attempt.
                       Item goes back to the queue, worker is marked paused.
    """
    _auth(x_worker_token)
    status = payload.get("status")
    if status not in ("done", "failed", "budget_exhausted", "fatal"):
        raise HTTPException(status_code=400, detail="status must be done|failed|budget_exhausted|fatal")
    db = _db()
    async with db.execute("SELECT * FROM figure_items WHERE item_id=?", (item_id,)) as c:
        row = await c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="item not found")
    item = dict(row)
    if item["status"] != "processing":
        raise HTTPException(status_code=409, detail=f"item is {item['status']}, not processing")
    async with db.execute("SELECT prompt_version FROM figure_jobs WHERE job_id=?", (item["job_id"],)) as c:
        pv = (await c.fetchone())[0]

    now = time.time()
    error = payload.get("error")
    result = payload.get("result")
    usage = payload.get("usage")
    model = payload.get("model")
    elapsed = payload.get("elapsed_s")
    if status == "done" and not isinstance(result, dict):
        raise HTTPException(status_code=400, detail="done needs a result object")

    if status == "fatal":
        await db.execute(
            "UPDATE figure_items SET status='queued', attempts=MAX(0, attempts-1), claimed_by=NULL, "
            "error=? WHERE item_id=?", (error, item_id))
        await db.execute(
            "UPDATE figure_worker SET state='paused', paused_reason=?, paused_at=?, last_seen=? WHERE id=1",
            (error or "fatal", now, now))
        call_status = "fatal"
    elif status == "done":
        await db.execute(
            "UPDATE figure_items SET status='done', result_json=?, error=NULL, elapsed_s=?, "
            "usage_json=?, model=?, completed_at=? WHERE item_id=?",
            (_canon(result), elapsed, _canon(usage) if usage is not None else None, model, now, item_id))
        call_status = "done"
    elif status == "budget_exhausted":
        await db.execute(
            "UPDATE figure_items SET status='budget_exhausted', error=?, elapsed_s=?, usage_json=?, "
            "model=?, completed_at=? WHERE item_id=?",
            (error or "budget exhausted", elapsed, _canon(usage) if usage is not None else None,
             model, now, item_id))
        call_status = "budget_exhausted"
    else:  # failed
        if item["attempts"] < MAX_ATTEMPTS:
            await db.execute(
                "UPDATE figure_items SET status='queued', error=?, elapsed_s=?, usage_json=?, "
                "model=?, claimed_by=NULL WHERE item_id=?",
                (error, elapsed, _canon(usage) if usage is not None else None, model, item_id))
        else:
            await db.execute(
                "UPDATE figure_items SET status='failed', error=?, elapsed_s=?, usage_json=?, "
                "model=?, completed_at=? WHERE item_id=?",
                (error, elapsed, _canon(usage) if usage is not None else None, model, now, item_id))
        call_status = "failed"
    await db.execute(
        "INSERT INTO figure_calls (at, kind, client_id, item_id, model, prompt_version, status, "
        "elapsed_s, usage_json, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now, item["kind"], item["client_id"], item_id, model, pv, call_status, elapsed,
         _canon(usage) if usage is not None else None, error))
    await _refresh_job_status(item["job_id"])
    if status != "fatal":
        await db.execute("UPDATE figure_worker SET last_seen=? WHERE id=1", (now,))
    await db.commit()
    async with db.execute("SELECT status, attempts FROM figure_items WHERE item_id=?", (item_id,)) as c:
        st = await c.fetchone()
    return {"item_id": item_id, "status": st[0], "attempts": st[1],
            "worker": _worker_public(await _worker_row())}
