"""도판 패널 분할 공유 로직 (P37, devlog 20260909_P37).

figure 이미지의 정의(렌더)·소스 키·분할 후보 선정·결과 파일 위치·DB 반영이 전부 여기 한 곳이다.
호스트 스크립트(`scripts/astra_subfigures.py`)는 이 모듈로 후보를 고르고 렌더해 Codex CLI 를
부른 뒤 파일만 쓰고, 컨테이너 명령(`sync_reference_subfigures`)이 같은 모듈로 그 파일을
`ReferenceSubfigure` 행으로 승격한다. 둘이 다른 정의를 쓰면 결과가 어느 이미지의 것인지 어긋난다.

결과 파일: `MEDIA_ROOT/references/subfigures/<figure_pk>/<source_key>.a<n>.json`
  - `source_key` = page·bbox·dpi·PDF 크기의 해시. 사용자가 bbox 를 고치면 키가 바뀌어
    옛 결과는 자동으로 stale 이 된다(파일은 감사용으로 남는다).
  - `a<n>` = 시도 번호. `astra_cli_bbox.extract` 가 기존 출력을 덮어쓰지 않으므로 시도마다 새 이름.
    실패한 시도는 `.run/` 디렉터리만 남기고 `.json` 은 없다 → `.json` 존재 = 완료.
  - `<source_key>.a<n>.meta.json` = figure pk·dpi·렌더 크기 사이드카(실패해도 남는다).
"""
import hashlib
import io
import json
import math
import os
import re
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

DEFAULT_DPI = 216          # 9/8 실험 입력(export_figex_batch)과 동일 — 결과가 그대로 재현된다
MAX_ATTEMPTS = 3
CONFIDENCES = ('high', 'medium', 'low')
# 분할 가치 순. 지도는 기본 제외(좌표맞춤엔 원본이 필요하고 패널 분할 가치가 낮다).
KIND_PRIORITY = {'photo': 0, 'unclassified': 1, 'other': 2, 'chart': 3, 'column': 4, 'map': 5}
_KEY_RE = re.compile(r'^[0-9a-f]{16}$')


# ─── 이미지 정의 ──────────────────────────────────────────────────────────

def figure_bbox(fig):
    """검증된 permille bbox [x0, y0, x1, y1]. 없거나 깨졌으면 ValueError(사유 코드)."""
    raw = (fig.bbox or {}).get('bbox')
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError('missing_figure_bbox')
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1000 for v in raw):
        raise ValueError('invalid_figure_bbox')
    x0, y0, x1, y1 = raw
    if x0 >= x1 or y0 >= y1:
        raise ValueError('empty_figure_bbox')
    return raw


def figure_pdf_path(fig):
    """figure 가 속한 논문의 PDF 경로. 없으면 ValueError('missing_pdf')."""
    ref = fig.reference
    if not ref.data:
        raise ValueError('missing_pdf')
    path = Path(ref.data.path)
    if not path.is_file():
        raise ValueError('missing_pdf')
    return path


def figure_source_key(fig, dpi=DEFAULT_DPI):
    """이 figure 이미지의 정체 = page · bbox · dpi · PDF 크기. `updated_at` 은 kind 확정만 해도
    바뀌므로 쓰지 않는다. 16 hex."""
    raw = figure_bbox(fig)
    size = figure_pdf_path(fig).stat().st_size
    payload = f'{fig.page_no}|{json.dumps(raw)}|{dpi}|{size}'
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def figure_metadata(fig):
    """캡션·subfigure 목록. DB 편집을 우선하고, 없으면 sync 가 쓰는 것과 같은 사이드카로 폴백."""
    caption, panels = fig.caption.strip(), fig.subfigures or []
    provenance = {'caption': 'ReferenceFigure.caption', 'subfigures': 'ReferenceFigure.subfigures'}
    if caption and panels:
        return caption, panels, provenance
    for suffix in ('.extract.claude.json', '.extract.json'):
        try:
            path = Path(str(fig.reference.data.path) + suffix)
        except ValueError:
            break
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        matches = [entry for entry in data.get('figures', [])
                   if entry.get('page') == fig.page_no - 1
                   and str(entry.get('number', '')).strip() == fig.figure_number.strip()]
        if len(matches) != 1:
            continue
        entry = matches[0]
        if not caption and entry.get('caption'):
            caption = entry['caption']
            provenance['caption'] = suffix
        if not panels and entry.get('subfigures'):
            panels = entry['subfigures']
            provenance['subfigures'] = suffix
        if caption and panels:
            break
    return caption, panels, provenance


def render_figure(fig, path, dpi=DEFAULT_DPI):
    """figure bbox 영역을 PDF 에서 PNG 로 렌더. 반환: 크기·clip 정보."""
    import fitz
    raw = figure_bbox(fig)
    x0, y0, x1, y1 = raw
    with fitz.open(str(figure_pdf_path(fig))) as pdf:
        if not 1 <= fig.page_no <= len(pdf):
            raise ValueError('invalid_page_number')
        page = pdf[fig.page_no - 1]
        clip = fitz.Rect(x0 / 1000 * page.rect.width, y0 / 1000 * page.rect.height,
                         x1 / 1000 * page.rect.width, y1 / 1000 * page.rect.height)
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=clip,
                              colorspace=fitz.csRGB, alpha=False)
        pix.save(str(path))
        return {'width': pix.width, 'height': pix.height,
                'pdf_clip_xyxy_points': list(clip), 'render_origin_px': [pix.x, pix.y]}


def crop_panel(fig, panel, dpi=DEFAULT_DPI):
    """패널 하나를 PNG bytes 로. 렌더 크기가 결과의 image_width/height 와 다르면(다른 dpi·PDF 교체)
    bbox 를 비례 변환한다."""
    import fitz
    from PIL import Image
    raw = figure_bbox(fig)
    x0, y0, x1, y1 = raw
    with fitz.open(str(figure_pdf_path(fig))) as pdf:
        page = pdf[fig.page_no - 1]
        clip = fitz.Rect(x0 / 1000 * page.rect.width, y0 / 1000 * page.rect.height,
                         x1 / 1000 * page.rect.width, y1 / 1000 * page.rect.height)
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=clip,
                              colorspace=fitz.csRGB, alpha=False)
        image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
    meta = fig.panel_sync or {}
    sw, sh = meta.get('image_width') or pix.width, meta.get('image_height') or pix.height
    bx0, by0, bx1, by1 = panel.bbox
    box = (max(0, math.floor(bx0 * pix.width / sw)), max(0, math.floor(by0 * pix.height / sh)),
           min(pix.width, math.ceil(bx1 * pix.width / sw)), min(pix.height, math.ceil(by1 * pix.height / sh)))
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError('empty_panel_box')
    buffer = io.BytesIO()
    image.crop(box).save(buffer, format='PNG')
    return buffer.getvalue()


# ─── 후보 선정 ────────────────────────────────────────────────────────────

def subfigure_count(fig):
    panels = fig.subfigures
    return len(panels) if isinstance(panels, list) else 0


def candidate_figures(*, min_subfigures=2, include_maps=False, reference_ids=None, figure_ids=None):
    """분할 대상 = dismissed 아님 · subfigure 캡션 ≥ min · bbox 있음 · PDF 있음.

    정렬: **논문 연도(오름차순)** → reference pk → page → kind 우선순위(photo 먼저) → pk.
    리스트(정렬 위해 실체화).

    연도가 앞서는 이유는 사람이 큐를 논문 단위로 따라 읽기 때문이다 — 종전처럼 kind 가
    맨 앞이면 2020년대 사진 도판이 1900년대 논문보다 먼저 나와 순서가 튄다. kind 는
    같은 논문·같은 페이지 안에서만 순서를 정한다."""
    from kprdb.models import ReferenceFigure
    qs = (ReferenceFigure.objects.filter(dismissed=False)
          .select_related('reference')
          # panel_sync 는 일부러 안 읽는다 — 호스트 레인은 그 컬럼을 안 쓰고, 덕분에 마이그레이션
          # 0046 이 운영에 적용되기 전에도(구 스키마) 후보 선정·분할이 돈다.
          # reference__year 를 빠뜨리면 정렬 키가 figure 마다 쿼리를 한 번씩 더 낸다(후보 수천 건).
          .only('pk', 'page_no', 'figure_number', 'caption', 'subfigures', 'bbox', 'figure_kind',
                'reference__id', 'reference__data', 'reference__year'))
    if reference_ids:
        qs = qs.filter(reference_id__in=list(reference_ids))
    if figure_ids:
        qs = qs.filter(pk__in=list(figure_ids))
    if not include_maps:
        qs = qs.exclude(figure_kind='map')
    picked = []
    for fig in qs.iterator():
        if subfigure_count(fig) < min_subfigures:
            continue
        try:
            figure_bbox(fig)
            figure_pdf_path(fig)
        except ValueError:
            continue
        picked.append(fig)
    picked.sort(key=lambda f: (reference_year_key(f), f.reference_id, f.page_no,
                               KIND_PRIORITY.get(f.figure_kind, 9), f.pk))
    return picked


def reference_year_key(fig):
    """정렬용 연도. `Reference.year` 는 CharField 라 빈값·비숫자가 들어올 수 있다.

    알 수 없는 연도는 **뒤로** 보낸다 — 연대순으로 훑는 사람 앞에 정체불명이 먼저 오면
    순서 자체를 못 믿게 된다.
    """
    raw = (getattr(fig.reference, 'year', '') or '').strip()
    return int(raw) if raw.isdigit() else 9999


# ─── 결과 파일 ────────────────────────────────────────────────────────────

def results_root():
    return Path(settings.MEDIA_ROOT) / 'references' / 'subfigures'


def result_dir(fig):
    return results_root() / str(fig.pk)


def _attempt_number(path):
    m = re.search(r'\.a(\d+)\.json$', path.name)
    return int(m.group(1)) if m else 0


def completed_results(fig, key):
    """현재 소스 키의 완료 파일들(시도 순). `.json` 이 있다는 것 자체가 완료다."""
    folder = result_dir(fig)
    if not folder.is_dir():
        return []
    files = [p for p in folder.glob(f'{key}.a*.json') if not p.name.endswith('.meta.json')]
    return sorted(files, key=_attempt_number)


def find_result(fig, key):
    files = completed_results(fig, key)
    return files[-1] if files else None


def figure_ids_with_results():
    """결과 파일이 하나라도 있는 figure pk 집합. 통계 화면용(figure 당 stat 을 피한다).

    **소스 키는 안 본다** — 키를 맞추려면 figure 마다 PDF 를 열어야 해서 목록 화면에서
    못 쓴다. 그래서 이 집합은 "분할을 돌렸다" 는 뜻이고, bbox 를 고쳐 결과가 낡은 경우도
    포함한다. 낡음 판정은 `sync_panels` 가 한다(거긴 키를 본다).
    """
    root = results_root()
    if not root.is_dir():
        return set()
    found = set()
    with os.scandir(root) as entries:
        for entry in entries:
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            try:
                with os.scandir(entry.path) as files:
                    if any(f.is_file() and f.name.endswith('.json')
                           and not f.name.endswith('.meta.json') for f in files):
                        found.add(int(entry.name))
            except OSError:
                continue
    return found


def attempt_count(fig, key):
    """시도 수 = `.run` 디렉터리 수(성공·실패 모두 남는다)."""
    folder = result_dir(fig)
    if not folder.is_dir():
        return 0
    return len([p for p in folder.glob(f'{key}.a*.json.run') if p.is_dir()])


def next_output_path(fig, key):
    return result_dir(fig) / f'{key}.a{attempt_count(fig, key) + 1}.json'


def meta_path(output_path):
    return output_path.with_name(output_path.name[:-len('.json')] + '.meta.json')


def load_result(path, subfigures):
    """호스트가 쓴 결과 JSON 을 검증해 반환. 승격 전 게이트 — 깨진 파일을 DB 에 넣지 않는다.
    caption_indices 가 현재 subfigures 범위를 벗어나면(캡션이 그새 바뀜) 그 인덱스만 버린다."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or not isinstance(data.get('panels'), list):
        raise ValueError('unexpected result schema')
    width, height = data.get('image_width'), data.get('image_height')
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
        raise ValueError('invalid image size')
    n = len(subfigures) if isinstance(subfigures, list) else 0
    panels = []
    for panel in data['panels']:
        bbox = panel.get('bbox')
        if not (isinstance(bbox, list) and len(bbox) == 4 and all(type(v) is int for v in bbox)):
            raise ValueError('panel bbox must be 4 ints')
        x0, y0, x1, y1 = bbox
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError('panel bbox outside image')
        if panel.get('confidence') not in CONFIDENCES or not isinstance(panel.get('label'), str):
            raise ValueError('invalid panel label/confidence')
        indices = [i for i in (panel.get('caption_indices') or []) if type(i) is int and 0 <= i < n]
        panels.append({'label': panel['label'], 'bbox': bbox,
                       'bbox_normalized': panel.get('bbox_normalized_1000'),
                       'caption_indices': indices, 'confidence': panel['confidence']})
    return data, panels


def _description(subfigures, indices):
    parts = []
    for i in indices:
        entry = subfigures[i]
        if isinstance(entry, dict):
            text = entry.get('description', entry.get('text', entry.get('caption', '')))
            if text:
                parts.append(str(text))
    return '\n'.join(parts)


def sync_panels(fig, path=None, *, force=False):
    """결과 파일 → ReferenceSubfigure 행. 반환 상태:
    unrenderable(bbox/PDF 없음) · no_result(현재 키의 완료 파일 없음) · unchanged · synced."""
    from kprdb.models import ReferenceSubfigure
    try:
        key = figure_source_key(fig)
    except ValueError:
        return 'unrenderable'
    path = Path(path) if path else find_result(fig, key)
    if path is None or not path.is_file():
        return 'no_result'
    if not path.name.startswith(key + '.'):
        return 'no_result'   # 다른 키(옛 bbox)의 결과는 stale — 현재 이미지의 것이 아니다
    current = fig.panel_sync or {}
    if not force and current.get('source_key') == key and current.get('result_file') == path.name:
        return 'unchanged'
    subfigures = fig.subfigures if isinstance(fig.subfigures, list) else []
    data, panels = load_result(path, subfigures)
    with transaction.atomic():
        ReferenceSubfigure.objects.filter(figure=fig).delete()
        ReferenceSubfigure.objects.bulk_create([
            ReferenceSubfigure(
                figure=fig, order=i + 1, label=p['label'][:50],
                description=_description(subfigures, p['caption_indices']),
                caption_index=p['caption_indices'][0] if p['caption_indices'] else None,
                caption_indices=p['caption_indices'], bbox=p['bbox'],
                bbox_normalized=p['bbox_normalized'], confidence=p['confidence'])
            for i, p in enumerate(panels)])
        fig.panel_sync = {
            'source_key': key, 'result_file': path.name,
            'model': data.get('model'), 'method': data.get('method'),
            'image_width': data['image_width'], 'image_height': data['image_height'],
            'is_compound': data.get('is_compound'), 'figure_kind': data.get('figure_kind'),
            'notes': data.get('notes') or [], 'elapsed_seconds': data.get('elapsed_seconds'),
            'panel_count': len(panels), 'synced_at': timezone.now().isoformat(),
        }
        fig.save(update_fields=['panel_sync', 'updated_at'])
    return 'synced'


def figures_with_results():
    """결과 디렉터리가 있는 figure pk 들 — sync 가 훑는 범위(22k 전수가 아니라 파일 있는 것만)."""
    root = results_root()
    if not root.is_dir():
        return []
    return sorted(int(p.name) for p in root.iterdir() if p.is_dir() and p.name.isdigit())
