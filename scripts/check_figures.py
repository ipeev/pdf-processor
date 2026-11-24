#!/usr/bin/env python3
"""v0.12.8 fourth-parser HTTP smoke: stdlib only; explicit paid upload.

No implicit retries, no API keys/document text in the emitted diagnostics.
This probes real worker calls, archived PNGs, and portable ZIP bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
import uuid
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SmokeFailure(Exception):
    pass


def request(base: str, method: str, path: str, *, payload: bytes | None = None,
            content_type: str | None = None, cap: int = 120_000_000, timeout: int = 40):
    headers = {'Accept': 'application/json'}
    if content_type:
        headers['Content-Type'] = content_type
    req = Request(base + path, data=payload, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            status, content = response.status, response.read(cap + 1)
    except HTTPError as exc:
        status, content = exc.code, exc.read(min(cap, 2_000_000) + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise SmokeFailure(f'HTTP {path}: unavailable: {type(exc).__name__}: {exc}') from exc
    if len(content) > cap:
        raise SmokeFailure(f'HTTP {path}: body exceeds inspection limit')
    return status, content


def as_json(status, payload, label):
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise SmokeFailure(f'{label}: HTTP {status}; invalid JSON') from exc
    if not isinstance(data, dict):
        raise SmokeFailure(f'{label}: JSON object expected')
    return data


def expect_json(base, path, *, status_expected=200):
    code, raw = request(base, 'GET', path, cap=8_000_000)
    data = as_json(code, raw, path)
    if code != status_expected:
        err = data.get('error') or {}
        raise SmokeFailure(f'{path}: HTTP {code} instead of {status_expected}; '
                           f'error={err.get("code", "unknown") if isinstance(err,dict) else "unknown"}')
    return data


def multipart_pdf(raw: bytes):
    boundary = '----pdf-figures-' + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="parser"\r\n\r\n'
            'vision_figures\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
            'filename="figure-smoke.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode()
    return body + raw + f'\r\n--{boundary}--\r\n'.encode(), f'multipart/form-data; boundary={boundary}'


def run(args, report):
    base = args.base_url.rstrip('/')
    if not base.endswith('/api/v1'):
        base += '/api/v1'
    for path in ('/health', '/ready'):
        obj = expect_json(base, path)
        expected = 'ok' if path == '/health' else 'ready'
        if obj.get('status') != expected:
            raise SmokeFailure(f'{path}: status {obj.get("status")!r}, expected {expected}')
    report['health'] = 'passed'
    if not args.paid_live:
        report['mode'] = 'health-only'
        return
    report['mode'] = 'paid-live'
    sample = Path(args.input)
    if not sample.is_file():
        raise SmokeFailure(f'Test PDF not found: {sample}')
    raw = sample.read_bytes()
    if not raw.startswith(b'%PDF-') or len(raw) > 20_000_000:
        raise SmokeFailure('Test input must be a PDF under 20 MB')
    print('PAID TEST: ONE explicit two-page vision_figures upload; no smoke-script upload retries.', flush=True)
    body, mime = multipart_pdf(raw)
    code, response = request(base, 'POST', '/documents', payload=body, content_type=mime, cap=2_000_000)
    answer = as_json(code, response, '/documents')
    if code != 202 or not isinstance(answer.get('documents'), list) or len(answer['documents']) != 1:
        err = answer.get('error') or {}
        raise SmokeFailure(f'POST /documents returned HTTP {code}, error '
                           f'{err.get("code") if isinstance(err,dict) else "unknown"}; '
                           'do not re-upload blindly if acceptance is uncertain')
    doc = answer['documents'][0]
    try:
        run_id = str(uuid.UUID(doc['id']))
    except (KeyError, ValueError, AttributeError) as exc:
        raise SmokeFailure('POST accepted without a valid run UUID') from exc
    report['run_id'] = run_id
    report['upload_cache'] = doc.get('cache')
    if doc.get('parser') != 'vision_figures':
        raise SmokeFailure('POST accepted wrong parser')
    print(f'Accepted vision_figures run {run_id} (cache={doc.get("cache")})', flush=True)
    end = time.monotonic() + args.wait_seconds
    status = None
    while time.monotonic() < end:
        state = expect_json(base, f'/documents/{run_id}')
        status = state.get('status')
        if status in ('completed', 'partial', 'failed'):
            report['status'] = status
            err = state.get('error')
            if isinstance(err, dict):
                report['pipeline_error_code'] = err.get('code')
            break
        time.sleep(min(2, max(0.1, end - time.monotonic())))
    else:
        raise SmokeFailure(f'Timed out after {args.wait_seconds}s, last state={status!r}')
    # Extraction might exist even when summarization is partial. Preserve useful results.
    if status == 'failed':
        raise SmokeFailure(f'Extraction failed, code={report.get("pipeline_error_code", "unknown")}')
    pages = expect_json(base, f'/documents/{run_id}/pages').get('pages')
    if not isinstance(pages, list) or len(pages) != 2:
        raise SmokeFailure('Expected two extracted pages')
    if any(p.get('page') != index or not isinstance(p.get('markdown'), str)
           for index, p in enumerate(pages, 1)):
        raise SmokeFailure('Page order or Markdown malformed')
    meta = expect_json(base, f'/runs/{run_id}/figures')
    figures = meta.get('figures')
    if not isinstance(figures, list):
        raise SmokeFailure('No figure catalogue')
    report['pages'] = len(pages)
    report['figures'] = len(figures)
    report['warnings_count'] = len(meta.get('warnings', [])) if isinstance(meta.get('warnings'), list) else None
    expected = [2, 1]
    for page_number, expected_count in enumerate(expected, 1):
        page_figures = [f for f in figures if f.get('page') == page_number]
        markdown = pages[page_number - 1]['markdown']
        if len(page_figures) != expected_count:
            raise SmokeFailure(f'Page {page_number}: expected {expected_count} figures, found {len(page_figures)}')
        for entry in page_figures:
            if f'](images/{entry["filename"]})' not in markdown:
                raise SmokeFailure(f'Page {page_number}: missing Markdown image reference')
            path = f'/runs/{run_id}/images/{entry["filename"]}'
            code, image = request(base, 'GET', path, cap=12_000_000)
            if (code != 200 or not image.startswith(b'\x89PNG\r\n\x1a\n')
                    or hashlib.sha256(image).hexdigest() != entry.get('sha256')):
                raise SmokeFailure(f'Page {page_number}: invalid PNG/sha or HTTP status')
    code, binary = request(base, 'GET', f'/runs/{run_id}/bundle.zip', cap=120_000_000)
    if code != 200:
        raise SmokeFailure(f'Figure ZIP download returned HTTP {code}')
    try:
        with zipfile.ZipFile(io.BytesIO(binary)) as archive:
            paths = archive.namelist()
            if paths != ['document.md', 'figures.json', *['images/' + f['filename'] for f in figures]]:
                # Asset order is index order; do not permit extra unverified ZIP files.
                raise SmokeFailure(f'ZIP entries differ from figure index: {paths}')
            if archive.testzip() is not None:
                raise SmokeFailure('ZIP contains a damaged member')
            for entry in figures:
                if hashlib.sha256(archive.read('images/' + entry['filename'])).hexdigest() != entry['sha256']:
                    raise SmokeFailure('ZIP image checksum mismatch')
            md = archive.read('document.md').decode('utf-8')
            if any(f'](images/{f["filename"]})' not in md for f in figures):
                raise SmokeFailure('ZIP Markdown does not refer to all bundled images')
    except (zipfile.BadZipFile, UnicodeError) as exc:
        raise SmokeFailure(f'Invalid ZIP document bundle: {exc}') from exc
    report['archive'] = 'validated'
    if status != 'completed':
        raise SmokeFailure(f'Extraction and images were valid, but run status is {status}: '
                           f'{report.get("pipeline_error_code", "summary not completed")}')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--base-url', default='http://127.0.0.1:8000/api/v1')
    ap.add_argument('--input', default='/app/backend/tests/fixtures/vision/sample_figures.pdf')
    ap.add_argument('--wait-seconds', type=int, default=600)
    ap.add_argument('--paid-live', action='store_true', help='Explicitly authorizes paid OpenRouter processing')
    args = ap.parse_args()
    report = {'release': '0.12.8', 'mode': 'health-only', 'status': 'not_started', 'health': 'not_checked'}
    try:
        run(args, report)
    except (SmokeFailure, AssertionError, KeyError, TypeError, ValueError) as exc:
        report['outcome'] = 'FAIL'
        report['reason'] = str(exc)[:400]
        print(f'FAIL: {exc}', file=sys.stderr, flush=True)
        print('FIGURE_SMOKE_JSON=' + json.dumps(report, sort_keys=True))
        return 1
    report['outcome'] = 'PASS'
    if not args.paid_live:
        report['status'] = 'ready'
    print('FIGURE_SMOKE_JSON=' + json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
