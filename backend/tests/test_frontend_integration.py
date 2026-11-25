"""Minimal frontend/Compose wiring contract tests; no Node/npm or Docker daemon required."""
import importlib.util
from pathlib import Path
from unittest.mock import patch
import subprocess
import re

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_frontend_files_are_present():
    expected = [
        'frontend/package.json', 'frontend/Dockerfile', 'frontend/nginx.conf',
        'frontend/src/App.tsx', 'frontend/src/contracts.ts', 'frontend/src/main.tsx',
        'frontend/src/styles.css', 'frontend/tests/contracts.test.mjs',
        'scripts/lock_frontend.sh', 'scripts/check_frontend.py', 'scripts/check_frontend.sh',
    ]
    assert all((ROOT / f).is_file() for f in expected)


def test_docker_compose_starts_frontend_on_localhost():
    compose = (ROOT / 'docker-compose.yml').read_text()
    assert '  frontend:' in compose
    assert 'dockerfile: frontend/Dockerfile' in compose
    assert '127.0.0.1:3000:80' in compose
    assert '127.0.0.1:8000:8000' in compose


def test_nginx_proxies_same_origin_api_and_upload_size():
    nginx = (ROOT / 'frontend/nginx.conf').read_text()
    assert 'location /api/' in nginx
    assert 'proxy_pass http://api:8000;' in nginx
    assert 'client_max_body_size 60m;' in nginx
    assert 'try_files $uri $uri/ /index.html;' in nginx


def test_frontend_secret_never_bundled():
    frontend = ROOT / 'frontend'
    source = '\n'.join(p.read_text() for p in (frontend / 'src').glob('*') if p.is_file())
    assert 'OPENROUTER_API_KEY' not in source
    assert 'https://openrouter.ai' not in source
    assert 'fetch(`/api/v1/documents/' in source
    assert "fetch('/api/v1/documents'" in source


def test_node_build_and_frontend_tests_are_part_of_image():
    dockerfile = (ROOT / 'frontend/Dockerfile').read_text()
    # The lock generator resolves the build image from the Dockerfile itself;
    # it must work for the selected Node build image without a version literal.
    assert re.search(r'^FROM node:[0-9]+-bookworm-slim AS builder$', dockerfile, re.MULTILINE)
    lock_script = (ROOT / 'scripts/lock_frontend.sh').read_text()
    assert 'node_image="$(awk' in lock_script
    assert "'/^FROM node:/ {print $2; exit}' frontend/Dockerfile" in lock_script
    assert '"$node_image" npm install --package-lock-only' in lock_script
    assert 'npm ls react react-dom vite rollup' in dockerfile
    assert 'npm ci' in dockerfile
    assert 'npm test && npm run build' in dockerfile
    assert re.search(r'^FROM nginx:[0-9]+\.[0-9]+-alpine$', dockerfile, re.MULTILINE)
    assert 'COPY --from=builder /workspace/dist' in dockerfile


def test_bootstrap_and_verification_include_frontend():
    setup = (ROOT / 'scripts/setup.sh').read_text()
    verify = (ROOT / 'scripts/verify.sh').read_text()
    assert 'scripts/lock_frontend.sh and reverify' in setup
    assert './scripts/check_frontend.sh' in setup
    assert './scripts/check_frontend.sh' in verify


@pytest.mark.parametrize('script', ['lock_frontend.sh', 'check_frontend.sh', 'setup.sh', 'verify.sh'])
def test_bash_scripts_parse(script):
    subprocess.run(['bash', '-n', str(ROOT / 'scripts' / script)], check=True)


def test_frontend_smoke_check_on_mocked_network():
    path = ROOT / 'scripts' / 'check_frontend.py'
    spec = importlib.util.spec_from_file_location('check_frontend', path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url.endswith('/api/v1/system/status'):
            return 200, 'application/json', b'{"redis":{"status":"online"},"rustfs":{"status":"online"},"openrouter":{"status":"not_monitored"},"opensearch":{"status":"online"},"archive":{"status":"online","consistency":"counts_match","recovery":{}}}'
        if url.endswith(('/api/v1/library?limit=1', '/api/v1/runs?limit=1')):
            return 200, 'application/json', b'{"items":[],"next_cursor":null}'
        if url.endswith('/api/v1/ready'):
            return 200, 'application/json', b'{"status":"ready","redis":"ok"}'
        if url.endswith('.js'):
            return 200, 'text/javascript', (
                b'/api/v1/system/status Processing History Document Library Permanent archive '
                b'/api/v1/runs/ /api/v1/library/ Currently processing No active processing jobs'
                + b'x' * 200
            )
        if url.endswith('.css'):
            return 200, 'text/css', b'.app-shell{}.dropzone{}.results-panel{}'
        return 200, 'text/html', b'<title>PDF Processor</title><div id="root"></div><script src="/assets/app.js"></script><link href="/assets/app.css" rel="stylesheet">'

    with patch.object(module, 'fetch', side_effect=fake_fetch):
        module.check('http://frontend')
    assert calls == ['http://frontend/', 'http://frontend/assets/app.js', 'http://frontend/assets/app.css', 'http://frontend/system', 'http://frontend/upload', 'http://frontend/library', 'http://frontend/runs/12345678-1234-1234-1234-123456789abc', 'http://frontend/library/' + 'a' * 64, 'http://frontend/api/v1/ready', 'http://frontend/api/v1/system/status', 'http://frontend/api/v1/library?limit=1', 'http://frontend/api/v1/runs?limit=1']
