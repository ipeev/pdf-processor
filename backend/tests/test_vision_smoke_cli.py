"""Paid-test admission is opt-in, with machine-readable failure records."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/check_figures.py'
spec = importlib.util.spec_from_file_location('check_figures', SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_multipart_explicit_parser_only_and_pdf_bytes():
    raw = b'%PDF-1.7\nprobe'
    payload, content_type = smoke.multipart_pdf(raw)
    assert content_type.startswith('multipart/form-data; boundary=')
    assert b'vision_figures' in payload and b'figure-smoke.pdf' in payload
    assert payload.count(raw) == 1
    assert b'OPENROUTER_API_KEY' not in payload


def test_default_health_only_makes_no_upload_calls(monkeypatch):
    requested = []
    def fake_get(base, path, *, status_expected=200):
        requested.append(path)
        return {'status': 'ok' if path == '/health' else 'ready'}
    monkeypatch.setattr(smoke, 'expect_json', fake_get)
    report = {}
    smoke.run(SimpleNamespace(base_url='http://localhost:8000', paid_live=False), report)
    assert requested == ['/health','/ready']
    assert report['mode'] == 'health-only'


def test_live_smoke_rejects_unaccepted_upload_without_retry(monkeypatch, tmp_path):
    pdf = tmp_path / 'safe.pdf'
    pdf.write_bytes(b'%PDF-1.7\nfixture')
    called = []
    def fake_get(base,path, *, status_expected=200):
        return {'status': 'ok' if path == '/health' else 'ready'}
    def fake_request(base,method,path,**kwargs):
        called.append((method,path))
        return 503, b'{"error":{"code":"ARCHIVE_UNAVAILABLE"}}'
    monkeypatch.setattr(smoke, 'expect_json', fake_get)
    monkeypatch.setattr(smoke,'request',fake_request)
    with pytest.raises(smoke.SmokeFailure, match='do not re-upload blindly'):
        smoke.run(SimpleNamespace(base_url='http://localhost:8000/api/v1',
            input=str(pdf), paid_live=True, wait_seconds=10), {})
    assert called == [('POST','/documents')]


def test_invalid_json_response_fails_closed():
    with pytest.raises(smoke.SmokeFailure, match='invalid JSON'):
        smoke.as_json(200, b'{', '/ready')
