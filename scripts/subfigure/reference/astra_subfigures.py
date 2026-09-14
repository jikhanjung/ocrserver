#!/usr/bin/env python3
"""
도판 패널 분할 — 기존 subfigure 캡션이 있는 figure 를 gpt-6-astra(Codex CLI, ChatGPT 로그인)로 분할 (P37).

claude_augment.py 와 같은 호스트 cron 레인이다: Codex CLI 는 컨테이너에 없고, 호스트는 **파일만
쓴다**. DB 는 읽기 전용(PRAGMA query_only)으로 열어 후보만 고른다. 결과 파일은 컨테이너의
`manage.py sync_reference_subfigures` 가 ReferenceSubfigure 행으로 승격한다(dual-writer 금지, devlog 074).

    figure(page·bbox) ─render(216dpi)→ PNG ─astra_cli_bbox.extract→ MEDIA_ROOT/references/subfigures/<fig>/<key>.a<n>.json

선행 조건: **캡션 매칭·분할이 먼저 끝나 있어야 한다.** 이 레인은 이미지와 **이미 쪼개진 캡션**을
보낼 뿐, 캡션을 만들거나 어느 도판의 것인지 찾지 않는다. 후보 조건 자체가 `subfigures` 항목
2개 이상이고(`figure_panels.candidate_figures`), 패널 설명은 분할이 아니라 **반영 시점에**
`subfigures` 에서 복사된다 → 일일 체인 순서 `도판 → 플레이트 설명 → 논문 단위 연결 → 패널`.
순서가 뒤집히면 패널은 잘려 있는데 설명이 비거나 옛 캡션이 붙는다.
자세히는 `docs/astra_cli_bbox.md` §선행 조건 · `docs/reference_pipeline.md` §11.

사용:
  python scripts/astra_subfigures.py --dry-run                 # 후보 수·순서만
  python scripts/astra_subfigures.py --limit 1                 # cron (10분 간격)
  python scripts/astra_subfigures.py --figure-ids 15470 15866  # 수동 지정
  python scripts/astra_subfigures.py --reference-ids 2275 --limit 0

가드: /srv/fsis2026/scripts/.env 의 ADVANCED_FEATURES_SUBFIGURE_SPLIT=true 가 아니면 exit 0 (fail-closed).
      AI 추출 플래그와 별개 — 이 레인만 따로 끌 수 있어야 한다.
환경: DATABASE_PATH, MEDIA_ROOT (cron 라인에서 명시). 호스트 venv (Django + PyMuPDF + Pillow).
"""
import argparse
import fcntl
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from astra_cli_bbox import extract  # noqa: E402

FLAG = 'ADVANCED_FEATURES_SUBFIGURE_SPLIT'
LOCKFILE = '/tmp/astra_subfigures.lock'
ENV_FILE = '/srv/fsis2026/scripts/.env'
# 다음 건도 똑같이 실패할 사유 — 첫 건에서 멈춘다(구독 한도·로그인·CLI 부재).
FATAL_MARKERS = ('login required', 'Codex CLI not found', 'usage limit', 'rate limit')


def _log(msg):
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] [{os.getpid()}] {msg}', flush=True)


def load_env_file(path=ENV_FILE):
    """claude_augment._load_env_file 와 같은 계약 — 이미 있는 환경변수는 덮지 않는다."""
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def feature_enabled(env=None):
    return (env if env is not None else os.environ).get(FLAG) == 'true'


def setup_django_readonly():
    """Django 를 읽기 전용 sqlite 로. WAL 전환도 쓰기라 export_figex_batch 와 같은 PRAGMA 를 건다."""
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings_fsis')
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    django.setup()
    from django.db import connections
    connection = connections['default']
    if connection.vendor == 'sqlite':
        connection.close()
        connection.settings_dict['OPTIONS'] = {
            **connection.settings_dict.get('OPTIONS', {}),
            'init_command': 'PRAGMA query_only=ON;',
        }


def is_fatal(exc):
    text = str(exc).lower()
    return any(marker.lower() in text for marker in FATAL_MARKERS)


def process_figure(fig, *, effort='high', timeout=600, codex='codex', retry_errors=False, dpi=None):
    """한 figure 처리. 반환 (status, detail). status:
    done(이미 완료) · exhausted(시도 소진) · completed · error. 치명 오류는 예외로 올린다."""
    from kprdb.services import figure_panels as fp
    dpi = dpi or fp.DEFAULT_DPI
    key = fp.figure_source_key(fig, dpi)
    if fp.find_result(fig, key):
        return 'done', key
    attempts = fp.attempt_count(fig, key)
    if attempts >= fp.MAX_ATTEMPTS and not retry_errors:
        return 'exhausted', f'{key} attempts={attempts}'
    caption, subfigures, provenance = fp.figure_metadata(fig)
    output = fp.next_output_path(fig, key)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fsis-subfig-') as work:
        image = Path(work) / f'fig_{fig.pk}.png'
        try:
            size = fp.render_figure(fig, image, dpi)
        except ValueError as exc:
            # 렌더 불가(쪽 번호가 PDF 범위 밖·bbox 깨짐 등)는 **이 figure 의 문제**지 이 실행의
            # 문제가 아니다. 예외로 올리면 main 이 치명으로 보고 실행을 멈추고, 아무것도 기록되지
            # 않아 다음 실행도 같은 자리에서 멈춘다 — 실제로 fig 2187(p.17, PDF 는 16 쪽)에
            # 레인 전체가 걸렸다. 시도로 기록해 3 회 뒤엔 exhausted 로 빠지게 한다.
            artifacts = output.with_name(output.name + '.run')
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / 'run.json').write_text(json.dumps(
                {'status': 'error', 'error': f'unrenderable: {exc}', 'figure_pk': fig.pk,
                 'page_no': fig.page_no}, ensure_ascii=False), encoding='utf-8')
            return 'error', f'unrenderable: {exc}'
        meta = {'figure_pk': fig.pk, 'reference_id': fig.reference_id, 'page_no': fig.page_no,
                'figure_number': fig.figure_number, 'figure_kind': fig.figure_kind,
                'source_key': key, 'dpi': dpi, 'source_bbox': fig.bbox, 'render': size,
                'caption_provenance': provenance, 'subfigure_count': len(subfigures),
                'started_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
        fp.meta_path(output).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            result = extract(image, caption, subfigures, output, codex, effort, timeout)
        except Exception as exc:
            if is_fatal(exc):
                raise
            return 'error', f'{type(exc).__name__}: {exc}'
    return 'completed', f'{key} panels={len(result["panels"])} {result["elapsed_seconds"]:.1f}s'


def main(argv=None):
    load_env_file()
    if not feature_enabled():
        return 0
    parser = argparse.ArgumentParser(description='Astra CLI 로 subfigure 캡션이 있는 figure 를 패널 분할')
    parser.add_argument('--limit', type=int, default=1, help='이번 실행에서 새로 호출할 figure 수 (0 = 전부)')
    parser.add_argument('--dry-run', action='store_true', help='후보 목록만 출력, 호출 없음')
    parser.add_argument('--min-subfigures', type=int, default=2)
    parser.add_argument('--include-maps', action='store_true', help='지도 figure 도 대상에 포함')
    parser.add_argument('--reference-ids', nargs='+', type=int)
    parser.add_argument('--figure-ids', nargs='+', type=int)
    parser.add_argument('--retry-errors', action='store_true', help='시도 3회 소진분도 다시')
    parser.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'), default='high')
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--codex', default='codex')
    args = parser.parse_args(argv)
    if args.limit < 0 or args.min_subfigures < 1 or args.timeout <= 0:
        parser.error('limit >= 0, min-subfigures >= 1, timeout > 0')

    lock = open(LOCKFILE, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log('already running, exiting')
        return 0

    setup_django_readonly()
    from kprdb.services import figure_panels as fp
    candidates = fp.candidate_figures(min_subfigures=args.min_subfigures, include_maps=args.include_maps,
                                      reference_ids=args.reference_ids, figure_ids=args.figure_ids)
    _log(f'candidates={len(candidates)} (min_subfigures={args.min_subfigures}, maps={"in" if args.include_maps else "out"})')
    counts = {'done': 0, 'exhausted': 0, 'completed': 0, 'error': 0}
    calls = 0
    for fig in candidates:
        if args.dry_run:
            key = fp.figure_source_key(fig)
            state = 'done' if fp.find_result(fig, key) else f'attempts={fp.attempt_count(fig, key)}'
            print(f'fig={fig.pk} ref={fig.reference_id} p.{fig.page_no} {fig.figure_number} kind={fig.figure_kind} '
                  f'subfigures={fp.subfigure_count(fig)} {state}')
            continue
        try:
            status, detail = process_figure(fig, effort=args.effort, timeout=args.timeout, codex=args.codex,
                                            retry_errors=args.retry_errors)
        except Exception as exc:
            _log(f'fig={fig.pk} FATAL {type(exc).__name__}: {exc} — stopping this run')
            return 2
        counts[status] += 1
        if status in ('completed', 'error'):
            calls += 1
            _log(f'fig={fig.pk} ref={fig.reference_id} {status}: {detail}')
            if args.limit and calls >= args.limit:
                break
    if not args.dry_run:
        _log('summary ' + ' '.join(f'{k}={v}' for k, v in counts.items()))
    return 1 if counts['error'] and not counts['completed'] else 0


if __name__ == '__main__':
    sys.exit(main())
