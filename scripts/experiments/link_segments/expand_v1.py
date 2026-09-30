"""Compact segment answer → the original link result schema."""
import re
LINE = re.compile(r'^\s*(x|t|h1|h2|e\s+(?P<labels>[^|:]+?))\s*(?P<opts>(\|\s*[spd]=.*?)*)\s*::\s?(?P<text>.*)$')
OPT = re.compile(r'\|\s*([spd])=(.*?)(?=\s*\|\s*[spd]=|$)')
def parse(segs: str):
    out = []
    for ln in segs.split('\n'):
        if not ln.strip():
            continue
        m = LINE.match(ln)
        if not m:
            out.append({'k': 'x', 't': ln.strip(), 'bad': True}); continue
        kind = m.group(1).split()[0]
        opts = {k: v.strip() for k, v in OPT.findall(m.group('opts') or '')}
        out.append({'k': kind, 't': m.group('text').strip(),
                    'l': [x.strip() for x in (m.group('labels') or '').split(',') if x.strip()], **opts})
    return out
def expand_figure(f: dict) -> tuple[dict, list]:
    segs = parse(f.get('segs', ''))
    caption = '\n'.join(s['t'] for s in segs)
    entries, group, h1, h2, warn = [], [], '', '', []
    for s in segs:
        if s.get('bad'): warn.append('unparsed line: ' + s['t'][:60])
        k = s['k']
        if k == 'h1': h1, h2, group = s['t'], '', []
        elif k == 'h2': h2 = s['t']
        elif k == 't':
            for e in group:
                if not e['_d']: e['description'] = (e['description'] + ' ' + s['t']).strip()
        elif k == 'e':
            sp = [x.strip() for x in s.get('s', '').split(';')] if s.get('s') else []
            pp = [x.strip() for x in s.get('p', '').split(';')] if s.get('p') else []
            for i, lab in enumerate(s['l']):
                d = s.get('d')
                e = {'label': lab, 'printed_label': pp[i] if i < len(pp) and pp[i] else lab,
                     'description': d if d else ' '.join(x for x in (h1, h2, s['t']) if x),
                     'specimen_number': sp[i] if i < len(sp) else '', '_d': bool(d)}
                entries.append(e); group.append(e)
    for e in entries: e.pop('_d')
    full = {k: v for k, v in f.items() if k != 'segs'}
    full['caption'] = caption; full['entries'] = entries
    order = ['figure_id', 'name', 'caption', 'caption_source', 'caption_pages', 'continuation_of', 'entries']
    return {k: full.get(k) for k in order}, warn
def expand(resp: dict) -> tuple[dict, list]:
    figs, warns = [], []
    for f in resp.get('figures', []):
        ff, w = expand_figure(f); figs.append(ff); warns += [f"{f.get('figure_id')}: {x}" for x in w]
    return {'figures': figs, 'skipped': resp.get('skipped', []), 'pages_consulted': resp.get('pages_consulted', []), 'notes': resp.get('notes', [])}, warns
