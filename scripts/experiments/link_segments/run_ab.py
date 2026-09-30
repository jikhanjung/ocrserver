"""Run the compact-format variant of each item with the production worker's own
run_codex (same codex command, idle watchdog, session cap). Skips items that
already have a result, so it can be re-run after an interruption.

    ~/venv/ocrserver/bin/python3 run_ab.py          # AB_VERSION=v3 by default
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
os.environ.setdefault('FIGURES_WORKER_TOKEN', 'ab-test')
os.environ.setdefault('FIGURES_IDLE_TIMEOUT', '1800')
os.environ['FIGURES_WORKER_ID'] = 'ab-test'
from common import OUT, ITEMS, PARALLEL, WORKER_DIR, VERSION
sys.path.insert(0, WORKER_DIR)
import figures_worker as fw
from build import build

def one(run_dir: str) -> None:
    item = run_dir.rsplit('/run/', 1)[0]; root = item.rsplit('/items/', 1)[0]; iid = item.split('/')[-1][:8]
    out = os.path.join(OUT, iid)
    if os.path.exists(os.path.join(out, 'result.json')):
        return
    if not os.path.isdir(root):
        print(f'skip {iid}: workspace gone (TTL)', flush=True); return
    prompt, schema = build(run_dir)
    print(f'[{time.strftime("%H:%M:%S")}] start {iid}', flush=True)
    info = fw.run_codex(root, prompt, schema, [], 'gpt-6-astra', 'high', 3600, out)
    keep = {k: info.get(k) for k in ('elapsed', 'usage', 'code', 'turn_ok', 'turn_err', 'reconnects', 'timed_out', 'stalled', 'response')}
    keep['orig_run'] = run_dir
    json.dump(keep, open(os.path.join(out, 'result.json'), 'w'), ensure_ascii=False)
    print(f'[{time.strftime("%H:%M:%S")}] done  {iid} el={info.get("elapsed")} out={(info.get("usage") or {}).get("output_tokens")} '
          f'stalled={info.get("stalled")} ok={isinstance(info.get("response"), dict)}', flush=True)

if __name__ == '__main__':
    print(f'{VERSION} → {OUT} (pid {os.getpid()})', flush=True)
    with ThreadPoolExecutor(PARALLEL) as ex:
        list(ex.map(one, json.load(open(ITEMS))))
    print('ALL DONE', flush=True)
