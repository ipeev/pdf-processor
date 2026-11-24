#!/usr/bin/env python3
"""Prepare exactly the eight Dependabot upgrades without network access.

Intentionally DOES NOT update lockfiles. Use upgrade_dependency_stack.sh to
resolve real, hashed registry artifacts and run the complete offline test gate.
Run in a disposable local development directory; no Git access is needed.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_UPGRADES = {'redis': ('redis>=5,<7', 'redis==8.1.0'),
                   'pypdf': ('pypdf>=5,<7', 'pypdf==7.1.1'),
                   'reportlab': ('reportlab>=4,<5', 'reportlab==5.1.0')}
NPM_UPGRADES = {'katex': '^0.19.1', '@vitejs/plugin-react': '^6.3.0', 'vite': '^8.3.8'}


def replace_one(text: str, old: str, new: str, label: str) -> str:
    if old == new or text.count(old) != 1:
        raise ValueError(f'{label}: expected exactly one {old!r}, got {text.count(old)}')
    return text.replace(old, new, 1)


def prepare():
    project = ROOT / 'backend/pyproject.toml'
    data = project.read_text()
    for name, (old, new) in PYTHON_UPGRADES.items():
        data = replace_one(data, f'"{old}"', f'"{new}"', name)
    project.write_text(data)

    package = ROOT / 'frontend/package.json'
    data = json.loads(package.read_text())
    for name, spec in NPM_UPGRADES.items():
        key = 'dependencies' if name == 'katex' else 'devDependencies'
        if name not in data[key]:
            raise ValueError(f'missing npm dependency {name}')
        data[key][name] = spec
    package.write_text(json.dumps(data, indent=2) + '\n')

    docker = ROOT / 'frontend/Dockerfile'
    text = docker.read_text()
    text = replace_one(text, 'FROM node:22-bookworm-slim', 'FROM node:26-bookworm-slim', 'Node Docker image')
    text = replace_one(text, 'FROM nginx:1.27-alpine', 'FROM nginx:1.31-alpine', 'Nginx Docker image')
    docker.write_text(text)

    workflow = ROOT / '.github/workflows/ci.yml'
    text = workflow.read_text()
    text = replace_one(text, 'Set up Node.js 22', 'Set up Node.js 26', 'CI step title')
    text = replace_one(text, "node-version: '22'", "node-version: '26'", 'CI Node runtime')
    workflow.write_text(text)

    unit = ROOT / 'frontend/tests/ui.test.mjs'
    text = unit.read_text()
    text = replace_one(text, "assert.equal(manifest.dependencies.katex, '^0.18.11')",
                       "assert.equal(manifest.dependencies.katex, '^0.19.1')", 'KaTeX regression')
    unit.write_text(text)

    print('Prepared: redis 8.1.0, pypdf 7.1.1, reportlab 5.1.0,')
    print('          vite 8.3.8, @vitejs/plugin-react 6.3.0, katex 0.19.1,')
    print('          Node 26 and Nginx 1.31.')
    print('IMPORTANT: source manifests changed; locks are STALE until resolved online.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Modify the checkout (not the lockfiles).')
    args = parser.parse_args()
    if not args.apply:
        parser.error('This is intentionally a modifying operation: pass --apply')
    prepare()

if __name__ == '__main__':
    main()
