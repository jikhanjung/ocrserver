"""Local figure + caption -> validated pixel boxes via Codex CLI / ChatGPT login."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from PIL import Image

try:
    from .astra_panels import PROMPT, SCHEMA, atomic_json, pixel_box, validate_prediction
except ImportError:
    from astra_panels import PROMPT, SCHEMA, atomic_json, pixel_box, validate_prediction

MODEL = 'gpt-6-astra'
# Preserve the target even where a rectangular crop necessarily includes neighbours.
INSTRUCTIONS = PROMPT + """
For interleaved specimens, including parts of neighbouring specimens inside a
rectangular crop is acceptable. Never shrink a box to remove a neighbour if doing
so clips the target specimen or its printed label. Target completeness takes
priority over tight boundaries. Shared scale bars need not be in each crop;
describe them in notes. Inspect the attached image directly. Return only the
requested JSON. Do not read other files, run commands, or use external tools.
"""


def load_captions(path, subcaptions=None):
    """Accept plain UTF-8 text or a structured caption JSON object."""
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix.lower() == '.json':
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError('Caption JSON must be an object')
        caption = data.get('original_caption', data.get('caption', ''))
        panels = data.get('existing_subfigures', data.get('subfigures', []))
    else:
        caption, panels = text, []
    if subcaptions:
        panels = json.loads(subcaptions.read_text(encoding='utf-8-sig'))
    if not isinstance(caption, str) or not isinstance(panels, list):
        raise ValueError('caption must be text; subfigures must be a list')
    for panel in panels:
        if not isinstance(panel, dict) or not isinstance(panel.get('label'), str):
            raise ValueError('Each subfigure needs a string label')
        if not isinstance(panel.get('description', panel.get('text', panel.get('caption', ''))), str):
            raise ValueError('Subfigure description must be text')
    return caption, panels


def run_command(command, env, cwd, timeout, prompt=None):
    """Stop the CLI and its children on timeout/interruption."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=env, cwd=cwd,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return process.returncode, stdout, stderr


def extract(image, caption, subfigures, output, codex='codex', effort='high', timeout=600):
    image, output = image.resolve(), output.resolve()
    with Image.open(image) as im:
        if im.format not in ('PNG', 'JPEG', 'WEBP'):
            raise ValueError('Use a PNG, JPEG or WebP image')
        width, height = im.size
        im.verify()
    executable = shutil.which(codex)
    if not executable:
        raise ValueError('Codex CLI not found; install it and run codex login')
    artifacts = output.with_name(output.name + '.run')
    if output.exists() or artifacts.exists():
        raise ValueError('Output or its .run directory exists; use a new output path')
    env = os.environ.copy()
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY'):
        env.pop(key, None)
    metadata = {'image_width': width, 'image_height': height,
                'original_caption': caption, 'existing_subfigures': subfigures}
    prompt = INSTRUCTIONS + '\n' + json.dumps(metadata, ensure_ascii=False)
    with tempfile.TemporaryDirectory(prefix='fsis-astra-bbox-') as work:
        code, status, login_stderr = run_command([executable, 'login', 'status'], env, work, 30)
        # Codex currently prints login status to stderr on some versions.
        if code or 'logged in using chatgpt' not in (status + login_stderr).lower():
            raise ValueError('ChatGPT login required: run codex login first')
        artifacts.mkdir(parents=True, exist_ok=False)
        atomic_json(artifacts / 'schema.json', SCHEMA)
        (artifacts / 'prompt.txt').write_text(prompt, encoding='utf-8')
        command = [executable, 'exec', '--ignore-user-config', '--ephemeral',
                   '--skip-git-repo-check', '--sandbox', 'read-only', '--model', MODEL,
                   '-c', 'model_reasoning_effort=' + json.dumps(effort),
                   '--image', str(image), '--output-schema', str(artifacts / 'schema.json'),
                   '--output-last-message', str(artifacts / 'response.json'), '--json', '-']
        run = {'model': MODEL, 'method': 'codex_cli', 'auth': 'ChatGPT', 'effort': effort,
               'image': str(image), 'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
               'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
               'command': command, 'status': 'running'}
        atomic_json(artifacts / 'run.json', run)
        start = time.monotonic()
        try:
            code, stdout, stderr = run_command(command, env, work, timeout, prompt)
            (artifacts / 'events.jsonl').write_text(stdout, encoding='utf-8')
            (artifacts / 'stderr.log').write_text(stderr, encoding='utf-8')
            if code:
                raise ValueError(f'Codex exited with code {code}; see {artifacts / "stderr.log"}')
            events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            if any(e.get('type') in ('turn.failed', 'error') for e in events):
                raise ValueError('Codex reported an error; see events.jsonl')
            if not any(e.get('type') == 'turn.completed' for e in events):
                raise ValueError('Codex did not report a completed turn')
            raw = json.loads((artifacts / 'response.json').read_text(encoding='utf-8'))
            prediction = validate_prediction(raw, {'subfigures': subfigures})
            elapsed = round(time.monotonic() - start, 2)
            result = {'schema_version': 1, 'model': MODEL, 'method': 'codex_cli',
                      'image': str(image), 'image_width': width, 'image_height': height,
                      'image_sha256': run['image_sha256'], 'elapsed_seconds': elapsed,
                      'bbox_coordinate_system': 'original_image_xyxy_pixels',
                      'bbox_convention': 'left/top inclusive, right/bottom exclusive (Pillow crop)',
                      'is_compound': prediction['is_compound'],
                      'figure_kind': prediction['figure_kind'], 'notes': prediction['notes'],
                      'original_caption': caption, 'existing_subfigures': subfigures,
                      'cli_turn_usage': [e.get('usage', {}) for e in events if e.get('type') == 'turn.completed'],
                      'panels': [{'label': p['label'], 'bbox': pixel_box(p['bbox'], width, height),
                                  'bbox_normalized_1000': p['bbox'],
                                  'caption_indices': p['caption_indices'], 'confidence': p['confidence']}
                                 for p in prediction['panels']]}
            atomic_json(output, result)
            run.update(status='completed', panels=len(result['panels']))
            return result
        except (Exception, KeyboardInterrupt) as exc:
            run.update(status='error', error=type(exc).__name__ + ': ' + str(exc))
            raise
        finally:
            run['elapsed_seconds'] = round(time.monotonic() - start, 2)
            atomic_json(artifacts / 'run.json', run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, type=Path)
    parser.add_argument('--caption', required=True, type=Path, help='UTF-8 .txt or caption JSON')
    parser.add_argument('--subcaptions', type=Path, help='Optional JSON list of labelled descriptions')
    parser.add_argument('--output', required=True, type=Path, help='New bbox JSON path')
    parser.add_argument('--codex', default='codex', help='Codex executable name or path')
    parser.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'), default='high')
    parser.add_argument('--timeout', type=float, default=600, help='CLI execution timeout in seconds')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    try:
        caption, panels = load_captions(args.caption, args.subcaptions)
        result = extract(args.image, caption, panels, args.output, args.codex, args.effort, args.timeout)
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    print(f'{len(result["panels"])} panels -> {args.output} ({result["elapsed_seconds"]:.2f}s)', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
