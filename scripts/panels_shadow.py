#!/usr/bin/env python3
"""panels_shadow — run the figsplit ONNX model next to every Astra panels call, for later comparison.

Watches the figures worker's panels workspaces (FIGURES_WS_DIR/<hash>/panels/<item>/run/a<N>/).
When a codex attempt has finished (run.json written), it runs the figsplit fp16 ONNX model on the
same figure.png and stores, per attempt, under SHADOW_DIR/<YYYYMMDD>/<item_id>/a<N>/:

    figure.png          the exact image Astra got (copy; the workspace is swept after 30 days)
    input.json          the INPUT JSON from Astra's prompt (request, caption, entries, image geometry)
    astra_response.json Astra's answer (absent if the attempt failed)
    astra_run.json      the worker's run record (status, elapsed, usage)
    onnx.json           figsplit output, two ways: no hint, and with the caption-entry labels as hint
                        (panels in figure permille, raw decoded text, seconds, model id)

Never touches the worker, the wrapper or its DB; read-only on the workspace. Shadow only: nothing
it produces is sent anywhere. Model is internal-use only (trained on copyrighted figures).

Runs with figsplit's venv (onnxruntime-gpu 1.31 + transformers for the Florence-2 processor):
    ~/projects/figsplit/model/.venv/bin/python scripts/panels_shadow.py [--since ISO|all] [--once]
By default only attempts finished after start are processed (--since all backfills the workspace).

Env:
    FIGURES_WS_DIR       default /srv/ocrserver/figure_ws
    SHADOW_DIR           default /mnt/disk1/ocrserver/panels_shadow
    FIGSPLIT_ONNX_DIR    default /mnt/disk1/figsplit-data/export/figsplit-onnx-fp16-B-f2l-1024L-v2
    FIGSPLIT_REPO        default ~/projects/figsplit (for figsplit_model.florence_onnx)
    SHADOW_GPU           default 1   (shares GPU 1 with the LLM; arena capped)
    SHADOW_GPU_MEM_GB    default 12
    SHADOW_POLL          default 30  seconds between scans
"""
import argparse
import datetime as dt
import glob
import json
import os
import shutil
import sys
import time

VERSION = "0.1.0"
WS_DIR = os.getenv("FIGURES_WS_DIR", "/srv/ocrserver/figure_ws")
SHADOW_DIR = os.getenv("SHADOW_DIR", "/mnt/disk1/ocrserver/panels_shadow")
ONNX_DIR = os.getenv("FIGSPLIT_ONNX_DIR", "/mnt/disk1/figsplit-data/export/figsplit-onnx-fp16-B-f2l-1024L-v2")
FIGSPLIT_REPO = os.path.expanduser(os.getenv("FIGSPLIT_REPO", "~/projects/figsplit"))
GPU = int(os.getenv("SHADOW_GPU", "1"))
GPU_MEM_GB = float(os.getenv("SHADOW_GPU_MEM_GB", "12"))
POLL = int(os.getenv("SHADOW_POLL", "30"))
INPUT_MARK = "=== INPUT (JSON) ==="


def log(msg: str) -> None:
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_predictor():
    sys.path.insert(0, os.path.join(FIGSPLIT_REPO, "model"))
    from figsplit_model import florence_onnx
    prov = [("CUDAExecutionProvider", {"device_id": GPU, "gpu_mem_limit": int(GPU_MEM_GB * (1 << 30))})]
    return florence_onnx.Predictor(ONNX_DIR, "fp16", providers=prov)


def run_model(pred, img_path: str, labels: list[str]) -> dict:
    """One figure, one prompt; keeps the raw text as well as the parsed panels."""
    from figsplit_model import florence, serialize
    from figsplit_model.florence_onnx import pixels, text_inputs
    t0 = time.monotonic()
    pair = {"img_path": img_path}
    ims, geo = florence.images(pred.processor, [pair])
    ids, mask = text_inputs(pred.processor, [serialize.prompt(labels or None)], pred.meta["image_token_id"])
    seqs = pred.generate(pixels(pred.processor, ims), ids, mask)
    raw = pred.processor.batch_decode(seqs, skip_special_tokens=False)[0]
    text = raw.replace("</s>", "").replace("<s>", "").replace("<pad>", "")
    panels = serialize.decode(text)
    if geo[0] is not None:
        panels = [{**q, "bbox_figure_1000": florence.from_square(q["bbox_figure_1000"], geo[0])} for q in panels]
    return {"prompt": serialize.prompt(labels or None), "panels": panels, "text": text,
            "tokens": len(seqs[0]), "elapsed_s": round(time.monotonic() - t0, 3)}


def read_input(prompt_path: str) -> dict | None:
    try:
        s = open(prompt_path, encoding="utf-8").read()
        return json.loads(s[s.index(INPUT_MARK) + len(INPUT_MARK):])
    except (OSError, ValueError):
        return None


def pending(since: float | None):
    """Finished attempts (run.json present) without a shadow record yet."""
    for run_json in glob.glob(os.path.join(WS_DIR, "*", "panels", "*", "run", "a*", "run.json")):
        run_dir = os.path.dirname(run_json)
        try:
            mt = os.path.getmtime(run_json)
        except OSError:
            continue
        if since is not None and mt < since:
            continue
        item_dir = os.path.dirname(os.path.dirname(run_dir))
        item_id, attempt = os.path.basename(item_dir), os.path.basename(run_dir)
        day = dt.datetime.fromtimestamp(mt, dt.timezone.utc).strftime("%Y%m%d")
        out = os.path.join(SHADOW_DIR, day, item_id, attempt)
        if not os.path.exists(os.path.join(out, "onnx.json")):
            yield mt, item_dir, run_dir, out


def shadow(pred, item_dir: str, run_dir: str, out: str) -> str:
    fig = os.path.join(item_dir, "figure.png")
    inp = read_input(os.path.join(run_dir, "prompt.txt"))
    if not os.path.exists(fig) or inp is None:
        return "skip (no figure.png or prompt)"
    os.makedirs(out, exist_ok=True)
    shutil.copy2(fig, os.path.join(out, "figure.png"))
    json.dump(inp, open(os.path.join(out, "input.json"), "w"), ensure_ascii=False, indent=1)
    for src, dst in (("response.json", "astra_response.json"), ("run.json", "astra_run.json")):
        if os.path.exists(os.path.join(run_dir, src)):
            shutil.copy2(os.path.join(run_dir, src), os.path.join(out, dst))
    item = inp.get("item") or {}
    labels = [str(e.get("label") or "").strip() for e in item.get("entries") or []]
    labels = [s for s in labels if s]
    res = {"model": "figsplit B-f2l-1024L-v2 fp16 onnx", "onnx_dir": ONNX_DIR, "shadow_version": VERSION,
           "item_id": os.path.basename(item_dir), "file_hash": os.path.basename(os.path.dirname(os.path.dirname(item_dir))),
           "attempt": os.path.basename(run_dir), "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "no_hint": run_model(pred, fig, [])}
    if labels:
        res["hint"] = run_model(pred, fig, labels)
    tmp = os.path.join(out, "onnx.json.tmp")
    json.dump(res, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(out, "onnx.json"))  # written last: its presence marks the attempt done
    n = len(res["no_hint"]["panels"])
    return f"{n} panels {res['no_hint']['elapsed_s']}s" + (f", hint {len(res['hint']['panels'])}" if labels else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO time or 'all' (default: process start)")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    since = None if a.since == "all" else (dt.datetime.fromisoformat(a.since).timestamp() if a.since else time.time())
    os.makedirs(SHADOW_DIR, exist_ok=True)
    log(f"panels_shadow {VERSION} ws={WS_DIR} out={SHADOW_DIR} gpu={GPU} since={a.since or 'start'}")
    pred = load_predictor()
    log("model loaded")
    while True:
        for _, item_dir, run_dir, out in sorted(pending(since)):
            try:
                log(f"{os.path.basename(item_dir)}/{os.path.basename(run_dir)}: {shadow(pred, item_dir, run_dir, out)}")
            except Exception as e:  # one bad figure must not stop the shadow
                log(f"{os.path.basename(item_dir)}/{os.path.basename(run_dir)}: error {type(e).__name__}: {e}")
                os.makedirs(out, exist_ok=True)
                json.dump({"error": f"{type(e).__name__}: {e}"}, open(os.path.join(out, "onnx.json"), "w"))
        if a.once:
            return 0
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
