"""One-command, failure-tolerant OpenRouter model selection for PDF Processor v0.8.1.

Run inside the project's non-root Docker benchmark wrapper:
    ./scripts/benchmark.sh all --execute

Without --execute, the suite generates fixtures and plans without paid calls.
Costs are reported but never used to stop the sweep.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from benchmarks import generate
from benchmarks.run import BASE_URL, load_models, plan

DATA = Path(__file__).resolve().parent
ROUTES = ('pypdf', 'image', 'pdf_native', 'pdf_mistral', 'summary')
# These make a screening run informative while limiting its API request count.
SCREENING = {
    'image': ('invoice,serial,bilingual,receipt', 'scan,hard'),
    'pdf_native': ('invoice,serial,bilingual,receipt', 'scan,hard'),
    'pdf_mistral': ('invoice,serial,bilingual,receipt', 'scan,hard'),
    'summary': ('letter,quarterly,project,invoice', 'clean'),
    'pypdf': ('', 'clean,scan,hard'),
}


def profile_scope(route: str, profile: str) -> tuple[str, str]:
    if profile == 'screening':
        return SCREENING[route]
    if route == 'summary':
        return '', 'clean'
    return '', 'clean,scan,hard' if route == 'pypdf' else 'scan,hard'


def select_for_route(models: list[dict], catalog: dict | None, route: str) -> tuple[list[dict], list[dict]]:
    """Skip unsupported/unlisted candidates instead of aborting the entire sweep."""
    if route == 'pypdf':
        return [{'alias': 'local_pypdf', 'id': 'local/pypdf', 'routes': ['pypdf']}], []
    active, omitted = [], []
    required = 'text' if route in ('summary', 'pdf_mistral') else ('file' if route == 'pdf_native' else 'image')
    for item in models:
        if route not in item['routes']:
            continue
        entry = catalog.get(item['id']) if catalog is not None else None
        reason = None
        if catalog is not None:
            if entry is None:
                reason = 'Model absent from current OpenRouter catalog'
            elif required not in ((entry.get('architecture') or {}).get('input_modalities') or []):
                reason = f'Missing {required} input modality in catalog'
        if reason:
            omitted.append({'route': route, 'alias': item['alias'], 'model': item['id'], 'reason': reason})
        else:
            active.append(item)
    return active, omitted


def load_catalog(api_key: str) -> dict:
    with httpx.Client(timeout=httpx.Timeout(30.0), follow_redirects=False) as client:
        result = client.get(BASE_URL + '/models', headers={'Authorization': 'Bearer ' + api_key})
        result.raise_for_status()
        items = result.json()['data']
        return {item['id']: item for item in items}


def _read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line.strip()]


def _recorded_cost(rows: list[dict]) -> float:
    return sum(float(row['cost_usd']) for row in rows if row.get('cost_usd') is not None)


def _unsafe_unknown_cost(rows: list[dict]) -> bool:
    # A rejected 404 may be skipped intentionally, but do not treat it as a free request.
    return any(row.get('cost_usd') is None and row.get('status') != 'unavailable'
               and row.get('http_status') != 404 for row in rows)


def _read_comparison(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline='', encoding='utf8') as file:
        return list(csv.DictReader(file))


def _qualified_ocr(row: dict) -> bool:
    try:
        return (int(row['successes']) >= 4 and float(row['success_rate']) >= .95
                and float(row['critical_fields']) >= .95
                and int(row['missing_cost_records']) == 0
                and row['avg_cost_usd'] != '' and row['mock'] == 'False')
    except (TypeError, ValueError, KeyError):
        return False


def _report(output: Path, sections: list[dict], omissions: list[dict], *, mode: str,
            profile: str, budget: float | None, stopped_reason: str | None) -> None:
    """Human-readable cross-route navigation and comparable-within-route rankings."""
    all_comp = [row for section in sections for row in _read_comparison(output / section['route'] / 'comparison.csv')]
    keys = (list(dict.fromkeys(key for r in all_comp for key in r)) if all_comp
            else ['route', 'alias', 'model', 'successes', 'units', 'critical_fields',
                  'cer', 'summary_anchor_recall', 'avg_cost_usd', 'measured_cost_usd'])
    with (output / 'comparison.csv').open('w', newline='', encoding='utf8') as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader(); writer.writerows(all_comp)
    all_rows = [row for section in sections for row in _read_rows(output / section['route'] / 'responses.jsonl')]
    total = _recorded_cost(all_rows)
    summary = {
        'version': '0.8.1', 'mode': mode, 'profile': profile,
        'reported_spend_usd': round(total, 7), 'budget_usd': None,
        'paid_requests_recorded': sum(1 for row in all_rows if mode == 'paid' and row['route'] != 'pypdf' and row['status'] != 'unavailable'),
        'has_unknown_cost': any(row.get('cost_usd') is None and row.get('route') != 'pypdf' for row in all_rows),
        'failed_requests': sum(row.get('route') != 'pypdf' and row.get('status') == 'error' for row in all_rows),
        'pypdf_no_text_units': sum(row.get('route') == 'pypdf' and row.get('status') == 'error' for row in all_rows),
        'unavailable_units': sum(row.get('status') == 'unavailable' for row in all_rows),
        'stop_reason': None, 'stage_warnings': [stopped_reason] if stopped_reason else [],
        'sections': sections, 'omitted_models': omissions,
        'warning': 'Recorded costs may be incomplete for failed calls. No budget limit is enforced; the provider key controls credit. Model summaries require manual factual review.',
    }
    (output / 'suite.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n', encoding='utf8')
    links = []
    for section in sections:
        name = html.escape(section['route'])
        link = f'<a href="{name}/report.html">{name}/report.html</a>' if section['evaluated'] else '(no measured responses)'
        links.append(f'<li>{name}: {link}; {section["observed"]}/{section["planned"]} units recorded</li>')
    rows_html = []
    for r in all_comp:
        qual = 'YES' if _qualified_ocr(r) and r['route'] not in ('summary', 'pypdf') else 'no'
        entries = (r['route'], r['alias'], r['successes'] + '/' + r['units'], r['critical_fields'],
                   r['cer'], r['summary_anchor_recall'], r['avg_cost_usd'], r['avg_latency_s'], qual)
        rows_html.append('<tr>' + ''.join('<td>' + html.escape(str(v)) + '</td>' for v in entries) + '</tr>')
    quality_lines = []
    for route in ('image', 'pdf_native', 'pdf_mistral'):
        eligible = [r for r in all_comp if r['route'] == route and _qualified_ocr(r)]
        eligible.sort(key=lambda r: float(r['avg_cost_usd']))
        if eligible:
            cheapest = eligible[0]
            by_quality = min((r for r in eligible if r.get('cer')), key=lambda r: float(r['cer']), default=None)
            detail = (f'Cheapest passing: <b>{html.escape(cheapest["alias"])}</b> '
                      f'(${float(cheapest["avg_cost_usd"]):.6f}/request)')
            if by_quality:
                detail += (f'; lowest CER among passing: <b>{html.escape(by_quality["alias"])}</b> '
                           f'(CER {float(by_quality["cer"]):.3f})')
        else:
            detail = 'No candidate passed the provisional accuracy, sample and billing filters.'
        quality_lines.append(f'<li><b>{html.escape(route)}</b>: {detail}</li>')
    summary_candidates = [r for r in all_comp if r['route'] == 'summary' and r.get('mock') == 'False'
                          and r.get('avg_cost_usd') not in ('', None)
                          and r.get('summary_anchor_recall') not in ('', None)]
    summary_candidates.sort(key=lambda r: float(r['avg_cost_usd']))
    if summary_candidates:
        cheapest_summary = summary_candidates[0]
        summary_note = (f'Lowest measured average summary-call cost: <b>{html.escape(cheapest_summary["alias"])}</b> '
                        f'(${float(cheapest_summary["avg_cost_usd"]):.6f}, anchor recall '
                        f'{float(cheapest_summary["summary_anchor_recall"]):.3f}). '
                        'This is NOT a validated factuality ranking; review the summaries manually.')
    else:
        summary_note = 'No measured summary candidate yet.'
    omission_list = ''.join('<li>' + html.escape(o['route'] + ' · ' + o['alias'] + ': ' + o['reason']) + '</li>'
                            for o in omissions)
    banner = ('<p class="warning">MOCK RESULTS: synthetic fixture echoes, not model measurements.</p>' if mode == 'mock'
              else '<p class="warning">DRY RUN: no OpenRouter requests sent.</p>' if mode == 'dry-run' else '')
    stopped = ('<p class="warning">Stage warning(s), other stages continued: ' + html.escape(stopped_reason) + '</p>') if stopped_reason else ''
    page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PDF Processor v0.8.1 — Model Selection</title><style>
body{font:15px system-ui, sans-serif;background:#101a2a;color:#e2edf5;margin:2rem auto;max-width:1200px;line-height:1.55;padding:0 1rem}
h1,h2{color:white}a{color:#78e5db}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:8px 10px;border-bottom:1px solid #39536a;text-align:left}th{color:#75e5d3}section{overflow-x:auto;background:#1a2c40;padding:15px;border-radius:9px} .warning{background:#563c22;padding:12px;border-radius:7px}</style></head><body>'''
    page += '<h1>PDF Processor v0.8.1 · OpenRouter model selection</h1>' + banner + stopped
    page += f'<p>Mode: {html.escape(mode)} · Profile: {html.escape(profile)} · Reported spend: ${total:.6f}'
    page += f' · Failed API requests: {summary["failed_requests"]} · PyPDF no-text units: {summary["pypdf_no_text_units"]} · Unavailable units: {summary["unavailable_units"]}</p>'
    page += '<p><b>Costs are reported only; they never stop the benchmark.</b> Unknown costs are explicitly unreported, not zero. '
    page += 'Do not compare raw scores across different input routes as identical inference. Human review of summaries and identifier mistakes remains mandatory.</p>'
    page += '<h2>Stages</h2><ul>' + ''.join(links) + '</ul>'
    page += '<h2>Provisional OCR affordability threshold</h2><p>At least four successful units, ≥95% success, ≥95% exact field recall, and fully reported costs; only within each route. No automatic quality winner is declared.</p><ul>' + ''.join(quality_lines) + '</ul>'
    page += '<h2>Summarization price shortlist</h2><p>' + summary_note + '</p>'
    page += '<h2>All measured comparisons</h2><section><table><tr><th>Route</th><th>Model</th><th>Success</th><th>Critical fields</th><th>CER</th><th>Summary anchors</th><th>Avg USD/call</th><th>Avg seconds</th><th>Qualifies?</th></tr>' + ''.join(rows_html) + '</table></section>'
    if omissions:
        page += '<h2>Skipped catalog candidates</h2><ul>' + omission_list + '</ul>'
    page += '<p>Per-route HTML reports contain full ground truth / output review. All documents are synthetic. '
    page += 'Summary anchor recall is literal matching, NOT a hallucination or semantic accuracy score. '
    page += 'Mistral parser route measures a combined OCR + language-model pipeline, not isolated Mistral OCR.</p></body></html>'
    (output / 'selection.html').write_text(page, encoding='utf8')


def execute(args: argparse.Namespace) -> Path:
    if args.execute and args.mock:
        raise ValueError('Cannot combine paid and mock modes')
    if (args.max_requests is not None and args.max_requests < 1) or args.repeats < 1:
        raise ValueError('Request cap and repetitions must be positive')
    api_key = (os.getenv('OPENROUTER_API_KEY') or '').strip() if args.execute else ''
    if args.execute and not api_key:
        raise ValueError('OPENROUTER_API_KEY is required for --execute; no calls sent')
    # Catalog fetch has no model usage billing and ensures bad aliases/modalities
    # cannot abort the sweep once expensive benchmarking has started.
    catalog = load_catalog(api_key) if args.execute else None
    output = args.output_dir or (DATA / 'results' / ('suite-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')))
    if output.exists():
        raise ValueError('Suite output directory already exists; use a fresh path to avoid accidental repeated paid requests')
    # Running generate within the same unprivileged container writes host-owned PDFs.
    manifest = generate.generate(args.fixtures, seed=args.seed)
    print(f'Generated {len(manifest["documents"])} PDFs from 12 scenarios into {args.fixtures}', flush=True)
    models = load_models(args.models_file)
    stages = []
    omitted = []
    planned_requests = 0
    for route in ROUTES:
        eligible, ignored = select_for_route(models, catalog, route)
        omitted.extend(ignored)
        if not eligible:
            stages.append({'route':route,'models':[], 'model_ids':[], 'jobs':[], 'planned':0})
            continue
        cases, variants = profile_scope(route, args.profile)
        ids = [x.strip() for x in cases.split(',') if x.strip()] if cases else sorted({d['id'] for d in manifest['documents']})
        var = [v.strip() for v in variants.split(',') if v.strip()]
        jobs = plan(manifest, eligible, ids=ids, variants=var, route=route, repeats=args.repeats)
        if route != 'pypdf': planned_requests += len(jobs)
        stages.append({'route':route,'models':[m['alias'] for m in eligible],
                       'model_ids':[m['id'] for m in eligible], 'jobs':jobs,
                       'planned':len(jobs), 'cases':cases, 'variants':variants})
    if args.max_requests is not None and planned_requests > args.max_requests and args.execute:
        raise ValueError(f'{planned_requests} paid requests planned, exceeds --max-requests {args.max_requests}. '
                         'Increase the cap explicitly or use --profile screening.')
    print(f'Suite plan: {planned_requests} potential paid requests, '
          f'profile={args.profile}, models file={args.models_file}', flush=True)
    for s in stages:
        print(f' - {s["route"]:12} {len(s["models"]):2d} candidates / {s["planned"]} units', flush=True)
    for item in omitted:
        print(' SKIP catalog:', item['route'], item['alias'], item['reason'], flush=True)
    if args.execute:
        print('PAID MODE: no spend limit in the runner; costs are reported as returned by OpenRouter. '
              'Failures and unknown-cost responses will not stop other tests.', flush=True)
    elif args.mock:
        print('MOCK MODE: fixture echoes only; no OpenRouter requests.', flush=True)
    else:
        print('DRY RUN: no OpenRouter requests. Use --execute for real OpenRouter requests.', flush=True)
    output.mkdir(parents=True)
    section_info = []
    total_spend = 0.0
    stage_warnings = []
    try:
        for stage in stages:
            route = stage['route']
            if not stage['models']:
                section_info.append({'route':route, 'planned':0, 'observed':0, 'evaluated':False})
                continue
            out = output / route
            cmd = [sys.executable, '-m', 'benchmarks.run',
                   '--fixtures', str(args.fixtures), '--models-file', str(args.models_file),
                   '--route', route, '--models', ','.join(stage['models']),
                   '--variants', stage['variants'], '--repeats', str(args.repeats),
                   '--max-requests', str(max(1, stage['planned'])),
                   '--max-output-tokens', str(args.max_output_tokens),
                   '--output-dir', str(out)]
            if stage['cases']:
                cmd += ['--cases', stage['cases']]
            if args.execute and route != 'pypdf':
                cmd += ['--execute', '--on-http-404', 'skip-model']
            elif args.mock and route != 'pypdf':
                cmd += ['--mock']
            elif not args.execute and not args.mock and route != 'pypdf':
                cmd += ['--dry-run']
            print('---', route, '---', flush=True)
            status = subprocess.run(cmd, check=False).returncode
            rows = _read_rows(out / 'responses.jsonl')
            evaluated = False
            if rows:
                e = subprocess.run([sys.executable, '-m', 'benchmarks.evaluate',
                                    '--run-dir', str(out), '--fixtures', str(args.fixtures)], check=False)
                evaluated = e.returncode == 0
                if not evaluated:
                    stage_warnings.append(f'Evaluation failed for {route}')
            section_info.append({'route': route, 'planned': stage['planned'],
                                 'observed':len(rows), 'evaluated':evaluated})
            total_spend += _recorded_cost(rows)
            if status != 0:
                stage_warnings.append(f'Benchmark stage {route} exited with code {status}; inspect its logs')
            if len(rows) < stage['planned'] and (args.execute or args.mock or route == 'pypdf'):
                stage_warnings.append('Incomplete stage ' + route + ' (see route logs)')
            if stage_warnings:
                print('WARN: Stage issue(s) recorded; continuing remaining routes.', flush=True)
    finally:
        _report(output, section_info, omitted, mode=('paid' if args.execute else 'mock' if args.mock else 'dry-run'),
                profile=args.profile, budget=None, stopped_reason='; '.join(stage_warnings) or None)
        print('Suite report:', output / 'selection.html', flush=True)
        print('Combined CSV:', output / 'comparison.csv', flush=True)
        print(f'Recorded spend ${total_spend:.6f}; unreported costs may exist. No runner spending limit was enforced.', flush=True)
    return output


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, default=DATA / 'fixtures')
    parser.add_argument('--models-file', type=Path, default=DATA / 'models_broad_sweep.json')
    parser.add_argument('--output-dir', type=Path, default=None)
    parser.add_argument('--profile', choices=['full', 'screening'], default='full',
                        help='full=all 12 cases on scan/hard, screening=4 representative OCR scenarios')
    parser.add_argument('--seed', type=int, default=20261112)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--max-requests', type=int, default=None,
                        help='Optional explicit maximum planned calls; default is the complete planned suite')
    parser.add_argument('--max-output-tokens', type=int, default=4096)
    parser.add_argument('--max-budget-usd', type=float, default=None,
                        help='DEPRECATED and ignored; costs are reported without a spending limit')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true', help='Make real PAID OpenRouter calls')
    mode.add_argument('--mock', action='store_true', help='Simulated fixture echoes only')
    mode.add_argument('--dry-run', action='store_true', help='Plan stages without paid requests (default)')
    args = parser.parse_args(argv)
    if not 1 <= args.max_output_tokens <= 16384:
        parser.error('--max-output-tokens must be in 1..16384')
    if args.max_budget_usd is not None:
        print('NOTE: --max-budget-usd is deprecated and ignored; costs are report-only.', flush=True)
    try:
        execute(args)
    except (OSError, ValueError, httpx.HTTPError, json.JSONDecodeError) as err:
        parser.exit(2, 'ERROR: ' + str(err) + '\n')


if __name__ == '__main__':
    main()
