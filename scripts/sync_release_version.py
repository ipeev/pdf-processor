#!/usr/bin/env python3
"""Synchronize generated release-version copies with backend/pyproject.toml.

That file is the ONLY manually edited application release version. The npm
manifest/lock, uv local package entry, and Compose labels must duplicate it
because of their independent packaging and deployment formats. --check never
writes files and is safe for CI; --write updates only version fields.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / 'backend/pyproject.toml').read_text())['project']['version']


def expected_files() -> dict[Path, str]:
    results: dict[Path, str] = {}
    for file in ('frontend/package.json', 'frontend/package-lock.json'):
        path = ROOT / file
        data = json.loads(path.read_text())
        data['version'] = VERSION
        if file.endswith('package-lock.json'):
            data['packages']['']['version'] = VERSION
        results[path] = json.dumps(data, ensure_ascii=False, indent=2) + '\n'

    path = ROOT / 'backend/uv.lock'
    source = path.read_text()
    pattern = r'(?ms)(^\[\[package\]\]\nname = "async-pdf-processor"\nversion = ")[^"]+("\n)'
    updated, count = re.subn(pattern, lambda m: m.group(1) + VERSION + m.group(2), source)
    if count != 1:
        raise ValueError('Expected one local async-pdf-processor entry in backend/uv.lock')
    results[path] = updated

    path = ROOT / 'docker-compose.yml'
    source = path.read_text()
    pattern = r'(?m)^(\s+org\.opencontainers\.image\.version: ")[^"]+("\s*)$'
    updated, count = re.subn(pattern, lambda m: m.group(1) + VERSION + m.group(2), source)
    if count != 3:
        raise ValueError(f'Expected 3 app container version labels, got {count}')
    results[path] = updated
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true', help='Fail if generated files are out of sync.')
    action.add_argument('--write', action='store_true', help='Update generated version copies.')
    action.add_argument('--print', dest='print_version', action='store_true', help='Print canonical version.')
    args = parser.parse_args()
    if args.print_version:
        print(VERSION)
        return
    changed = []
    for path, expected in expected_files().items():
        current = path.read_text()
        if current != expected:
            changed.append(path.relative_to(ROOT).as_posix())
            if args.write:
                path.write_text(expected)
    if args.check and changed:
        parser.exit(1, 'FAIL: stale generated version metadata: ' + ', '.join(changed) +
                    '; run python3 scripts/sync_release_version.py --write\n')
    print(f'PASS: release version {VERSION} synchronized' if not changed else
          f'Updated release version {VERSION}: ' + ', '.join(changed))


if __name__ == '__main__':
    main()
