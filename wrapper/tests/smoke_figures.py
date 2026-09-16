"""Smoke test for the figure-split job API (wrapper 0.3.0).

Runs the FastAPI app in-process against a temp DB with the worker token set,
then walks the whole contract: PDF store → workspace → submit (all kinds,
validation errors, dedup) → claim → heartbeat → result variants (done /
failed→requeue / fatal→pause / budget) → resume → dashboard summary.

Run inside the wrapper image (has fastapi/httpx/aiosqlite/pymupdf):
    docker run --rm -e FIGURES_WORKER_TOKEN=t -v $PWD/wrapper:/app honestjung/ocrwrapper:0.3.0 \
        python -m tests.smoke_figures
"""
import json
import os
import sys
import tempfile

import fitz

tmp = tempfile.mkdtemp(prefix="figsmoke-")
os.environ["DB_PATH"] = os.path.join(tmp, "ocrserver.db")
os.environ["PDF_DIR"] = os.path.join(tmp, "pdfs")
os.environ["LLM_DB_PATH"] = os.path.join(tmp, "llm.db")
os.environ["METRICS_DB_PATH"] = os.path.join(tmp, "metrics.db")
os.environ.setdefault("FIGURES_WORKER_TOKEN", "t")
os.environ["FIGURES_MAX_ATTEMPTS"] = "2"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

TOKEN = os.environ["FIGURES_WORKER_TOKEN"]
W = {"X-Worker-Token": TOKEN}
PROMPT = {"version": "test-v1", "instructions": "do the thing", "schema": {"type": "object"}}
checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)


def make_pdf() -> bytes:
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {i} FIGURE 1")
    return doc.tobytes()


with TestClient(main.app) as c:
    pdf = make_pdf()
    # ── /pdfs ──
    r = c.post("/pdfs", files={"file": ("a.pdf", pdf, "application/pdf")}, data={"client_id": "pm-a"})
    ok(r.status_code == 201 and r.json()["existed"] is False, f"pdf upload {r.text}")
    h = r.json()["file_hash"]
    ok(c.head(f"/pdfs/{h}").status_code == 200, "pdf head 200")
    ok(c.head("/pdfs/" + "0" * 64).status_code == 404, "pdf head 404")
    ok(c.post("/pdfs", files={"file": ("x.pdf", b"nope", "application/pdf")}).status_code == 400, "non-pdf rejected")
    ok(c.post("/pdfs", files={"file": ("a.pdf", pdf, "application/pdf")}).json()["existed"] is True, "re-upload existed")

    # ── workspace ──
    pages = [{"page": i, "markdown": f"<div data-bbox='1 2 3 4' data-label='Text'><p>page {i}</p></div>"} for i in range(3)]
    r = c.post("/figures/workspace", json={"client_id": "pm-a", "file_hash": h, "ocr_digest": "d1d1d1d1d1d1d1d1", "pages": pages})
    ok(r.status_code == 201 and r.json()["pages"] == 3, f"workspace {r.text}")
    ok(c.head(f"/figures/workspace/{h}/d1d1d1d1d1d1d1d1").status_code == 200, "workspace head")
    ok(c.head(f"/figures/workspace/{h}/nope").status_code == 404, "workspace missing 404")
    r = c.post("/figures/workspace", json={"file_hash": "0" * 64, "ocr_digest": "d1d1d1d1d1d1d1d1", "pages": pages})
    ok(r.status_code == 404 and "pdf_missing" in r.text, "workspace needs pdf")
    r = c.post("/figures/workspace", json={"file_hash": h, "ocr_digest": "d1d1d1d1d1d1d1d1", "pages": [{"page": 0}]})
    ok(r.status_code == 400, "workspace page validation")

    # ── submit: validation ──
    base = {"client_id": "pm-a", "file_hash": h, "ocr_digest": "d1d1d1d1d1d1d1d1", "prompt": PROMPT}
    ok(c.post("/figures/bogus", json=base).status_code == 404, "unknown kind")
    r = c.post("/figures/detect", json={**base, "items": [{"key": "x", "page": -1}]})
    ok(r.status_code == 400, "detect page validation")
    r = c.post("/figures/detect", json={**base, "ocr_digest": "zzzzzzzzzzzzzzzz", "items": [{"key": "x", "page": 0}]})
    ok(r.status_code == 404 and "workspace_missing" in r.text, "detect needs workspace")
    r = c.post("/figures/panels", json={**base, "prompt": {"version": "v"}, "items": [{"key": "x", "page": 0, "bbox_page_1000": [1, 2, 3, 4]}]})
    ok(r.status_code == 400 and "instructions" in r.text, "prompt validation")
    r = c.post("/figures/panels", json={**base, "items": [{"key": "x", "page": 0, "bbox_page_1000": [1, 2, 3]}]})
    ok(r.status_code == 400, "bbox validation")
    r = c.post("/figures/link", json={**base, "items": [{"key": "paper", "figures": []}]})
    ok(r.status_code == 400, "link figures non-empty")
    r = c.post("/figures/panels", json={**base, "items": [{"key": "a", "page": 0, "bbox_page_1000": [1, 2, 3, 4]},
                                                          {"key": "a", "page": 1, "bbox_page_1000": [1, 2, 3, 4]}]})
    ok(r.status_code == 400 and "duplicate" in r.text, "duplicate keys")

    # ── submit: real jobs ──
    items = [{"key": f"f{i}", "page": i, "bbox_page_1000": [100, 100, 900, 800], "caption": "Fig", "entries": [{"label": "1"}]}
             for i in range(3)]
    r = c.post("/figures/panels", json={**base, "items": items})
    ok(r.status_code == 202 and r.json()["queued"] == 3, f"panels submit {r.text}")
    j1 = r.json()["job_id"]
    r = c.post("/figures/detect", json={**base, "client_id": "pm-b", "items": [{"key": "d0", "page": 0, "hint_bbox_page_1000": None, "reasons": ["no_caption"]}]})
    ok(r.status_code == 202, f"detect submit {r.text}")
    j2 = r.json()["job_id"]
    r = c.post("/figures/link", json={**base, "items": [{"key": "paper", "figures": [{"figure_id": "f1", "page": 0, "bbox_page_1000": [1, 2, 3, 4]}]}]})
    ok(r.status_code == 202, f"link submit {r.text}")
    j3 = r.json()["job_id"]
    ok(c.get(f"/figures/panels/{j1}").json()["status"] == "queued", "job queued")
    ok(c.get(f"/figures/detect/{j1}").status_code == 404, "kind mismatch 404")
    ok(len(c.get("/figures/jobs?client_id=pm-a").json()["items"]) == 2, "list by client")

    # ── internal auth ──
    ok(c.post("/internal/figures/claim", json={}).status_code == 403, "claim without token")
    ok(c.post("/internal/figures/claim", json={}, headers={"X-Worker-Token": "wrong"}).status_code == 403, "claim bad token")

    # ── claim: fairness — pm-a submitted first but pm-b has never completed anything either; ties by first submit → pm-a
    r = c.post("/internal/figures/claim", json={"worker_id": "w1"}, headers=W)
    ok(r.status_code == 200 and r.json()["item"]["client_id"] == "pm-a", f"claim 1 {r.text}")
    it = r.json()["item"]
    ok(it["kind"] == "panels" and it["attempt"] == 1 and it["prompt"]["version"] == "test-v1"
       and it["pdf_path"].endswith(f"{h}.pdf") and it["request"]["page"] == 0, "claim payload")
    ok(c.get(f"/figures/panels/{j1}").json()["status"] == "processing", "job processing after claim")
    ok(c.post(f"/internal/figures/items/{it['item_id']}/heartbeat", headers=W).status_code == 200, "heartbeat")

    # ── result: done ──
    r = c.post(f"/internal/figures/items/{it['item_id']}/result", headers=W,
               json={"status": "done", "result": {"panels": [1, 2]}, "elapsed_s": 9.5, "usage": {"input_tokens": 10}, "model": "gpt-6-astra"})
    ok(r.status_code == 200 and r.json()["status"] == "done", f"result done {r.text}")
    ok(c.post(f"/internal/figures/items/{it['item_id']}/result", headers=W, json={"status": "done", "result": {}}).status_code == 409, "double result 409")

    # ── next claim goes to pm-b (never completed) ──
    r = c.post("/internal/figures/claim", json={"worker_id": "w1"}, headers=W)
    ok(r.json()["item"]["client_id"] == "pm-b" and r.json()["item"]["kind"] == "detect", f"fairness {r.text}")
    it2 = r.json()["item"]

    # ── result: fatal → item requeued, attempts not counted, worker paused ──
    r = c.post(f"/internal/figures/items/{it2['item_id']}/result", headers=W, json={"status": "fatal", "error": "ChatGPT login required"})
    ok(r.json()["status"] == "queued" and r.json()["attempts"] == 0 and r.json()["worker"]["state"] == "paused", f"fatal {r.text}")
    r = c.post("/internal/figures/claim", json={"worker_id": "w1"}, headers=W)
    ok(r.json()["item"] is None and r.json()["worker"]["state"] == "paused", "claim returns nothing while paused")
    ok(c.get("/api/figures").json()["worker"]["paused_reason"].startswith("ChatGPT"), "api/figures shows pause")
    r = c.post("/figures/worker/resume")
    ok(r.json()["state"] == "idle", "worker resume")

    # ── result: failed twice → failed (MAX_ATTEMPTS=2) ──
    r = c.post("/internal/figures/claim", json={"worker_id": "w1"}, headers=W)
    it3 = r.json()["item"]
    ok(it3["client_id"] == "pm-b", "pm-b again after pause")
    r = c.post(f"/internal/figures/items/{it3['item_id']}/result", headers=W, json={"status": "failed", "error": "boom"})
    ok(r.json()["status"] == "queued" and r.json()["attempts"] == 1, f"failed → requeue {r.text}")
    # pm-a has a completion, pm-b none → pm-b first again
    r = c.post("/internal/figures/claim", json={"worker_id": "w1"}, headers=W)
    ok(r.json()["item"]["item_id"] == it3["item_id"] and r.json()["item"]["attempt"] == 2, "second attempt")
    r = c.post(f"/internal/figures/items/{it3['item_id']}/result", headers=W, json={"status": "failed", "error": "boom2"})
    ok(r.json()["status"] == "failed", "attempts exhausted → failed")
    jd = c.get(f"/figures/detect/{j2}").json()
    ok(jd["status"] == "failed" and jd["items"][0]["error"] == "boom2", f"detect job failed {jd['status']}")
    r = c.post(f"/figures/detect/{j2}/resume")
    ok(r.json()["requeued"] == 0, "resume without retry_errors: nothing (attempts exhausted)")
    r = c.post(f"/figures/detect/{j2}/resume?retry_errors=true")
    ok(r.json()["requeued"] == 1 and c.get(f"/figures/detect/{j2}").json()["status"] == "queued", "resume retry_errors")

    # ── budget_exhausted ──
    r = c.post("/internal/figures/claim", json={"worker_id": "w1"}, headers=W)
    it4 = r.json()["item"]
    r = c.post(f"/internal/figures/items/{it4['item_id']}/result", headers=W, json={"status": "budget_exhausted", "error": "10 min cap"})
    ok(r.json()["status"] == "budget_exhausted", "budget status")

    # ── finish remaining panels + link, check job rollups ──
    for _ in range(3):
        r = c.post("/internal/figures/claim", json={"worker_id": "w1"}, headers=W)
        item = r.json()["item"]
        if item is None:
            break
        c.post(f"/internal/figures/items/{item['item_id']}/result", headers=W, json={"status": "done", "result": {"k": item["key"]}, "elapsed_s": 1})
    ok(c.post("/internal/figures/claim", json={"worker_id": "w1"}, headers=W).json()["item"] is None, "queue drained")
    # Which job absorbed the budget_exhausted item depends on fair-share order
    # (pm-a completed first, so pm-a's next item was picked) — assert accordingly.
    jp = c.get(f"/figures/panels/{j1}").json()
    if it4["job_id"] == j1:
        ok(jp["status"] == "done_with_errors" and jp["done"] == 2 and jp["failed"] == 1 and jp["completed_at"],
           f"panels job rollup {jp['status']} {jp['done']}/{jp['failed']}")
        ok(c.get(f"/figures/detect/{j2}").json()["status"] == "done", "detect job done after retry")
    else:
        ok(jp["status"] == "done" and jp["done"] == 3 and jp["completed_at"], f"panels job done {jp['status']} {jp['done']}")
        ok(c.get(f"/figures/detect/{j2}").json()["status"] == "failed", "detect job failed (budget)")
    ok(c.get(f"/figures/link/{j3}").json()["status"] == "done", "link job done")

    # ── dedup: resubmit same panels → all cached ──
    r = c.post("/figures/panels", json={**base, "items": items})
    n_done_panels = jp["done"]
    ok(r.json()["cached"] == n_done_panels and r.json()["queued"] == 3 - n_done_panels, f"dedup {r.text}")
    jc = c.get(f"/figures/panels/{r.json()['job_id']}").json()
    ok(jc["items"][0]["status"] == "done" and jc["items"][0]["result"] == {"panels": [1, 2]}, "cached result copied")
    # drain the re-queued remainder (if any) so the summary below is deterministic
    while True:
        rr = c.post("/internal/figures/claim", json={"worker_id": "w1"}, headers=W).json()
        if rr["item"] is None or rr["item"]["job_id"] != jc["job_id"]:
            break
        c.post(f"/internal/figures/items/{rr['item']['item_id']}/result", headers=W, json={"status": "done", "result": {"x": 1}, "elapsed_s": 1})
    ok(c.get(f"/figures/panels/{jc['job_id']}").json()["status"] == "done", "dedup job completes")
    r = c.post("/figures/panels", json={**base, "items": items, "force": True})
    ok(r.json()["cached"] == 0, "force bypasses dedup")
    r = c.post("/figures/panels", json={**base, "client_id": "pm-c", "items": items})
    ok(r.json()["cached"] == 0, "dedup is per client_id")

    # ── worker status + summary ──
    r = c.post("/internal/figures/worker/status", headers=W, json={"worker_id": "w1", "version": "0.3.0", "state": "sleeping", "next_call_at": 1})
    ok(r.json()["state"] == "sleeping", "worker status")
    f = c.get("/api/figures").json()
    ok(f["calls_24h"]["total"] >= 8 and f["items"]["panels"]["done"] >= 5 and f["worker"]["alive"], f"summary {json.dumps(f)[:300]}")
    ok(c.get("/api/stats").status_code == 200 and c.get("/health").status_code == 200, "OCR endpoints still fine")

print(f"OK — {checks} checks passed")
