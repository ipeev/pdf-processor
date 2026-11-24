"""Offline contract checks for local Dependabot upgrade recipe (no network needed)."""
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import json

ROOT = Path(__file__).resolve().parents[2]

def test_dependency_upgrade_manifest_is_explicit_and_reproducible(tmp_path):
    for rel in ('scripts/prepare_dependency_upgrades.py', 'backend/pyproject.toml',
                'frontend/package.json', 'frontend/Dockerfile',
                '.github/workflows/ci.yml', 'frontend/tests/ui.test.mjs'):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    tool = tmp_path / 'scripts/prepare_dependency_upgrades.py'
    # The same regression suite must also pass *after* the local upgrade
    # regenerates its locks; an already-upgraded tree must not silently apply
    # the migration a second time.
    before = (tmp_path/'backend/pyproject.toml').read_text()
    already_upgraded = 'redis==8.1.0' in before
    result = subprocess.run([sys.executable, str(tool), '--apply'], text=True, capture_output=True)
    if already_upgraded:
        assert result.returncode != 0, 'reapplying an upgrade must fail explicitly'
    else:
        assert result.returncode == 0, result.stderr
    project = tomllib.loads((tmp_path/'backend/pyproject.toml').read_text())['project']
    assert 'redis==8.1.0' in project['dependencies']
    assert 'pypdf==7.1.1' in project['dependencies']
    assert 'reportlab==5.1.0' in project['optional-dependencies']['test']
    npm = json.loads((tmp_path/'frontend/package.json').read_text())
    assert npm['dependencies']['katex'] == '^0.19.1'
    assert npm['devDependencies']['vite'] == '^8.3.8'
    assert npm['devDependencies']['@vitejs/plugin-react'] == '^6.3.0'
    docker = (tmp_path/'frontend/Dockerfile').read_text()
    assert 'FROM node:26-bookworm-slim' in docker
    assert 'FROM nginx:1.31-alpine' in docker
    workflow = (tmp_path/'.github/workflows/ci.yml').read_text()
    assert "node-version: '26'" in workflow
    assert "assert.equal(manifest.dependencies.katex, '^0.19.1')" in (tmp_path/'frontend/tests/ui.test.mjs').read_text()


def test_upgrade_runner_never_merges_or_runs_paid_tests():
    runner = (ROOT/'scripts/upgrade_dependency_stack.sh').read_text()
    assert 'git merge' not in runner
    assert 'git status' not in runner
    assert 'git rev-parse' not in runner
    assert 'tar -czf' in runner  # backup exists without requiring Git
    assert 'OPENROUTER_API_KEY=''' in runner
    assert './scripts/test_release.sh --offline' in runner
    assert './scripts/check_regressions.sh' in runner
    assert 'npm install --package-lock-only' in runner
    assert 'lock --python /usr/local/bin/python3.15' in runner


def test_dependency_change_cannot_pass_with_old_uv_lock(tmp_path):
    for rel in ('scripts/prepare_dependency_upgrades.py', 'scripts/check_release_candidate.py',
                'backend/pyproject.toml', 'backend/uv.lock',
                'frontend/package.json', 'frontend/package-lock.json',
                'frontend/Dockerfile', 'frontend/tests/ui.test.mjs',
                '.github/workflows/ci.yml', 'docker-compose.yml'):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    # Deliberately make the Python requirements inconsistent with the lock,
    # independent of whether the current tree is pre- or post-upgrade.
    project = tmp_path / 'backend/pyproject.toml'
    data = project.read_text()
    if 'redis==8.1.0' in data:
        data = data.replace('redis==8.1.0', 'redis>=5,<7', 1)
    else:
        assert 'redis>=5,<7' in data
        data = data.replace('redis>=5,<7', 'redis==8.1.0', 1)
    project.write_text(data)
    checker = tmp_path / 'scripts/check_release_candidate.py'
    result = subprocess.run([sys.executable, str(checker), '--runtime-only'],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'regenerate lock with uv' in result.stderr


def test_backend_image_contains_ci_workflow_for_dependency_upgrade_contract():
    # The image runs all Python contract tests, including tests of the CI
    # update tool; it needs the public CI file but not public documentation.
    dockerfile = (ROOT / 'backend/Dockerfile').read_text()
    assert 'COPY .github/workflows/ci.yml ./.github/workflows/ci.yml' in dockerfile


def test_local_dependency_upgrade_has_safe_resume_without_publication_scan():
    runner = (ROOT/'scripts/upgrade_dependency_stack.sh').read_text()
    assert '--resume' in runner
    assert 'if [[ "$mode" == \'resume\' ]]' in runner
    assert 'run_offline_gates' in runner
    # It must not run the public-release scanner against a local .env.
    assert not any(line.strip() == 'python3 scripts/check_publication.py'
                   for line in runner.splitlines())
    # Source CI must retain strict public-source scanning.
    workflow = (ROOT/'.github/workflows/ci.yml').read_text()
    assert 'python3 scripts/check_publication.py' in workflow


def test_release_upgraded_dependency_locks_match_targets():
    """Shipping locks must include the actual eight requested upgrades, not only manifest edits."""
    python_lock = tomllib.loads((ROOT / 'backend/uv.lock').read_text())
    py_versions = {p['name']: p['version'] for p in python_lock['package']}
    assert {name: py_versions[name] for name in ('redis', 'pypdf', 'reportlab')} == {
        'redis': '8.1.0', 'pypdf': '7.1.1', 'reportlab': '5.1.0',
    }
    npm_lock = json.loads((ROOT / 'frontend/package-lock.json').read_text())
    npm_versions = {name: meta.get('version') for name, meta in npm_lock['packages'].items()}
    assert {name: npm_versions['node_modules/' + name] for name in (
        'vite', '@vitejs/plugin-react', 'katex')} == {
        'vite': '8.3.8', '@vitejs/plugin-react': '6.3.0', 'katex': '0.19.1',
    }
    package = json.loads((ROOT / 'frontend/package.json').read_text())
    assert package['dependencies'] == npm_lock['packages']['']['dependencies']
    assert package['devDependencies'] == npm_lock['packages']['']['devDependencies']
    dockerfile = (ROOT / 'frontend/Dockerfile').read_text()
    assert 'FROM node:26-bookworm-slim AS builder' in dockerfile
    assert 'FROM nginx:1.31-alpine' in dockerfile
    workflow = (ROOT / '.github/workflows/ci.yml').read_text()
    assert "node-version: '26'" in workflow
