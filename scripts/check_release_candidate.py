#!/usr/bin/env python3
"""Offline version/lock verification, plus full-source documentation checks.

By default validate the complete checkout, including docs and CI.
--runtime-only validates the pinned dependency metadata copied into the
minimal backend Docker image, which intentionally excludes public docs.
No downloads, dependency resolution, or provider access are performed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def require(ok: bool, message: str):
    if not ok:
        raise SystemExit(f'FAIL: {message}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-only', action='store_true',
                        help='Check packaged manifests and locks in the minimal Docker image (no public docs).')
    args = parser.parse_args()
    backend = tomllib.loads((ROOT / 'backend/pyproject.toml').read_text())['project']
    uvpath = ROOT / 'backend/uv.lock'
    npmpath = ROOT / 'frontend/package-lock.json'
    require(uvpath.is_file(), 'missing backend/uv.lock')
    require(npmpath.is_file(), 'missing frontend/package-lock.json')
    uv = tomllib.loads(uvpath.read_text())
    require(uv.get('version') == 1 and len(uv.get('package', ())) >= 25, 'unexpected Python lock format')
    require(backend['requires-python'] == '>=3.15,<3.16',
            f"wrong stable Python metadata in {ROOT / 'backend/pyproject.toml'}: "
            f"expected requires-python='>=3.15,<3.16'; "
            f"found version={backend['version']!r}, requires-python={backend['requires-python']!r}. "
            "Check that the running Docker image matches the source release.")
    root = [p for p in uv['package'] if p['name'] == backend['name']]
    require(len(root) == 1 and root[0]['version'] == backend['version'],
            f"wrong stable Python metadata in the running Docker image or source release: expected version={root[0]['version']!r} from uv.lock; found version={backend['version']!r} in pyproject.toml. Check that the running Docker image matches the source release.")
    require(root[0].get('source') == {'editable': '.'}, 'backend root project must be local editable source')
    import re
    def requirement_metadata(spec, extra=None):
        m = re.fullmatch(r'([A-Za-z0-9_-]+)(?:\[([^\]]+)\])?(.*)', spec)
        require(m is not None, f'invalid backend requirement: {spec}')
        result = {'name': m.group(1).lower().replace('_', '-'), 'specifier': m.group(3)}
        if m.group(2):
            result['extras'] = sorted(m.group(2).split(','))
        if extra:
            result['marker'] = f"extra == '{extra}'"
        return result
    expected_requirements = [requirement_metadata(spec) for spec in backend['dependencies']]
    for extra, deps in backend.get('optional-dependencies', {}).items():
        expected_requirements.extend(requirement_metadata(spec, extra=extra) for spec in deps)
    installed_requirements = root[0].get('metadata', {}).get('requires-dist', [])
    normalize = lambda x: json.dumps(x, sort_keys=True)
    require(sorted(map(normalize, expected_requirements)) == sorted(map(normalize, installed_requirements)),
            'backend dependency constraints differ from uv.lock; regenerate lock with uv')
    require(uv['requires-python'] in ('==3.15.*', '>=3.15,<3.16'), 'uv requires-python differs from Python 3.15 minor')
    for pkg in uv['package']:
        for artifact in pkg.get('wheels', []) + ([pkg['sdist']] if 'sdist' in pkg else []):
            require(artifact.get('hash', '').startswith('sha256:'), f"unhashed Python archive for {pkg['name']}")
    frontend = json.loads((ROOT / 'frontend/package.json').read_text())
    npm = json.loads(npmpath.read_text())
    require(npm.get('lockfileVersion') == 3 and len(npm.get('packages', {})) >= 30, 'unexpected npm lock format')
    require(frontend['name'] == npm['name'] == npm['packages']['']['name'], 'frontend root project name differs')
    require(frontend['version'] == npm['version'] == npm['packages']['']['version'] == backend['version'], 'frontend version differs from lock')
    for k in ('dependencies', 'devDependencies'):
        require(frontend[k] == npm['packages'][''].get(k), f'npm root {k} drift')
    for name, meta in npm['packages'].items():
        if name.startswith('node_modules/') and meta.get('resolved', '').startswith('https://'):
            require(meta.get('integrity', '').startswith('sha512-') or meta.get('integrity', '').startswith('sha1-'), f'unhashed npm dependency: {name}')
    # Release labels deliberately change the Compose service configuration
    # on upgrades, triggering container recreation even with cached layers.
    compose = (ROOT / 'docker-compose.yml').read_text()
    import re
    labels = re.findall(r'^\s+org\.opencontainers\.image\.version: *["\']?([^"\'\s]+)',
                        compose, flags=re.MULTILINE)
    require(labels == [backend['version']] * 3,
            f'Compose frontend/api/worker release labels missing or stale: {labels!r}')
    # Only the source checkout contains documentation and workflow definitions.
    # Do not weaken these checks: CI runs this same tool without --runtime-only,
    # and verify.sh validates the full checkout before starting Docker.
    if not args.runtime_only:
        for doc in ('README.md', 'project_overview.md', 'QUICKSTART_LINUX.md',
                    'docs/ARCHITECTURE.md', 'docs/CI_AND_TESTING.md',
                    'docs/OPERATIONS.md', 'docs/FULL_TEXT_SEARCH.md',
                    'docs/PUBLISHING_GITHUB.md'):
            path = ROOT / doc
            require(path.is_file(), f'missing public documentation: {doc}')
        sync = ROOT / 'scripts/sync_release_version.py'
        require(sync.is_file(), 'missing release-version synchronization tool')
        publishing = ROOT / 'docs/PUBLISHING_GITHUB.md'
        workflow = ROOT / '.github/workflows/ci.yml'
        require(workflow.is_file(), 'CI workflow missing from source checkout')
        require('test_release.sh --offline' in workflow.read_text(),
                'CI missing offline release gate')
    print(f"PASS: bundled Python and npm lockfiles match both v{backend['version']} public-release manifests; hashes intact, no dependency resolution.")
    print('Validation:', 'runtime metadata' if args.runtime_only else 'full source + docs + CI')
    print('Locked Python packages:', len(uv['package']), '| Locked npm nodes:', len(npm['packages'])-1)
    print('Python lock SHA256:', hashlib.sha256(uvpath.read_bytes()).hexdigest())
    print('npm lock SHA256:   ', hashlib.sha256(npmpath.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
