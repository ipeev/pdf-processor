"""v0.9.0: independent loop per thread; bounded restart and shutdown."""
import asyncio
from collections import Counter
import json
import signal
import threading
import time

import httpx
import pytest

from app.config import Settings
from app.cache import fingerprint
from app.schemas import ParserChoice
from app.openrouter_client import OpenRouterClient
from app.system_monitor import _worker_states, publish_worker_state
from app.worker import WorkerSupervisor
from tests.fake_redis import FakeRedis


def test_supervisor_runs_independent_threads_and_event_loops():
    settings = Settings(_env_file=None, worker_threads=5)
    identities = []
    lock = threading.Lock()

    async def run_instance(_settings, stop_event, _register):
        with lock:
            identities.append((threading.get_ident(), id(asyncio.get_running_loop())))
        while not stop_event.is_set():
            await asyncio.sleep(.01)

    supervisor = WorkerSupervisor(settings, runner=run_instance)
    originals = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    timer = threading.Timer(.3, supervisor.request_shutdown)
    timer.start()
    try:
        supervisor.run()
    finally:
        timer.cancel()
        timer.join()
    assert len(identities) == 5
    assert len({item[0] for item in identities}) == 5
    assert len({item[1] for item in identities}) == 5
    assert not any(t.is_alive() for t in supervisor._threads.values())
    assert {s: signal.getsignal(s) for s in originals} == originals


def test_supervisor_restarts_failed_instance_without_interrupting_others():
    settings = Settings(_env_file=None, worker_threads=2)
    started = Counter()
    lock = threading.Lock()

    async def run_instance(_settings, stop_event, _register):
        name = threading.current_thread().name
        with lock:
            started[name] += 1
            iteration = started[name]
        if name == 'pdf-worker-1' and iteration == 1:
            raise RuntimeError('simulated one-thread failure')
        while not stop_event.is_set():
            await asyncio.sleep(.02)

    supervisor = WorkerSupervisor(settings, runner=run_instance)
    timer = threading.Timer(2.7, supervisor.request_shutdown)
    timer.start()
    try:
        supervisor.run()
    finally:
        timer.cancel()
        timer.join()
    assert started['pdf-worker-1'] >= 2
    assert started['pdf-worker-2'] == 1


@pytest.mark.asyncio
async def test_monitor_includes_40_distinct_threads_and_groups():
    redis = FakeRedis()
    for index in range(40):
        group = '1234abcd' if index < 20 else '89abcdef'
        await publish_worker_state(redis, f'worker-{index:02d}-{index:08x}',
                                   'extracting' if index % 2 else 'idle',
                                   'gemini', 5, container=group)
    found = await _worker_states(redis)
    assert len(found) == 40
    assert Counter(item['container'] for item in found) == {'1234abcd': 20, '89abcdef': 20}
    assert sum(item['state'] == 'extracting' for item in found) == 20


@pytest.mark.asyncio
async def test_each_model_role_is_independently_configured():
    settings = Settings(_env_file=None, openrouter_api_key='test-only',
                        native_pdf_model='vendor/native', image_ocr_model='vendor/image',
                        summary_model='vendor/summary')
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'finish_reason':'stop', 'message':{'content':'ok'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = OpenRouterClient(settings, client)
        await gateway.complete_pdf_page(b'%PDF example', page=1, max_tokens=100)
        await gateway.complete_image_page(b'png example', page=1, max_tokens=100)
        await gateway.complete_text('test summary', max_tokens=100)
    assert [call['model'] for call in calls] == ['vendor/native', 'vendor/image', 'vendor/summary']
    baseline = fingerprint('a' * 64, ParserChoice.gemini, settings)
    other = settings.model_copy(update={'summary_model': 'vendor/other-summary'})
    assert baseline != fingerprint('a' * 64, ParserChoice.gemini, other)
    other = settings.model_copy(update={'native_pdf_model': 'vendor/other-native'})
    assert baseline != fingerprint('a' * 64, ParserChoice.gemini, other)


def test_legacy_model_env_aliases_remain_supported(monkeypatch):
    monkeypatch.delenv("NATIVE_PDF_MODEL", raising=False)
    monkeypatch.delenv("IMAGE_OCR_MODEL", raising=False)
    monkeypatch.setenv('GEMINI_MODEL', 'legacy/native')
    monkeypatch.setenv('GPT_MODEL', 'legacy/image')
    settings = Settings(_env_file=None)
    assert settings.native_pdf_model == 'legacy/native'
    assert settings.image_ocr_model == 'legacy/image'
    monkeypatch.setenv('NATIVE_PDF_MODEL', 'modern/native')
    assert Settings(_env_file=None).native_pdf_model == 'modern/native'
