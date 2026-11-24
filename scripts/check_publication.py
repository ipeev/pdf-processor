#!/usr/bin/env python3
"""Fail-fast public-repository checks (stdlib only, no network).

Not a substitute for manual source, PDF fixture, history, or licensing review.
The archive is scanned rather than git files so pre-commit and CI both work.
"""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED = {'.git', '.venv', 'venv', 'node_modules', 'dist', 'test-results', '__pycache__', '.pytest_cache'}
TEXT_SUFFIXES = {'.md', '.py', '.ts', '.tsx', '.js', '.mjs', '.sh', '.yml', '.yaml', '.toml', '.json', '.txt', '.env', '.example', '.mmd', '.dot', '.gitignore'}
FORBIDDEN_NAMES = {'.env', 'id_rsa', 'id_ed25519', 'credentials.json', 'secrets.json'}
PRIVATE_MARKERS = ('/Users/', '~/Projects/')
HOME_USER_PATH = re.compile(r'/home/[A-Za-z0-9_.-]+/')
KEY_MARKERS = ('gh' + 'p_', 'github' + '_pat_', '-----BEGIN ' + 'PRIVATE KEY-----')
SHELL_HOST_PATH = re.compile(r'(?m)^[a-z][a-z0-9_.-]+@[a-z0-9.-]+:(?:/|~)')
OPENROUTER_TOKEN = re.compile('sk' + '-or-v1-[A-Za-z0-9_-]{24,}')
ERRORS: list[str] = []

for path in ROOT.rglob('*'):
    if any(part in EXCLUDED for part in path.relative_to(ROOT).parts):
        continue
    if not path.is_file():
        continue
    rel = path.relative_to(ROOT)
    if path.name in FORBIDDEN_NAMES or path.suffix.lower() in {'.pem', '.p12', '.pfx'}:
        ERRORS.append(f'Private file must not be published: {rel}')
        continue
    if path.suffix.lower() not in TEXT_SUFFIXES and not path.name.startswith('.git'):
        continue
    try:
        data = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        continue
    for marker in PRIVATE_MARKERS + KEY_MARKERS:
        # The checker itself contains these fragments as Python string literals.
        if path.name == 'check_publication.py':
            continue
        if marker in data:
            ERRORS.append(f'Potential private data in {rel}: review marker')
            break
    if path.name != 'check_publication.py' and HOME_USER_PATH.search(data):
        ERRORS.append(f'Potential local home directory in {rel}')
    if path.name != 'check_publication.py' and SHELL_HOST_PATH.search(data):
        ERRORS.append(f'Potential private SSH host/path in {rel}')
    # Synthetic short keys deliberately appear in unit tests and redaction rules.
    if path.name != 'check_publication.py' and OPENROUTER_TOKEN.search(data):
        ERRORS.append(f'Potential OpenRouter credential in {rel}')
    if path.name != '.env.example':
        if re.search(r'^OPENROUTER_API_KEY\s*=\s*\S+', data, re.MULTILINE):
            ERRORS.append(f'Nonempty OpenRouter key assignment in {rel}')

# Public documentation only: current-state references and an explicit roadmap.
PUBLIC_DOCS = {
    'README.md', 'project_overview.md', 'QUICKSTART_LINUX.md',
    'CONTRIBUTING.md', 'SECURITY.md', 'benchmarks/README.md',
    'docs/ARCHITECTURE.md', 'docs/CI_AND_TESTING.md',
    'docs/CONFIGURATION.md', 'docs/CONTAINER_SECURITY.md',
    'docs/OPENROUTER_KEY_TELEMETRY.md', 'docs/OPERATIONS.md',
    'docs/PERSISTENT_API.md', 'docs/FULL_TEXT_SEARCH.md', 'docs/VISION_RESPONSE_DIAGNOSTICS.md', 'docs/PUBLISHING_GITHUB.md',
    'docs/ROADMAP.md', 'docs/PAGE_PROGRESS.md',
}
for rel in PUBLIC_DOCS:
    if not (ROOT / rel).is_file():
        ERRORS.append(f'Missing public documentation: {rel}')
for path in ROOT.glob('docs/*.md'):
    rel = path.relative_to(ROOT).as_posix()
    if rel not in PUBLIC_DOCS:
        ERRORS.append(f'Unexpected obsolete documentation: {rel}')
if (ROOT / 'README_HISTORY.md').exists():
    ERRORS.append('Historical README must not ship')

license_file = ROOT / 'LICENSE'
if not license_file.is_file() or not license_file.read_text().startswith('MIT License\n'):
    ERRORS.append('MIT LICENSE file missing')
for rel in PUBLIC_DOCS:
    path = ROOT / rel
    if not path.exists():
        continue
    content = path.read_text(encoding='utf-8')
    for target in re.findall(r'\]\(([^)]+)\)', content):
        url = target.split('#', 1)[0]
        if not url or '://' in url or url.startswith(('mailto:', '#')):
            continue
        if not (path.parent / url).exists():
            ERRORS.append(f'Broken Markdown link in {rel}: {target}')

if ERRORS:
    for error in ERRORS:
        print('FAIL:', error, file=sys.stderr)
    raise SystemExit(1)
print('PASS: MIT license, current-docs set, public-source pattern scan and Markdown links (manual review still required).')
