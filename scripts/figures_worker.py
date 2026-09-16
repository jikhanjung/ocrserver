#!/usr/bin/env python3
"""figures_worker — host-side executor for wrapper 0.3.0 figure jobs (P02 stage 2).

    wrapper (container, DB writer) ◀─ /internal/figures/* ─▶ this process (host, jikhanjung)
                                                              │
                                                              ├─ builds a per-paper workspace
                                                              │  (client-uploaded page text + page renders)
                                                              ├─ renders the figure crop / target page
                                                              └─ runs `codex exec` (gpt-6-astra) and posts the JSON back

One item at a time. One call per FIGURES_MIN_INTERVAL seconds (advertised by
the wrapper, D8). Never touches SQLite. Never reads the wrapper's OCR rows —
workspace text comes from what the client uploaded (P02 §3.3).

Outcomes reported to the wrapper (see wrapper/figures.py item_result):
    done              response parsed + passes the request's JSON schema
    failed            CLI error / unparsable / schema violation  → wrapper re-queues while attempts remain
    budget_exhausted  session hit the per-kind timeout           → terminal
    fatal             not logged in / CLI missing / usage limit  → wrapper pauses the worker, item re-queued

Env (systemd unit loads /srv/ocrserver/.env):
    FIGURES_WORKER_TOKEN   required
    WRAPPER_URL            default http://127.0.0.1:8080  (nginx allows /internal/ from loopback)
    PDF_DIR                default /srv/ocrserver/data/pdfs   (read-only is enough)
    FIGURES_WS_DIR         default /srv/ocrserver/figure_ws   (owned by the worker user)
    CODEX_BIN              default codex
    FIGURES_POLL_INTERVAL  default 30   seconds between empty claims
    FIGURES_SESSION_TIMEOUT_DETECT / _LINK / _PANELS   default 600 / 1200 / 600
    FIGURES_PAGE_DPI       default 100  full-page renders in the workspace
    FIGURES_TARGET_DPI     default 150  target page with the hint box (detect)
    FIGURES_MAX_LONG_PX    default 4000 cap for the panels crop
"""
import html
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time

import fitz
import requests
from PIL import Image, ImageDraw

VERSION = "0.3.0"
WRAPPER_URL = os.getenv("WRAPPER_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.getenv("FIGURES_WORKER_TOKEN", "")
PDF_DIR = os.getenv("PDF_DIR", "/srv/ocrserver/data/pdfs")
WS_DIR = os.getenv("FIGURES_WS_DIR", "/srv/ocrserver/figure_ws")
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
POLL_INTERVAL = int(os.getenv("FIGURES_POLL_INTERVAL", "30"))
SESSION_TIMEOUT = {
    "detect": int(os.getenv("FIGURES_SESSION_TIMEOUT_DETECT", "600")),
    "link": int(os.getenv("FIGURES_SESSION_TIMEOUT_LINK", "1200")),
    "panels": int(os.getenv("FIGURES_SESSION_TIMEOUT_PANELS", "600")),
}
PAGE_DPI = int(os.getenv("FIGURES_PAGE_DPI", "100"))
TARGET_DPI = int(os.getenv("FIGURES_TARGET_DPI", "150"))
MAX_LONG_PX = int(os.getenv("FIGURES_MAX_LONG_PX", "4000"))
WS_TTL_DAYS = int(os.getenv("FIGURES_WORKSPACE_TTL_DAYS", "7"))
WORKER_ID = os.getenv("FIGURES_WORKER_ID", socket.gethostname())
DEFAULT_MODEL = "gpt-6-astra"

# Fatal = the next item would fail the same way; the wrapper pauses us.
FATAL_MARKERS = ("login required", "not logged in", "codex cli not found",
                 "usage limit", "rate limit", "hit your usage", "quota")
# stderr noise seen on every run on this host (KOPRI network); never fatal.
NOISE = ("failed to refresh available models", "backend-api/ps/mcp",
         "transport channel closed")

_stop = False


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ── wrapper API ───────────────────────────────────────────────────────────────

def api(method: str, path: str, **kw):
    r = requests.request(method, f"{WRAPPER_URL}{path}", headers={"X-Worker-Token": TOKEN},
                         timeout=kw.pop("timeout", 60), **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} → {r.status_code} {r.text[:300]}")
    return r.json() if r.content else {}


def set_status(state: str, **extra) -> None:
    try:
        api("POST", "/internal/figures/worker/status",
            json={"worker_id": WORKER_ID, "version": VERSION, "state": state, **extra})
    except Exception as e:
        log(f"status post failed: {e}")


class Heartbeat(threading.Thread):
    def __init__(self, item_id: str):
        super().__init__(daemon=True)
        self.item_id, self._ev = item_id, threading.Event()

    def run(self):
        while not self._ev.wait(60):
            try:
                api("POST", f"/internal/figures/items/{self.item_id}/heartbeat", timeout=15)
            except Exception as e:
                log(f"heartbeat failed: {e}")

    def stop(self):
        self._ev.set()


# ── workspace ─────────────────────────────────────────────────────────────────

_DIV_RE = re.compile(r"<div\b([^>]*)>(.*?)</div>", re.S | re.I)
_ATTR_RE = re.compile(r'data-(bbox|label)="([^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")
_ALT_RE = re.compile(r'alt="([^"]*)"')


def _text(inner: str) -> str:
    t = _TAG_RE.sub(" ", inner.replace("</p>", "\n").replace("<br>", "\n").replace("<br/>", "\n"))
    t = html.unescape(t)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def page_text(markdown: str) -> str:
    """chandra layout HTML → '[Label x0 y0 x1 y1] text' per block. Falls back
    to a plain tag strip for pre-layout output."""
    lines = []
    for attrs, inner in _DIV_RE.findall(markdown or ""):
        a = dict(_ATTR_RE.findall(attrs))
        body = _text(inner)
        # picture blocks usually carry only <img alt="…">: keep the model's description
        alts = [html.unescape(x) for x in _ALT_RE.findall(inner) if x.strip()]
        if alts:
            body = ("[image: " + " | ".join(alts) + "] " + body).strip()
        if body or a.get("label") in ("Figure", "Image", "Diagram", "Picture"):
            lines.append(f"[{a.get('label', '?')} {a.get('bbox', '')}] {body}".rstrip())
    if lines:
        return "\n".join(lines)
    return _text(markdown or "")


def ws_root(file_hash: str, ocr_digest: str) -> str:
    return os.path.join(WS_DIR, file_hash, ocr_digest)


def ensure_workspace(file_hash: str, ocr_digest: str, pdf_path: str) -> str:
    """Materialize text/ + pages/ once per (paper, OCR version). Idempotent."""
    root = ws_root(file_hash, ocr_digest)
    ready = os.path.join(root, ".ready")
    if os.path.exists(ready):
        return root
    os.makedirs(os.path.join(root, "text"), exist_ok=True)
    os.makedirs(os.path.join(root, "pages"), exist_ok=True)
    ws = api("GET", f"/internal/figures/workspace/{file_hash}/{ocr_digest}", timeout=120)
    pages = sorted(ws["pages"], key=lambda p: p["page"])
    all_parts = []
    for p in pages:
        t = page_text(p["markdown"])
        with open(os.path.join(root, "text", f"p{p['page']:03d}.txt"), "w") as f:
            f.write(t + "\n")
        all_parts.append(f"=== page {p['page']} ===\n{t}\n")
    with open(os.path.join(root, "text", "all.txt"), "w") as f:
        f.write("\n".join(all_parts))
    with fitz.open(pdf_path) as doc:
        n = len(doc)
        for i in range(n):
            out = os.path.join(root, "pages", f"p{i:03d}.png")
            if os.path.exists(out):
                continue
            pix = doc[i].get_pixmap(matrix=fitz.Matrix(PAGE_DPI / 72, PAGE_DPI / 72),
                                    colorspace=fitz.csRGB, alpha=False)
            pix.save(out)
    with open(os.path.join(root, "README.txt"), "w") as f:
        f.write(
            f"One paper. {n} PDF pages, {len(pages)} pages of OCR text. PAGE NUMBERS ARE 0-BASED everywhere.\n"
            f"text/pNNN.txt   OCR text of page NNN, one line per layout block: [Label x0 y0 x1 y1] text\n"
            f"                (bbox = page-relative 0..1000, origin top-left; labels: Text, Caption, Figure, Image, Table, ...)\n"
            f"text/all.txt    all pages, separated by '=== page N ===' — grep here first\n"
            f"pages/pNNN.png  full page render at {PAGE_DPI} dpi — open only the pages you need\n"
            f"items/<id>/     the current task: figure.json (input) and target.png (target page with the hint box)\n")
    with open(ready, "w") as f:
        f.write(str(time.time()))
    log(f"workspace built {file_hash[:12]}/{ocr_digest[:12]}: {n} pages")
    return root


def sweep_workspaces() -> int:
    """Delete per-paper workspaces untouched for WS_TTL_DAYS (mirrors the
    wrapper's figure_workspaces TTL). Run at startup and once a day."""
    cutoff = time.time() - WS_TTL_DAYS * 86400
    removed = 0
    if not os.path.isdir(WS_DIR):
        return 0
    for fh in os.listdir(WS_DIR):
        top = os.path.join(WS_DIR, fh)
        if not os.path.isdir(top):
            continue
        for sub in os.listdir(top):
            d = os.path.join(top, sub)
            try:
                if os.path.isdir(d) and os.path.getmtime(d) < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
        if not os.listdir(top):
            os.rmdir(top)
    if removed:
        log(f"workspace sweep: removed {removed} dir(s) older than {WS_TTL_DAYS}d")
    return removed


# ── rendering ─────────────────────────────────────────────────────────────────

def render_target(pdf_path: str, page: int, hint_bbox, out: str) -> dict:
    with fitz.open(pdf_path) as doc:
        pg = doc[page]
        pix = pg.get_pixmap(matrix=fitz.Matrix(TARGET_DPI / 72, TARGET_DPI / 72),
                            colorspace=fitz.csRGB, alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if hint_bbox:
        d = ImageDraw.Draw(im)
        x0, y0, x1, y1 = hint_bbox
        w, h = im.size
        d.rectangle([x0 / 1000 * w, y0 / 1000 * h, x1 / 1000 * w, y1 / 1000 * h],
                    outline=(220, 0, 0), width=4)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 170, 28], fill=(255, 255, 255))
    d.text((6, 6), f"page {page} (0-based)", fill=(0, 0, 0))
    im.save(out)
    return {"width": im.size[0], "height": im.size[1], "dpi": TARGET_DPI}


def render_crop(pdf_path: str, page: int, bbox, dpi: int, out: str) -> dict:
    """Same geometry as fsis figure_panels.render_figure (permille × page rect,
    clip render), plus a long-side cap so a full-page plate doesn't become a
    40-megapixel upload."""
    x0, y0, x1, y1 = bbox
    with fitz.open(pdf_path) as doc:
        pg = doc[page]
        clip = fitz.Rect(x0 / 1000 * pg.rect.width, y0 / 1000 * pg.rect.height,
                         x1 / 1000 * pg.rect.width, y1 / 1000 * pg.rect.height)
        long_pt = max(clip.width, clip.height)
        eff = min(dpi, MAX_LONG_PX * 72 / long_pt) if long_pt > 0 else dpi
        pix = pg.get_pixmap(matrix=fitz.Matrix(eff / 72, eff / 72), clip=clip,
                            colorspace=fitz.csRGB, alpha=False)
        pix.save(out)
        return {"width": pix.width, "height": pix.height, "dpi": round(eff, 2),
                "pdf_clip_xyxy_points": [clip.x0, clip.y0, clip.x1, clip.y1]}


# ── codex ─────────────────────────────────────────────────────────────────────

def _clean_env() -> dict:
    env = os.environ.copy()
    for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(k, None)
    return env


def _run(cmd, cwd, timeout, stdin=None):
    p = subprocess.Popen(cmd, cwd=cwd, env=_clean_env(), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True)
    try:
        out, err = p.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        return None, out, err
    return p.returncode, out, err


def _strip_noise(s: str) -> str:
    return "\n".join(l for l in (s or "").splitlines() if not any(n in l.lower() for n in NOISE))


def fatal_reason(*texts) -> str | None:
    blob = _strip_noise("\n".join(t for t in texts if t)).lower()
    for m in FATAL_MARKERS:
        if m in blob:
            return m
    return None


def check_login() -> str | None:
    if not shutil.which(CODEX_BIN):
        return "Codex CLI not found"
    code, out, err = _run([CODEX_BIN, "login", "status"], None, 30)
    txt = (out or "") + (err or "")
    if code is None:
        return None  # timeout on the status probe alone is not proof of anything
    if code or "logged in using chatgpt" not in txt.lower():
        return "ChatGPT login required: " + _strip_noise(txt).strip()[:200]
    return None


def run_codex(cwd: str, prompt: str, schema: dict, images: list, model: str, effort: str,
              timeout: int, run_dir: str) -> dict:
    os.makedirs(run_dir, exist_ok=True)
    schema_path = os.path.join(run_dir, "schema.json")
    resp_path = os.path.join(run_dir, "response.json")
    with open(schema_path, "w") as f:
        json.dump(schema, f, ensure_ascii=False, indent=1)
    with open(os.path.join(run_dir, "prompt.txt"), "w") as f:
        f.write(prompt)
    cmd = [CODEX_BIN, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
           "--sandbox", "read-only", "--model", model,
           "-c", "model_reasoning_effort=" + json.dumps(effort)]
    for im in images:
        cmd += ["--image", im]
    cmd += ["--output-schema", schema_path, "--output-last-message", resp_path, "--json", "-"]
    t0 = time.monotonic()
    code, out, err = _run(cmd, cwd, timeout, stdin=prompt)
    elapsed = round(time.monotonic() - t0, 1)
    with open(os.path.join(run_dir, "events.jsonl"), "w") as f:
        f.write(out or "")
    with open(os.path.join(run_dir, "stderr.log"), "w") as f:
        f.write(err or "")
    events = []
    for line in (out or "").splitlines():
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    usage = {}
    for e in events:
        if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict):
            for k, v in e["usage"].items():
                if isinstance(v, (int, float)):
                    usage[k] = usage.get(k, 0) + v
    turn_ok = any(e.get("type") == "turn.completed" for e in events)
    turn_err = [e for e in events if e.get("type") in ("turn.failed", "error")]
    response = None
    if os.path.exists(resp_path):
        try:
            with open(resp_path) as f:
                response = json.load(f)
        except Exception:
            response = None
    info = {"code": code, "elapsed": elapsed, "usage": usage, "turn_ok": turn_ok,
            "turn_err": turn_err[:3], "response": response, "stdout": out or "", "stderr": err or "",
            "command": cmd}
    with open(os.path.join(run_dir, "run.json"), "w") as f:
        json.dump({k: v for k, v in info.items() if k not in ("stdout", "stderr", "response")},
                  f, ensure_ascii=False, indent=1, default=str)
    return info


# ── minimal JSON-schema check (no jsonschema on the host) ─────────────────────

def schema_errors(value, schema: dict, path: str = "$") -> list:
    errs = []
    if not isinstance(schema, dict):
        return errs
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool, "null": type(None)}
    if t:
        allowed = t if isinstance(t, list) else [t]
        ok = any(isinstance(value, types[a]) and not (a in ("integer", "number") and isinstance(value, bool))
                 for a in allowed if a in types)
        if not ok:
            return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}.{k}: required")
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                errs += schema_errors(value[k], sub, f"{path}.{k}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            errs += schema_errors(v, schema["items"], f"{path}[{i}]")
    return errs[:10]


# ── one item ──────────────────────────────────────────────────────────────────

def build_prompt(instructions: str, payload: dict) -> str:
    return (instructions.rstrip() + "\n\n=== INPUT (JSON) ===\n"
            + json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def process(item: dict) -> dict:
    """Returns the payload to POST as the item's result."""
    kind = item["kind"]
    req = item["request"]
    prompt = item["prompt"]
    options = item.get("options") or {}
    model = options.get("model") or DEFAULT_MODEL
    effort = options.get("effort") or "high"
    # The wrapper's pdf_path is a container path; resolve against our own PDF_DIR.
    pdf_path = os.path.join(PDF_DIR, f"{item['file_hash']}.pdf")
    if not os.path.exists(pdf_path):
        return {"status": "failed", "error": f"pdf_missing: {pdf_path}"}
    attempt = item.get("attempt", 1)

    reason = check_login()
    if reason:
        return {"status": "fatal", "error": reason}

    if kind in ("detect", "link"):
        root = ensure_workspace(item["file_hash"], item["ocr_digest"], pdf_path)
        item_dir = os.path.join(root, "items", item["item_id"])
        os.makedirs(item_dir, exist_ok=True)
        cwd, images = root, []
        with fitz.open(pdf_path) as doc:
            n_pages = len(doc)
        payload = {"kind": kind, "item": req,
                   "workspace": {"pdf_pages": n_pages, "page_numbering": "0-based",
                                 "text_dir": "text/", "all_text": "text/all.txt",
                                 "pages_dir": "pages/", "page_dpi": PAGE_DPI}}
        if kind == "detect":
            page = req["page"]
            if page >= n_pages:
                return {"status": "failed", "error": f"page {page} out of range ({n_pages} pages)"}
            target = os.path.join(item_dir, "target.png")
            tinfo = render_target(pdf_path, page, req.get("hint_bbox_page_1000"), target)
            rel = os.path.relpath(target, root)
            payload["target_image"] = {"path": rel, **tinfo,
                                       "hint_box_drawn": bool(req.get("hint_bbox_page_1000"))}
            images = [rel]
        with open(os.path.join(item_dir, "figure.json"), "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        run_dir = os.path.join(item_dir, "run", f"a{attempt}")
    else:  # panels
        item_dir = os.path.join(WS_DIR, item["file_hash"], "panels", item["item_id"])
        os.makedirs(item_dir, exist_ok=True)
        with fitz.open(pdf_path) as doc:
            n_pages = len(doc)
        if req["page"] >= n_pages:
            return {"status": "failed", "error": f"page {req['page']} out of range ({n_pages} pages)"}
        dpi = int(options.get("dpi") or 216)
        fig_png = os.path.join(item_dir, "figure.png")
        rinfo = render_crop(pdf_path, req["page"], req["bbox_page_1000"], dpi, fig_png)
        payload = {"kind": kind, "item": req,
                   "image": {"path": "figure.png", **rinfo},
                   # astra_panels.py contract names, so the fsis prompt works unchanged
                   "image_width": rinfo["width"], "image_height": rinfo["height"],
                   "original_caption": req.get("caption", ""),
                   "existing_subfigures": req.get("entries", [])}
        cwd, images = item_dir, ["figure.png"]
        run_dir = os.path.join(item_dir, "run", f"a{attempt}")

    text = build_prompt(prompt["instructions"], payload)
    hb = Heartbeat(item["item_id"])
    hb.start()
    try:
        info = run_codex(cwd, text, prompt["schema"], images, model, effort,
                         SESSION_TIMEOUT[kind], run_dir)
    finally:
        hb.stop()
    base = {"elapsed_s": info["elapsed"], "usage": info["usage"] or None, "model": model}
    fr = fatal_reason(info["stdout"], info["stderr"])
    if fr:
        return {"status": "fatal", "error": f"codex: {fr} — {run_dir}", **base}
    if info["code"] is None:
        return {"status": "budget_exhausted",
                "error": f"session exceeded {SESSION_TIMEOUT[kind]}s — {run_dir}", **base}
    if info["code"]:
        tail = _strip_noise(info["stderr"]).strip().splitlines()[-1:] or [""]
        return {"status": "failed", "error": f"codex exit {info['code']}: {tail[0][:200]} — {run_dir}", **base}
    if info["turn_err"] or not info["turn_ok"]:
        return {"status": "failed", "error": f"codex turn failed/incomplete — {run_dir}", **base}
    if not isinstance(info["response"], dict):
        return {"status": "failed", "error": f"no JSON object in response — {run_dir}", **base}
    errs = schema_errors(info["response"], prompt["schema"])
    if errs:
        return {"status": "failed", "error": "schema: " + "; ".join(errs) + f" — {run_dir}", **base}
    return {"status": "done", "result": info["response"], **base}


# ── main loop ─────────────────────────────────────────────────────────────────

def _sleep(seconds: float, state: str = "sleeping") -> None:
    set_status(state, next_call_at=time.time() + seconds)
    end = time.monotonic() + seconds
    while not _stop and time.monotonic() < end:
        time.sleep(min(5, end - time.monotonic()))


def main() -> int:
    global _stop
    if not TOKEN:
        log("FIGURES_WORKER_TOKEN is not set"); return 2
    os.makedirs(WS_DIR, exist_ok=True)

    def _sig(*_):
        global _stop
        _stop = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    log(f"figures_worker {VERSION} id={WORKER_ID} wrapper={WRAPPER_URL} ws={WS_DIR} codex={shutil.which(CODEX_BIN)}")
    set_status("idle")
    sweep_workspaces()
    last_sweep = time.monotonic()
    while not _stop:
        if time.monotonic() - last_sweep > 86400:
            sweep_workspaces(); last_sweep = time.monotonic()
        try:
            c = api("POST", "/internal/figures/claim", json={"worker_id": WORKER_ID})
        except Exception as e:
            log(f"claim failed: {e}"); _sleep(POLL_INTERVAL, "idle"); continue
        item = c.get("item")
        interval = int(c.get("min_interval_s") or 300)
        if not item:
            w = c.get("worker") or {}
            if w.get("state") == "paused":
                log(f"paused by wrapper: {w.get('paused_reason')} — waiting for /figures/worker/resume")
            _sleep(POLL_INTERVAL, "idle")
            continue
        log(f"claim {item['kind']} {item['key']} job={item['job_id'][:8]} attempt={item['attempt']} client={item['client_id']}")
        set_status("running")
        try:
            res = process(item)
        except Exception as e:
            res = {"status": "failed", "error": f"worker exception: {type(e).__name__}: {e}"}
        log(f"  → {res['status']} {res.get('elapsed_s', '')}s {(res.get('error') or '')[:160]}")
        try:
            api("POST", f"/internal/figures/items/{item['item_id']}/result", json=res, timeout=120)
        except Exception as e:
            log(f"result post failed: {e}")
        if res["status"] == "fatal":
            _sleep(POLL_INTERVAL, "idle")
        else:
            _sleep(interval)
    set_status("idle")
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
