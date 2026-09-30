"""Compare A/B results with the production answers (A) and print one JSON line per item + totals.

    <python with tiktoken> compare.py        # AB_VERSION picks the run dir and the expander
A figure where both sides have <= 1 unlabelled entry counts as the same; specimen numbers
are compared by their digits; descriptions by word-sequence similarity.
"""
import json, os, sys, glob, re, difflib, statistics as st
from common import OUT, VERSION, WORKER_DIR
os.environ.setdefault('FIGURES_WORKER_TOKEN', 'x')
sys.path.insert(0, WORKER_DIR)
sys.path.append(os.path.expanduser('~/venv/ocrserver/lib/python3.14/site-packages'))  # fitz for figures_worker
import figures_worker as fw
if VERSION == 'v1':
    from expand_v1 import expand, parse
else:
    from expand import expand, parse
from tok import enc
E = enc(); T = lambda s: len(E.encode(s))
words = lambda s: re.findall(r'\w+', (s or '').lower())
sim = lambda a, b: difflib.SequenceMatcher(None, words(a), words(b), autojunk=False).ratio() if (a or b) else 1.0
rows = []
for rf in sorted(glob.glob(os.path.join(OUT, '*', 'result.json'))):
    B = json.load(open(rf)); run_a = B['orig_run']
    A_resp = json.load(open(run_a + '/response.json')); A_run = json.load(open(run_a + '/run.json'))
    schema = json.load(open(run_a + '/schema.json'))
    iid = os.path.basename(os.path.dirname(rf))
    r = {'item': iid, 'A_el': A_run['elapsed'], 'A_out': A_run['usage']['output_tokens'], 'A_json': T(open(run_a + '/response.json').read()),
         'B_el': B['elapsed'], 'B_out': (B.get('usage') or {}).get('output_tokens'), 'B_stalled': B.get('stalled'), 'B_recon': B.get('reconnects'),
         'B_ok': isinstance(B.get('response'), dict)}
    if not r['B_ok']:
        rows.append(r); continue
    rawB = open(os.path.join(os.path.dirname(rf), 'response.json')).read(); r['B_json'] = T(rawB)
    X, warns = expand(B['response']); r['warn'] = len(warns); r['schema_err'] = fw.schema_errors(X, schema)
    segs = [s for f in B['response']['figures'] for s in parse(f.get('segs', ''))]
    r['n_d'] = sum(1 for s in segs if s.get('d')); r['n_t'] = sum(1 for s in segs if s['k'] == 't'); r['n_h'] = sum(1 for s in segs if s['k'].startswith('h'))
    FA = {f['figure_id']: f for f in A_resp['figures']}; FB = {f['figure_id']: f for f in X['figures']}
    r['figs'] = (len(FA), len(FB), len(set(FA) & set(FB)))
    r['skipped'] = (sorted(s['figure_id'] for s in A_resp.get('skipped', [])), sorted(s['figure_id'] for s in X.get('skipped', [])))
    ea = eb = lab_same = spec_same = 0; ds = []; cs = []; name_same = 0
    for fid in set(FA) & set(FB):
        a, b = FA[fid], FB[fid]
        ea += len(a['entries']); eb += len(b['entries']); name_same += a.get('name') == b.get('name')
        cs.append(sim(a['caption'], b['caption']))
        la = [e['label'] for e in a['entries']]; lb = [e['label'] for e in b['entries']]
        lab_same += (la == lb) or (len(la) <= 1 and len(lb) <= 1 and set(la + lb) <= {'1', ''})
        bm = {e['label']: e for e in b['entries']}
        for e in a['entries']:
            o = bm.get(e['label'])
            if o:
                ds.append(sim(e['description'], o['description']))
                dig = lambda s: re.findall(r'\d+', s or '')
                spec_same += dig(e['specimen_number']) == dig(o['specimen_number'])
    common = len(set(FA) & set(FB))
    r.update({'entries': (ea, eb), 'labels_same_figs': f'{lab_same}/{common}', 'names_same': f'{name_same}/{common}',
              'spec_same': f'{spec_same}/{len(ds)}', 'desc_sim': round(st.mean(ds), 3) if ds else None,
              'desc_low': sum(1 for x in ds if x < 0.8), 'cap_sim': round(st.mean(cs), 3) if cs else None})
    rows.append(r)
    json.dump(X, open(os.path.join(os.path.dirname(rf), 'expanded.json'), 'w'), ensure_ascii=False, indent=1)
for r in rows: print(json.dumps(r, ensure_ascii=False))
ok = [r for r in rows if r['B_ok']]
if ok:
    print(f"\nSUM n={len(ok)} A_el {sum(r['A_el'] for r in ok):.0f}s  B_el {sum(r['B_el'] for r in ok):.0f}s  ({sum(r['B_el'] for r in ok)/sum(r['A_el'] for r in ok):.0%})"
          f" | A_out {sum(r['A_out'] for r in ok):,}  B_out {sum(r['B_out'] for r in ok):,} ({sum(r['B_out'] for r in ok)/sum(r['A_out'] for r in ok):.0%})"
          f" | A_json {sum(r['A_json'] for r in ok):,}  B_json {sum(r['B_json'] for r in ok):,} ({sum(r['B_json'] for r in ok)/sum(r['A_json'] for r in ok):.0%})")
