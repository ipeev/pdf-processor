"""No-provider network tests for bounded paid retries and verified page reuse."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.archive_store import ArchiveStore, Artifact
from app.config import Settings
from app.errors import ProcessingError
from app.parsers.vision_figures import decode_page_response


class MemoryArchive(ArchiveStore):
    def __init__(self):
        self.objects = {}

    async def optional(self, key):
        return Artifact(self.objects[key], '"mock"') if key in self.objects else None

    async def _read(self, key):
        return Artifact(self.objects[key], '"mock"')

    async def _write(self, key, data, content_type, expected_etag=None):
        if key in self.objects:
            raise ProcessingError('ARCHIVE_CONFLICT', 'Existing artifact')
        self.objects[key] = data
        return '"mock"'


async def validate(raw):
    markdown, figures = decode_page_response(raw, 8)
    return markdown, figures


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    async def no_sleep(_): pass
    monkeypatch.setattr('app.vision_retry.asyncio.sleep', no_sleep)


@pytest.mark.asyncio
async def test_two_invalid_page_responses_then_valid_and_reused():
    store = MemoryArchive()
    cfg = Settings(_env_file=None, vision_figures_page_attempts=3)
    doc = 'c'*64
    png = b'PNG page 8'
    calls = []
    async def call():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return '```json\n{"markdown": "text", "figures": []}\n```'
        if len(calls) == 2:
            return '{"markdown": "hello [[FIGURE:figure-1]]", "figures": []}'
        return '{"markdown": "OK", "figures": []}'
    first_run = uuid4()
    attempts = []
    async def on_attempt(number):
        attempts.append(number)
    result = await store.durable_vision_page(doc, first_run, 8, png, cfg, call, validate,
                                             on_attempt=on_attempt)
    assert attempts == [1, 2, 3]
    assert result[0] == 'OK'
    assert calls == [1, 2, 3]
    assert store.vision_call_key(doc,first_run,8,response=True,attempt=1) in store.objects
    assert store.vision_call_key(doc,first_run,8,response=True,attempt=2) in store.objects
    assert store.vision_call_key(doc,first_run,8,response=True,attempt=3) in store.objects
    assert await store.durable_vision_page(doc, uuid4(), 8, png, cfg, call, validate) == result
    assert calls == [1, 2, 3], 'a new run must reuse the validated page'
    assert len([k for k in store.objects if '/vision-cache/' in k]) == 1


@pytest.mark.asyncio
async def test_unknown_outcome_is_never_auto_retried():
    store = MemoryArchive()
    cfg = Settings(_env_file=None, vision_figures_page_attempts=3)
    doc='d'*64
    run_id=uuid4()
    calls=[]
    async def fail():
        calls.append(True)
        raise ProcessingError('PROVIDER_UNAVAILABLE','Timeout',diagnostic={'kind':'timeout'})
    with pytest.raises(ProcessingError, match='PROVIDER_UNAVAILABLE'):
        await store.durable_vision_page(doc,run_id,1,b'PNG',cfg,fail,validate)
    with pytest.raises(ProcessingError, match='VISION_CALL_OUTCOME_UNKNOWN'):
        await store.durable_vision_page(doc,run_id,1,b'PNG',cfg,fail,validate)
    assert len(calls)==1


@pytest.mark.asyncio
async def test_old_invalid_reply_is_not_cached():
    store = MemoryArchive()
    cfg = Settings(_env_file=None)
    doc='e'*64
    old_run=uuid4()
    new_run=uuid4()
    key = store.vision_call_key(doc,old_run,1,response=True)
    store.objects[key] = b'NOT JSON'
    calls=[]
    async def replacement():
        calls.append(1)
        return json.dumps({'markdown':'Recovered','figures':[]})
    result = await store.durable_vision_page(doc,new_run,1,b'PNG',cfg,replacement,validate,prior_runs=(old_run,))
    assert result[0]=='Recovered'
    assert calls == [1]


@pytest.mark.asyncio
async def test_cache_key_scoped_to_document_and_input_config():
    store=MemoryArchive()
    cfg=Settings(_env_file=None)
    doc='f'*64
    async def call(): return '{"markdown":"hi","figures":[]}'
    await store.durable_vision_page(doc,uuid4(),1,b'PNG',cfg,call,validate)
    assert store.vision_page_cache_key(doc,b'PNG',cfg) != store.vision_page_cache_key('a'*64,b'PNG',cfg)
    assert store.vision_page_cache_key(doc,b'PNG',cfg) != store.vision_page_cache_key(doc,b'other',cfg)
    assert store.vision_page_cache_key(doc,b'PNG',cfg) != store.vision_page_cache_key(doc,b'PNG',Settings(_env_file=None,vision_figures_max_tokens_per_page=17000))


@pytest.mark.asyncio
async def test_invalid_responses_logged_on_each_failed_attempt(caplog):
    from app.openrouter_client import VisionProviderText
    store=MemoryArchive()
    cfg=Settings(_env_file=None,vision_log_invalid_response=True)
    raw='not valid JSON ' + ('☀' * 4000)
    envelope=json.dumps({'choices':[{'message':{'content':raw},'finish_reason':'stop'}]})
    async def call():
        return VisionProviderText(raw,http_response=envelope,finish_reason='stop',model='mock/model')
    with pytest.raises(ProcessingError, match='VISION_RESPONSE_INVALID'):
        await store.durable_vision_page('b'*64,uuid4(),5,b'PNG',cfg,call,validate)
    records=[r for r in caplog.records if getattr(r,'event','')=='vision.response.invalid.raw']
    assert len(records)==3
    assert [r.vision_attempt for r in records]==[1,2,3]
    assert all(r.vision_raw_model_content==raw and r.vision_raw_http_response==envelope for r in records)
