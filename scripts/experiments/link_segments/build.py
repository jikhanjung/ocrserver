"""Build the compact-format prompt/schema for an A/B item from its original run:
the original link instructions + seg_format_<v>.md, and the original schema with
`caption`/`entries` replaced by one `segs` string."""
import json, os
from common import FORMAT

def compact_schema(orig: dict) -> dict:
    s = json.loads(json.dumps(orig))
    fig = s['properties']['figures']['items']
    for k in ('caption', 'entries'):
        fig['properties'].pop(k); fig['required'].remove(k)
    fig['properties']['segs'] = {'type': 'string', 'description': 'caption pieces, one per line: <kind> :: <text>'}
    fig['required'].append('segs')
    return s

def build(run_dir: str) -> tuple[str, dict]:
    fmt = open(FORMAT).read()
    prompt = open(os.path.join(run_dir, 'prompt.txt')).read()
    head, sep, inp = prompt.partition('=== INPUT (JSON) ===')
    head = head.rstrip().removesuffix('Return only JSON conforming to the schema.').rstrip()
    new = head + '\n\n' + fmt.rstrip() + '\n\nReturn only JSON conforming to the schema.\n\n' + sep + inp
    return new, compact_schema(json.load(open(os.path.join(run_dir, 'schema.json'))))
