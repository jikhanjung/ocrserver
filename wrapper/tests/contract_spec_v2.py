"""Contract test against PaperMeister's figure_server_spec_v2 (2026-09-16).

Uses the client's *real* prompt/schema files (not a copy — they are the
client's domain rules) from PM_PROMPTS_DIR, builds the three requests exactly
as spec v2 §4-§6 show them, submits them to the in-process app, claims them
as the worker would, and checks the worker's schema check accepts the spec's
example replies and rejects a malformed one.

    docker run --rm -e FIGURES_WORKER_TOKEN=t -e PM_PROMPTS_DIR=/pm_prompts \
        -v $PWD/wrapper:/app -v <dir with detect.md,detect.schema.json,...>:/pm_prompts:ro \
        -w /app honestjung/ocrwrapper:0.3.3 python -m tests.contract_spec_v2
"""
import hashlib
import json
import os
import sys
import tempfile

import fitz

PM = os.environ.get("PM_PROMPTS_DIR")
if not PM or not os.path.exists(os.path.join(PM, "detect.md")):
    print("SKIP: PM_PROMPTS_DIR not set or has no prompts")
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="figcontract-")
os.environ["DB_PATH"] = os.path.join(tmp, "ocrserver.db")
os.environ["PDF_DIR"] = os.path.join(tmp, "pdfs")
os.environ["LLM_DB_PATH"] = os.path.join(tmp, "llm.db")
os.environ["METRICS_DB_PATH"] = os.path.join(tmp, "metrics.db")
os.environ.setdefault("FIGURES_WORKER_TOKEN", "t")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

# the worker's schema checker (host script, no fastapi needed)
WORKER = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts")
if not os.path.exists(os.path.join(WORKER, "figures_worker.py")):
    WORKER = "/worker_scripts"
sys.path.insert(0, WORKER)
from figures_worker import schema_errors, hint_boxes_of  # noqa: E402

W = {"X-Worker-Token": os.environ["FIGURES_WORKER_TOKEN"]}
checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)


def load(kind):  # same as papermeister.figure_prompts.load
    instructions = open(os.path.join(PM, f"{kind}.md"), encoding="utf-8").read()
    schema = json.load(open(os.path.join(PM, f"{kind}.schema.json"), encoding="utf-8"))
    digest = hashlib.sha256((instructions + json.dumps(schema, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()
    return {"kind": kind, "version": f"{kind}-v1-{digest[:12]}", "instructions": instructions, "schema": schema}


PROMPTS = {k: load(k) for k in ("detect", "link", "panels")}
for k, p in PROMPTS.items():
    ok(p["schema"].get("additionalProperties") is False and set(p["schema"]["required"]) == set(p["schema"]["properties"]),
       f"{k} schema: Codex structured-output constraints (all required, additionalProperties false)")

# ── spec §4-§6 example replies must pass the worker's checker ──
LINK_REPLY = {"figures": [{"figure_id": "984", "name": "Plate II", "caption": "PLATE II. Oistodus …",
                           "caption_source": "explanation_page", "caption_pages": [12], "continuation_of": None,
                           "entries": [{"label": "1", "printed_label": "1", "description": "Lateral view, YSUG 00287",
                                        "specimen_number": "YSUG 00287"}]}],
              "skipped": [{"figure_id": "990", "reason": "explanation_not_found"}],
              "pages_consulted": [3, 12, 13, 14, 41], "notes": ["Plate IV is printed twice, pp. 40 and 60"]}
PANELS_REPLY = {"is_compound": True, "figure_kind": "fossil_plate", "non_compound_reason": "",
                "panels": [{"label": "1", "bbox_figure_1000": [12, 8, 331, 402], "caption_indices": [0], "confidence": "high"}],
                "annotation_indices": [], "notes": ["shared scale bar bottom right"]}
DETECT_REPLY = {"figures": [{"bbox_page_1000": [40, 60, 960, 940], "from": ["1201", "1202", "1203"],
                             "name": "Plate IV", "name_inferred": True, "kind": "plate",
                             "caption": "", "caption_pages": [26], "caption_kind": "explanation_page", "confidence": "high"}],
                "dismiss": ["1210"], "pages_consulted": [26, 27, 28], "notes": []}
ok(schema_errors(LINK_REPLY, PROMPTS["link"]["schema"]) == [], "link example reply passes")
ok(schema_errors(PANELS_REPLY, PROMPTS["panels"]["schema"]) == [], "panels example reply passes")
ok(schema_errors(DETECT_REPLY, PROMPTS["detect"]["schema"]) == [], "detect example reply passes")
bad = json.loads(json.dumps(LINK_REPLY)); bad["figures"][0]["caption_source"] = "guess"; del bad["notes"]
errs = schema_errors(bad, PROMPTS["link"]["schema"])
ok(any("enum" in e for e in errs) and any("notes" in e for e in errs), f"bad link reply rejected: {errs}")
bad2 = json.loads(json.dumps(DETECT_REPLY)); bad2["figures"][0]["name_inferred"] = "yes"
ok(schema_errors(bad2, PROMPTS["detect"]["schema"]), "detect: string for boolean rejected")


def make_pdf(n=30):
    doc = fitz.open()
    for i in range(n):
        doc.new_page().insert_text((72, 72), f"page {i}")
    return doc.tobytes()


with TestClient(main.app) as c:
    pdf = make_pdf()
    h = c.post("/pdfs", files={"file": ("a.pdf", pdf, "application/pdf")}, data={"client_id": "papermeister-c2a80813"}).json()["file_hash"]
    pages = [{"page": i, "markdown": f"<div data-bbox='100 100 900 800' data-label='Text'><p>page {i}</p></div>"} for i in range(30)]
    digest = hashlib.sha256(json.dumps(pages, sort_keys=True).encode()).hexdigest()
    r = c.post("/figures/workspace", json={"client_id": "papermeister-c2a80813", "file_hash": h, "ocr_digest": digest, "pages": pages})
    ok(r.status_code == 201, f"workspace {r.text}")
    ok(c.head(f"/figures/workspace/{h}/{digest}").status_code == 200, "workspace HEAD")
    common = {"client_id": "papermeister-c2a80813", "file_hash": h, "ocr_digest": digest,
              "options": {"model": "gpt-6-astra", "effort": "high", "dpi": 216}, "force": False}

    # §4 link — one item per paper, locked figure carries caption+entries, hints block
    link_item = {"key": f"{h[:12]}@{digest[:12]}@{PROMPTS['link']['version']}", "page_count": 30,
                 "figures": [
                     {"figure_id": "984", "page": 14, "bbox_page_1000": [71, 125, 930, 880], "assembly": "plate_page_union",
                      "page_kind": "plate", "name_hint": "Plate II", "plate": 2, "plate_inferred": False,
                      "caption_hint": "", "label_hints": [], "reasons": [], "locked": False},
                     {"figure_id": "985", "page": 3, "bbox_page_1000": [100, 100, 900, 500], "assembly": "single", "page_kind": "body",
                      "name_hint": "Fig. 4", "caption_hint": "Fig. 4. …", "locked": True,
                      "caption": "Fig. 4. A person wrote this.", "entries": [{"label": "a", "description": "…"}]}],
                 "hints": {"plate_pages": [12, 40, 41], "explanation_pages": [12], "caption_pages": [3, 7]}}
    r = c.post("/figures/link", json={**common, "items": [link_item], "prompt": PROMPTS["link"]})
    ok(r.status_code == 202 and r.json()["queued"] == 1, f"link submit {r.text}")
    link_job = r.json()["job_id"]

    # §5 panels — item.dpi alongside options.dpi, piece boxes, label hints
    panel_item = {"key": "984@abc123", "page": 14, "bbox_page_1000": [71, 125, 930, 880],
                  "caption": "PLATE II. …", "entries": [{"label": "1", "description": "…"}],
                  "piece_boxes_figure_1000": [[0, 0, 475, 543]], "label_hints": [], "dpi": 216}
    r = c.post("/figures/panels", json={**common, "items": [panel_item], "prompt": PROMPTS["panels"]})
    ok(r.status_code == 202, f"panels submit {r.text}")

    # §6 detect — page-level item with hint_boxes + figure_keys + figures[] + reasons + hints; and an empty-box page doubt
    detect_items = [
        {"key": f"{h[:12]}|27|{digest[:12]}|{PROMPTS['detect']['version']}", "page": 27,
         "hint_boxes": [[48, 70, 282, 188], [294, 70, 527, 188]], "figure_keys": ["1201", "1202"],
         "reasons": ["unmarked_plate_page"],
         "figures": [{"figure_id": "1201", "bbox_page_1000": [48, 70, 282, 188], "assembly": "single", "name_hint": "",
                      "caption_hint": "", "reasons": ["unmarked_plate_page"]},
                     {"figure_id": "1202", "bbox_page_1000": [294, 70, 527, 188], "assembly": "single", "name_hint": "",
                      "caption_hint": "", "reasons": ["unmarked_plate_page"]}],
         "hints": {"plate_pages": [26, 30], "explanation_pages": [26]}},
        {"key": f"{h[:12]}|5|{digest[:12]}|{PROMPTS['detect']['version']}", "page": 5,
         "hint_boxes": [], "figure_keys": [], "reasons": ["plate_without_pictures"], "figures": [], "hints": {}},
    ]
    r = c.post("/figures/detect", json={**common, "items": detect_items, "prompt": PROMPTS["detect"]})
    ok(r.status_code == 202 and r.json()["queued"] == 2, f"detect submit {r.text}")

    # worker side: claim each and check what it would render
    seen = {}
    for _ in range(4):
        it = c.post("/internal/figures/claim", json={"worker_id": "contract"}, headers=W).json()["item"]
        ok(it is not None, "claim")
        seen[it["kind"] + ":" + it["key"]] = it
        ok(it["prompt"]["version"] == PROMPTS[it["kind"]]["version"] and it["options"]["dpi"] == 216, "claim carries prompt+options")
        if it["kind"] == "detect":
            boxes = hint_boxes_of(it["request"])
            ok(len(boxes) == len(it["request"]["hint_boxes"]), "hint boxes preserved (none full-page)")
        c.post(f"/internal/figures/items/{it['item_id']}/result", headers=W,
               json={"status": "done", "elapsed_s": 1, "model": "gpt-6-astra",
                     "result": {"link": LINK_REPLY, "panels": PANELS_REPLY, "detect": DETECT_REPLY}[it["kind"]]})
    ok(len(seen) == 4, "all four items claimed")
    j = c.get(f"/figures/link/{link_job}").json()
    ok(j["status"] == "done" and j["items"][0]["result"]["figures"][0]["figure_id"] == "984", "link result round-trips")
    # dedup on resubmission with identical prompt version
    r = c.post("/figures/link", json={**common, "items": [link_item], "prompt": PROMPTS["link"]})
    ok(r.json()["cached"] == 1, "same request → cached")
    # a prompt edit changes version → not cached
    p2 = dict(PROMPTS["link"]); p2["version"] = "link-v1-edited"; p2["instructions"] += "\n(edited)"
    r = c.post("/figures/link", json={**common, "items": [link_item], "prompt": p2})
    ok(r.json()["cached"] == 0, "edited prompt → new call")

print(f"OK — {checks} checks passed (spec v2 contract, prompts from {PM})")
