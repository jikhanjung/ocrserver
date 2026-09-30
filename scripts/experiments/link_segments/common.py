"""Shared paths for the link segment-format A/B experiment (docs/FIGURE_LINK_STALLS.md §6).

Env:
    AB_VERSION   format version, picks seg_format_<v>.md and the output dir   (default v3)
    AB_OUT       where runs are written, outside the repo                     (default ~/.cache/ocrserver-ab)
    AB_ITEMS     JSON list of original run dirs (…/items/<id>/run/aN)          (default items_20260929.json)
    AB_PARALLEL  concurrent codex sessions                                     (default 3)
"""
import os
HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = os.getenv('AB_VERSION', 'v3')
OUT = os.path.join(os.path.expanduser(os.getenv('AB_OUT', '~/.cache/ocrserver-ab')), VERSION)
ITEMS = os.getenv('AB_ITEMS', os.path.join(HERE, 'items_20260929.json'))
FORMAT = os.path.join(HERE, f'seg_format_{VERSION}.md')
PARALLEL = int(os.getenv('AB_PARALLEL', '3'))
WORKER_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)))  # scripts/
