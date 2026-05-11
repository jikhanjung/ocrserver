#!/usr/bin/env python3
"""Batch OCR processor for Chandra2-vLLM Pod.

Scans /workspace/pdfs/ for PDF files, renders pages to JPEG,
sends to local vLLM server, saves results as JSON.

Usage:
    python3 batch_ocr.py [--input-dir /workspace/pdfs] [--output-dir /workspace/ocr_json]
                         [--dpi 150] [--vllm-url http://localhost:8000] [--resume]
                         [--limit 100] [--concurrency 4]

Output: {sha256_hash}.json per PDF, same format as PaperMeister's ocr_json.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import fitz  # PyMuPDF
import requests
from PIL import Image


def sha256_file(filepath):
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(131072), b''):
            h.update(chunk)
    return h.hexdigest()


def render_page(doc, page_idx, dpi=150, quality=85):
    """Render a single PDF page to base64 JPEG."""
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = doc[page_idx].get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def ocr_page(image_b64, vllm_url, timeout=120, retries=3, session=None):
    """Send one page image to vLLM and return markdown text."""
    payload = {
        'model': 'chandra',
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'Extract all text from this page in markdown.'},
                {'type': 'image_url', 'image_url': {
                    'url': f'data:image/jpeg;base64,{image_b64}',
                }},
            ],
        }],
        'max_tokens': 8192,
    }
    client = session or requests
    last_err = None

    for attempt in range(1, retries + 1):
        try:
            resp = client.post(
                f'{vllm_url}/v1/chat/completions',
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get('choices', [])
            if choices:
                return choices[0].get('message', {}).get('content', '')
            return ''
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)

    raise last_err


def wait_for_vllm(vllm_url, timeout=300):
    """Wait for vLLM server to become ready."""
    print(f'[INIT] Waiting for vLLM server at {vllm_url} ...', flush=True)
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(f'{vllm_url}/health', timeout=5)
            if resp.status_code == 200:
                print(f'[INIT] vLLM server ready ({time.time() - start:.1f}s)', flush=True)
                return True
        except Exception:
            pass
        time.sleep(5)
    print(f'[INIT] ERROR: vLLM not ready after {timeout}s', flush=True)
    return False


def find_pdfs(input_dir):
    """Recursively find all PDF files."""
    pdfs = []
    for root, dirs, files in os.walk(input_dir):
        for f in sorted(files):
            if f.lower().endswith('.pdf'):
                pdfs.append(os.path.join(root, f))
    return pdfs


def is_valid_output_json(path):
    """Return True when an output JSON exists and looks complete enough to resume from."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return False

    if not isinstance(data, dict):
        return False

    total_pages = data.get('total_pages')
    done_pages = data.get('done_pages')
    failed_pages = data.get('failed_pages')
    pages = data.get('pages')

    if not isinstance(total_pages, int) or total_pages < 0:
        return False
    if not isinstance(done_pages, int) or done_pages < 0:
        return False
    if not isinstance(failed_pages, int) or failed_pages < 0:
        return False
    if not isinstance(pages, list):
        return False

    if len(pages) != total_pages:
        return False
    if done_pages + failed_pages != total_pages:
        return False

    return failed_pages == 0


def load_partial_output_json(path):
    """Load an existing partial OCR result if it is structurally usable for page-level retries."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    total_pages = data.get('total_pages')
    done_pages = data.get('done_pages')
    failed_pages = data.get('failed_pages')
    failed_page_numbers = data.get('failed_page_numbers')
    pages = data.get('pages')

    if not isinstance(total_pages, int) or total_pages < 0:
        return None
    if not isinstance(done_pages, int) or done_pages < 0:
        return None
    if not isinstance(failed_pages, int) or failed_pages < 0:
        return None
    if not isinstance(failed_page_numbers, list):
        return None
    if not isinstance(pages, list) or len(pages) != total_pages:
        return None
    if done_pages + failed_pages != total_pages:
        return None

    page_results = {}
    for page in pages:
        if not isinstance(page, dict):
            return None
        page_idx = page.get('page')
        if not isinstance(page_idx, int):
            return None
        page_results[page_idx] = page

    if len(page_results) != total_pages:
        return None

    failed_page_indexes = set()
    for page_num in failed_page_numbers:
        if not isinstance(page_num, int):
            return None
        page_idx = page_num - 1
        if page_idx < 0 or page_idx >= total_pages:
            return None
        failed_page_indexes.add(page_idx)

    if len(failed_page_indexes) != failed_pages:
        return None

    if failed_pages == 0:
        return None

    return {
        'page_results': page_results,
        'failed_page_indexes': failed_page_indexes,
    }


def save_json_atomic(out_path, result):
    """Write JSON atomically so interrupted runs do not leave partial output files."""
    tmp_path = f'{out_path}.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, out_path)


def process_single_page(doc, page_idx, dpi, vllm_url, session):
    """Render and OCR one page, returning a page record."""
    page_t0 = time.time()
    image_b64 = render_page(doc, page_idx, dpi)
    text = ocr_page(image_b64, vllm_url, session=session)
    page_elapsed = time.time() - page_t0
    return {
        'page': page_idx,
        'markdown': text.strip(),
        'duration_ms': int(page_elapsed * 1000),
    }


def process_single_page_from_pdf(pdf_path, page_idx, dpi, vllm_url):
    """Open a PDF, process one page, and return its OCR result."""
    with fitz.open(pdf_path) as doc:
        return process_single_page(doc, page_idx, dpi, vllm_url, session=None)


def run_page_jobs(pdf_path, page_indexes, dpi, vllm_url, concurrency, total_pages, prefix):
    """Process a set of pages with bounded concurrency."""
    page_results = {}
    failed_pages = {}

    if concurrency <= 1:
        for page_idx in page_indexes:
            try:
                page_record = process_single_page_from_pdf(pdf_path, page_idx, dpi, vllm_url)
                page_results[page_idx] = page_record
                if (page_idx + 1) % 10 == 0 or page_idx == 0:
                    print(
                        f'{prefix}   page {page_idx + 1}/{total_pages} '
                        f'({page_record["duration_ms"] / 1000:.1f}s)',
                        flush=True,
                    )
            except Exception as e:
                failed_pages[page_idx] = str(e)
                print(f'{prefix}   page {page_idx + 1} ERROR: {e}', flush=True)
                page_results[page_idx] = {
                    'page': page_idx,
                    'markdown': '',
                    'error': str(e),
                }
        return page_results, failed_pages

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_page = {
            executor.submit(process_single_page_from_pdf, pdf_path, page_idx, dpi, vllm_url): page_idx
            for page_idx in page_indexes
        }
        for future in as_completed(future_to_page):
            page_idx = future_to_page[future]
            try:
                page_record = future.result()
                page_results[page_idx] = page_record
                print(
                    f'{prefix}   page {page_idx + 1}/{total_pages} '
                    f'({page_record["duration_ms"] / 1000:.1f}s)',
                    flush=True,
                )
            except Exception as e:
                failed_pages[page_idx] = str(e)
                print(f'{prefix}   page {page_idx + 1} ERROR: {e}', flush=True)
                page_results[page_idx] = {
                    'page': page_idx,
                    'markdown': '',
                    'error': str(e),
                }

    return page_results, failed_pages


def process_pdf(pdf_path, output_dir, vllm_url, dpi=150, concurrency=1, file_idx=0, total_files=0):
    """Process a single PDF: render all pages, OCR each, save JSON."""
    prefix = f'[{file_idx}/{total_files}]' if total_files else ''
    filename = os.path.basename(pdf_path)

    # Compute hash
    file_hash = sha256_file(pdf_path)
    out_path = os.path.join(output_dir, f'{file_hash}.json')

    print(f'{prefix} {filename}', flush=True)
    print(f'{prefix}   hash: {file_hash[:16]}...', flush=True)

    # Open PDF
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f'{prefix}   ERROR opening PDF: {e}', flush=True)
        return None, str(e)

    total_pages = doc.page_count
    print(f'{prefix}   pages: {total_pages}', flush=True)

    partial_result = load_partial_output_json(out_path)
    page_results = {}
    failed_pages = {}
    page_indexes_to_process = list(range(total_pages))

    if partial_result:
        page_results = dict(partial_result['page_results'])
        failed_pages = {page_idx: page_results[page_idx].get('error', 'previous run failed')
                        for page_idx in sorted(partial_result['failed_page_indexes'])}
        page_indexes_to_process = sorted(partial_result['failed_page_indexes'])
        print(
            f'{prefix}   resuming partial output: retrying {len(page_indexes_to_process)} failed pages',
            flush=True,
        )

    t0 = time.time()
    doc.close()

    current_results, current_failures = run_page_jobs(
        pdf_path,
        page_indexes_to_process,
        dpi,
        vllm_url,
        concurrency,
        total_pages,
        prefix,
    )
    page_results.update(current_results)
    failed_pages.update(current_failures)
    for page_idx in current_results:
        if page_idx not in current_failures:
            failed_pages.pop(page_idx, None)

    if failed_pages:
        retry_page_numbers = ', '.join(str(page_idx + 1) for page_idx in sorted(failed_pages))
        print(f'{prefix}   retrying failed pages: {retry_page_numbers}', flush=True)
        time.sleep(3)

        retry_results, retry_failures = run_page_jobs(
            pdf_path,
            sorted(failed_pages),
            dpi,
            vllm_url,
            concurrency,
            total_pages,
            prefix,
        )
        page_results.update(retry_results)
        failed_pages = retry_failures
        for page_idx in sorted(retry_results):
            if page_idx not in retry_failures:
                print(
                    f'{prefix}   page {page_idx + 1}/{total_pages} recovered '
                    f'({retry_results[page_idx]["duration_ms"] / 1000:.1f}s)',
                    flush=True,
                )
            else:
                print(f'{prefix}   page {page_idx + 1} RETRY ERROR: {retry_failures[page_idx]}', flush=True)

    elapsed = time.time() - t0

    # Build result (same format as PaperMeister ocr_json)
    result = {
        'pdf': filename,
        'source_pdf': pdf_path,
        'hash': file_hash,
        'processed_at': datetime.now().isoformat(),
        'dpi': dpi,
        'total_pages': total_pages,
        'done_pages': total_pages - len(failed_pages),
        'failed_pages': len(failed_pages),
        'failed_page_numbers': [page_idx + 1 for page_idx in sorted(failed_pages)],
        'elapsed_seconds': round(elapsed, 1),
        'pages': [page_results[page_idx] for page_idx in sorted(page_results)],
    }

    # Save
    save_json_atomic(out_path, result)

    pages_per_sec = total_pages / elapsed if elapsed > 0 else 0
    print(f'{prefix}   done: {total_pages - len(failed_pages)}/{total_pages} pages, '
          f'{elapsed:.1f}s ({pages_per_sec:.2f} pages/s) → {os.path.basename(out_path)}',
          flush=True)

    if failed_pages:
        return file_hash, f'{len(failed_pages)} pages failed'

    return file_hash, None


def main():
    parser = argparse.ArgumentParser(description='Batch OCR for Chandra2-vLLM Pod')
    parser.add_argument('--input-dir', default='/workspace/pdfs', help='Directory with PDF files')
    parser.add_argument('--output-dir', default='/workspace/ocr_json', help='Output directory for JSON results')
    parser.add_argument('--vllm-url', default='http://localhost:8000', help='vLLM server URL')
    parser.add_argument('--dpi', type=int, default=150)
    parser.add_argument('--concurrency', type=int, default=4, help='Number of pages to OCR concurrently')
    parser.add_argument('--resume', action='store_true', help='Skip PDFs that already have output JSON')
    parser.add_argument('--limit', type=int, default=0, help='Process at most N files (0=all)')
    args = parser.parse_args()
    args.concurrency = max(1, args.concurrency)

    os.makedirs(args.output_dir, exist_ok=True)

    # Wait for vLLM
    if not wait_for_vllm(args.vllm_url):
        return 1

    # Find PDFs
    pdfs = find_pdfs(args.input_dir)
    print(f'\n[SCAN] Found {len(pdfs)} PDF files in {args.input_dir}', flush=True)

    if not pdfs:
        print('[SCAN] Nothing to process.', flush=True)
        return 0

    # Resume: skip already processed
    if args.resume:
        before = len(pdfs)
        filtered = []
        for pdf_path in pdfs:
            h = sha256_file(pdf_path)
            json_path = os.path.join(args.output_dir, f'{h}.json')
            if not is_valid_output_json(json_path):
                filtered.append(pdf_path)
        pdfs = filtered
        print(f'[SCAN] Resume: {before - len(pdfs)} already done, {len(pdfs)} remaining', flush=True)

    if args.limit > 0:
        pdfs = pdfs[:args.limit]
        print(f'[SCAN] --limit {args.limit} applied', flush=True)

    # Process
    total = len(pdfs)
    ok = 0
    failed = 0
    total_pages = 0
    global_t0 = time.time()

    print(f'\n{"="*60}', flush=True)
    print(f'[START] Processing {total} PDFs', flush=True)
    print(f'[START] Page concurrency: {args.concurrency}', flush=True)
    print(f'{"="*60}\n', flush=True)

    for i, pdf_path in enumerate(pdfs, 1):
        file_hash, err = process_pdf(
            pdf_path, args.output_dir, args.vllm_url,
            dpi=args.dpi, concurrency=args.concurrency, file_idx=i, total_files=total,
        )
        if err:
            failed += 1
        else:
            ok += 1
            # Count pages from the output
            json_path = os.path.join(args.output_dir, f'{file_hash}.json')
            try:
                with open(json_path) as f:
                    data = json.load(f)
                total_pages += data.get('done_pages', 0)
            except Exception:
                pass
        print('', flush=True)

    global_elapsed = time.time() - global_t0

    print(f'{"="*60}', flush=True)
    print(f'[DONE]', flush=True)
    print(f'  Files:  {ok} ok, {failed} failed, {total} total', flush=True)
    print(f'  Pages:  {total_pages}', flush=True)
    print(f'  Time:   {global_elapsed:.1f}s ({global_elapsed/60:.1f}m)', flush=True)
    if total_pages > 0:
        print(f'  Speed:  {total_pages/global_elapsed:.2f} pages/s', flush=True)
        print(f'  Cost:   ~${global_elapsed/3600 * 0.39:.2f} (A40 @ $0.39/hr)', flush=True)
    print(f'  Output: {args.output_dir}', flush=True)
    print(f'{"="*60}', flush=True)


if __name__ == '__main__':
    sys.exit(main() or 0)
