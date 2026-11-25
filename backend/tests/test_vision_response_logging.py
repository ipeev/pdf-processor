"""Offline diagnostics: preserve full rejected provider output without normal log leakage."""
from __future__ import annotations

import io
import json
import logging

import httpx
import pytest

from app.config import Settings
from app.errors import ProcessingError
from app.observability import JsonFormatter, TextFormatter
from app.openrouter_client import OpenRouterClient, VisionProviderText
from app.vision_debug import log_invalid_vision_response


def output_for(raw: str, *, enabled: bool = True, formatter=None, cause=None):
    config = Settings(_env_file=None, vision_log_invalid_response=enabled)
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(formatter or JsonFormatter())
    log = logging.getLogger('app.vision_debug')
    original = log.handlers[:], log.propagate, log.level
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.ERROR)
    try:
        exc = ProcessingError('VISION_RESPONSE_INVALID', 'Model reply is not valid full-mode JSON.')
        if cause is not None:
            try:
                raise exc from cause
            except ProcessingError as raised:
                exc = raised
        log_invalid_vision_response(config, raw, page=8, attempt=2, exc=exc)
    finally:
        log.handlers, log.propagate, log.level = original
    return buffer.getvalue()


def test_full_model_and_http_response_not_truncated():
    content = '\n```json\n' + 'not-json-' * 850 + '\n```\n'  # > 2,000 chars
    envelope = json.dumps({'choices': [{'finish_reason': 'stop',
                                         'message': {'content': content}}], 'usage': {'total_tokens': 100}})
    response = VisionProviderText(content, http_response=envelope,
                                  finish_reason='stop', model='test/model')
    cause = json.JSONDecodeError('Expecting value', content, 0)
    entry = json.loads(output_for(response, cause=cause))
    assert entry['event'] == 'vision.response.invalid.raw'
    assert entry['page'] == 8 and entry['vision_attempt'] == 2
    assert entry['vision_raw_model_content'] == content
    assert entry['vision_raw_http_response'] == envelope
    assert entry['vision_finish_reason'] == 'stop'
    assert entry['vision_json_decode_error']['line'] == 1
    assert content not in entry['message']


def test_full_raw_response_in_text_mode():
    raw = '\n' + 'X' * 7000 + '\n'
    out = output_for(raw, formatter=TextFormatter())
    assert 'vision_raw_model_content=' + json.dumps(raw, ensure_ascii=False) in out
    assert len(out) > 7000
    assert out.count('\n') == 1  # newline inside content is escaped, not split


def test_disabled_mode_does_not_print_full_content():
    assert output_for('private PDF contents', enabled=False) == ''


def test_only_validation_errors_trigger_full_content():
    original = logging.getLogger('app.vision_debug')
    # Non-vision errors are deliberately ignored regardless of debug setting.
    config = Settings(_env_file=None, vision_log_invalid_response=True)
    log_invalid_vision_response(config, 'private PDF contents', page=1, attempt=1,
                                exc=ProcessingError('PROVIDER_UNAVAILABLE', 'Timeout'))


@pytest.mark.asyncio
async def test_real_client_preserves_exact_response_envelope_without_network():
    content = '```json\n{"markdown":"oops","figures":[]}\n```\n'
    envelope = json.dumps({'model': 'test/vision', 'choices': [
        {'finish_reason': 'stop', 'message': {'content': content}}]})
    def handler(request):
        return httpx.Response(200, text=envelope, headers={'content-type': 'application/json'})
    config = Settings(_env_file=None, openrouter_api_key='test-only-key',
                      vision_figures_model='test/vision', vision_log_invalid_response=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient(config, http)
        result = await client.complete_vision_figures_page(b'\x89PNG\r\n\x1a\n', page=8, max_tokens=1024)
    assert isinstance(result, str)
    assert result == content.strip()
    assert result.provider_model_content == content
    assert result.provider_http_response == envelope
    assert result.provider_finish_reason == 'stop'


@pytest.mark.asyncio
async def test_historical_saved_response_is_available_without_any_provider_call():
    from uuid import uuid4
    from app.vision_response_cli import _raw_response
    from app.archive_store import ArchiveStore, Artifact
    class ArchiveMemory(ArchiveStore):
        def __init__(self, items):
            self.items = items
        async def optional(self, key):
            return Artifact(self.items[key], '"test"') if key in self.items else None
        async def iter_manifest_keys(self):
            for key in self.items:
                if key.endswith('/metadata.json'):
                    yield key
    run=uuid4(); doc='a'*64; stored=b'```json\n{"unclosed": [\n'
    store=ArchiveMemory({f'{doc}/runs/{run}/metadata.json':b'{}',
                         f'{doc}/runs/{run}/vision/responses/p0008.json':stored})
    assert await _raw_response(store,run,8,1,None)==stored
    with pytest.raises(LookupError):
        await _raw_response(store,run,21,1,doc)
