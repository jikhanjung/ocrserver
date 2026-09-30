"""v2 compact segment answer → the original link result schema."""
import re
LINE = re.compile(r'^\s*(?P<kind>x|h1|h2|t|e)(?:\s+(?P<labels>[^|:]+?))?\s*(?P<opts>(\|\s*[Lsd]=.*?)*)\s*::\s?(?P<text>.*)$')
OPT = re.compile(r'\|\s*([Lsd])=(.*?)(?=\s*\|\s*[Lsd]=|$)')
trim = lambda s: re.sub(r'[\s,;:]+$', '', s.strip())
close = lambda s: trim(s) + ('' if trim(s).endswith(('.', '!', '?', ')')) or not trim(s) else '.')
def parse(segs):
    out = []
    for ln in segs.split('\n'):
        if not ln.strip(): continue
        m = LINE.match(ln)
        if not m: out.append({'k': 'x', 't': ln.strip(), 'bad': True, 'l': []}); continue
        opts = {k: v.strip() for k, v in OPT.findall(m.group('opts') or '')}
        out.append({'k': m.group('kind'), 't': m.group('text').strip(),
                    'l': [x.strip() for x in (m.group('labels') or '').split(',') if x.strip()], **opts})
    return out
def expand_figure(f):
    segs = parse(f.get('segs', ''))
    caption = '\n'.join(((s['L'] + ' ') if s.get('L') else '') + s['t'] for s in segs)
    entries, group, h1, h2, warn = [], [], '', '', []
    for s in segs:
        if s.get('bad'): warn.append('unparsed: ' + s['t'][:60])
        k = s['k']
        if k == 'h1': h1, h2, group = s['t'], '', []
        elif k == 'h2': h2 = s['t']
        elif k == 't':
            tgt = [e for e in (entries if s['l'] else group) if (not s['l'] or e['label'] in s['l'])]
            if s['l'] and len(tgt) < len(s['l']): warn.append(f"t labels not found: {s['l']}")
            for e in tgt:
                if not e['_d']: e['_tail'].append(s['t'])
        elif k == 'e':
            sp = [x.strip() for x in s.get('s', '').split(';')] if s.get('s') else []
            for i, lab in enumerate(s['l']):
                e = {'label': lab, 'printed_label': s['L'] if (s.get('L') and len(s['l']) == 1) else lab,
                     '_body': [x for x in (h1, h2, s['t']) if x], '_d': s.get('d'), '_tail': [],
                     'specimen_number': sp[i] if i < len(sp) else ''}
                entries.append(e); group.append(e)
    out = []
    for e in entries:
        if e['_d']:
            desc = e['_d']
        else:
            # headings and the entry's own text run on as printed ("Specimen X in" + "lateral view");
            # only the end of that sentence and each trailing remark are closed off.
            parts = [' '.join(x.strip() for x in e['_body'])] + e['_tail']
            desc = ' '.join(close(p) for p in parts if p.strip())
        out.append({'label': e['label'], 'printed_label': e['printed_label'], 'description': desc.strip(), 'specimen_number': e['specimen_number']})
    full = {k: v for k, v in f.items() if k != 'segs'}
    full['caption'] = caption; full['entries'] = out
    order = ['figure_id', 'name', 'caption', 'caption_source', 'caption_pages', 'continuation_of', 'entries']
    return {k: full.get(k) for k in order}, warn
def expand(resp):
    figs, warns = [], []
    for f in resp.get('figures', []):
        ff, w = expand_figure(f); figs.append(ff); warns += [f"{f.get('figure_id')}: {x}" for x in w]
    return {'figures': figs, 'skipped': resp.get('skipped', []), 'pages_consulted': resp.get('pages_consulted', []), 'notes': resp.get('notes', [])}, warns
