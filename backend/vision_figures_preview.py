#!/usr/bin/env python3
"""Isolated local preview for the FOURTH parser (no archive writes).

Offline: uv run python vision_figures_preview.py --input X.pdf --fixture-dir ./fixtures/vision --output ./preview
Live:    uv run python vision_figures_preview.py --input X.pdf --send --output ./preview
Never touches Redis, RustFS, OpenSearch or the application's existing runs.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote

from app.config import Settings
from app.openrouter_client import OpenRouterClient
from app.parsers.vision_figures import VisionFiguresParser
from app.errors import ProcessingError


class FixtureClient:
    def __init__(self, directory: Path):
        self.directory = directory

    async def complete_vision_figures_page(self, image: bytes, *, page: int, max_tokens: int) -> str:
        fixture = self.directory / f'page_{page:04d}.json'
        if not fixture.is_file():
            raise ProcessingError('FIXTURE_MISSING', f'Offline fixture missing for page {page}.')
        return fixture.read_text(encoding='utf-8')


def write_preview(result, output: Path) -> None:
    """Write only the canonical local Markdown and PNG assets (no request/secret data)."""
    output.mkdir(parents=True, exist_ok=False)
    (output / 'images').mkdir()
    paths = set()
    manifest = []
    for asset in result.assets:
        if not asset.path.startswith('images/') or Path(asset.path).name != unquote(asset.path.split('/')[-1]):
            raise ProcessingError('INVALID_ASSET', 'Invalid local image name.')
        if asset.path in paths or '/' in Path(asset.path).name or '\\' in asset.path or '..' in asset.path:
            raise ProcessingError('INVALID_ASSET', 'Duplicate or unsafe image name.')
        paths.add(asset.path)
        if hashlib.sha256(asset.data).hexdigest() != asset.sha256:
            raise ProcessingError('RESULT_CORRUPT', 'Figure data hash does not match.')
        (output / asset.path).write_bytes(asset.data)
        manifest.append({'page': asset.page, 'id': asset.id, 'path': asset.path,
                         'kind': asset.kind, 'caption': asset.caption, 'sha256': asset.sha256,
                         'bytes': len(asset.data), 'width': asset.width, 'height': asset.height,
                         'bbox_raw': list(asset.raw_bbox_norm), 'bbox_adjusted': list(asset.adjusted_bbox_norm),
                         'warnings': list(asset.warnings)})
    document = '\n\n---\n\n'.join(f'# Page {page.page}\n\n{page.markdown.strip() or "_(Blank page)_"}'
                                     for page in result.pages) + '\n'
    (output/'document.md').write_text(document, encoding='utf-8')
    (output/'figures.json').write_text(json.dumps({'figures': manifest, 'warnings': result.warnings}, indent=2, ensure_ascii=False) + '\n')


async def main():
    ap = argparse.ArgumentParser(description='Preview fourth parser outputs outside the production archive')
    ap.add_argument('--input', required=True, type=Path, help='PDF to transcribe')
    ap.add_argument('--output', type=Path, required=True, help='New output directory (must not exist)')
    ap.add_argument('--fixture-dir', type=Path, help='Offline page_0001.json, page_0002.json, etc.')
    ap.add_argument('--env-file', type=Path, help='Existing PDF Processor .env for a live request; never copied')
    ap.add_argument('--send', action='store_true', help='Explicitly authorize paid OpenRouter calls')
    args = ap.parse_args()
    if args.send == bool(args.fixture_dir):
        ap.error('Choose EXACTLY ONE: --send (paid) OR --fixture-dir (offline).')
    if not args.input.is_file():
        ap.error('Input PDF does not exist.')
    if args.output.exists():
        ap.error('Output path already exists: refuse to overwrite a previous run.')
    if args.env_file and not args.env_file.is_file():
        ap.error('Environment file is missing.')
    settings = Settings(_env_file=str(args.env_file) if args.env_file else None)
    if args.send:
        print(f'PAID PREVIEW: model={settings.vision_figures_model}; one HTTP attempt per page; NO automatic retries.')
        async with _openrouter(settings) as llm:
            result = await VisionFiguresParser(llm, settings).parse(args.input)
    else:
        print('OFFLINE PREVIEW: saved JSON only, zero paid calls.')
        result = await VisionFiguresParser(FixtureClient(args.fixture_dir), settings).parse(args.input)
    write_preview(result, args.output)
    print(f'Result: {args.output / "document.md"}; pages={len(result.pages)}; figures={len(result.assets)}; warnings={len(result.warnings)}')


class _openrouter:
    def __init__(self, settings): self.llm = OpenRouterClient(settings)
    async def __aenter__(self): return self.llm
    async def __aexit__(self, *_): await self.llm.aclose()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except ProcessingError as exc:
        import sys
        print(f'PREVIEW FAILED: {exc.code}: {exc.message}', file=sys.stderr)
        sys.exit(1)
