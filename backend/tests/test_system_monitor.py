"""Phase 5.5: monitoring is read-only, bounded, and never disrupts processing."""
from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api import router
from app.main import app
from app.queue import DocumentQueue
from app.object_store import S3ObjectStore
from app.redis_store import DocumentRepository
from app.system_monitor import (
    CACHE_COUNTER_KEY, WORKER_KEY_PREFIX, publish_worker_state,
    record_cache_outcome, remove_worker_state, system_snapshot,
)
from tests.fake_redis import FakeRedis
from tests.test_phase3_object_store import FakeS3Client


@pytest.fixture
def local_dashboard(settings):
    redis = FakeRedis()
    queue = DocumentQueue(redis, "monitor:test:stream", "monitor:test:group", "worker-test-1234abcd")
    fake_s3 = FakeS3Client()
    fake_s3.bucket_exists = True
    with patch("app.object_store.boto3.client", return_value=fake_s3):
        store = S3ObjectStore(settings)
    app.state.redis = redis
    app.state.queue = queue
    app.state.repository = DocumentRepository(redis, "monitor:test:docs")
    app.state.object_store = store
    app.state.settings = settings
    app.state.openrouter_monitor = None
    return redis, queue, store, fake_s3


def enable_s3_list(fake_s3):
    def list_objects_v2(*, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        keys = sorted(key for key in fake_s3.objects if key.startswith(Prefix))
        offset = int(ContinuationToken) if ContinuationToken else 0
        selected = keys[offset:offset + MaxKeys]
        response = {"Contents": [{"Key": key, "Size": len(fake_s3.objects[key])} for key in selected],
                    "IsTruncated": offset + MaxKeys < len(keys)}
        if response["IsTruncated"]:
            response["NextContinuationToken"] = str(offset + MaxKeys)
        return response
    fake_s3.list_objects_v2 = list_objects_v2


@pytest.mark.asyncio
async def test_native_redis_s3_and_worker_snapshot(local_dashboard):
    redis, queue, store, s3 = local_dashboard
    enable_s3_list(s3)
    s3.objects["uploads/test.pdf"] = b"%PDF sample"
    s3.objects["not-uploads/keep"] = b"ignore"
    await queue.ensure_group()
    await queue.redis.xadd(queue.stream, {"document_id": str(uuid4()), "parser": "pypdf"})
    await record_cache_outcome(redis, 0)
    await record_cache_outcome(redis, 2)
    await record_cache_outcome(redis, 1)
    await publish_worker_state(redis, queue.consumer, "extracting", "gpt5_mini", 5)
    view = await system_snapshot(redis, store, queue)
    assert view["api"]["status"] == "online"
    assert view["redis"]["status"] == "online"
    assert view["redis"]["queue"] == {"waiting": 1, "pending": 0}
    assert view["redis"]["cache"] == {"hits": 1, "misses": 1, "shared": 1}
    assert view["redis"]["workers"] == [{"id": "1234abcd", "state": "extracting", "parser": "gpt5_mini", "container": None}]
    assert view["rustfs"] == {"status": "online", "objects": 1, "bytes": 11, "truncated": False}
    assert view["openrouter"]["status"] == "not_monitored"
    assert view["openrouter"]["privacy_policy"] == "not_verifiable_by_key_api"
    await remove_worker_state(redis, queue.consumer)
    assert (await system_snapshot(redis, store, queue))["redis"]["workers"] == []


@pytest.mark.asyncio
async def test_endpoint_is_same_origin_read_only_and_no_secrets(local_dashboard):
    redis, queue, store, s3 = local_dashboard
    enable_s3_list(s3)
    await queue.ensure_group()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/system/status")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["redis"]["status"] == "online"
    assert "access_key" not in str(body).lower()
    assert "secret" not in str(body).lower()
    assert "document_id" not in str(body).lower()
    assert body["openrouter"]["status"] == "not_monitored"
    assert body["openrouter"]["privacy_policy"] == "not_verifiable_by_key_api"


@pytest.mark.asyncio
async def test_partial_failure_does_not_hide_other_services(local_dashboard):
    redis, queue, store, s3 = local_dashboard
    enable_s3_list(s3)
    redis.available = False
    result = await system_snapshot(redis, store, queue)
    assert result["redis"]["status"] == "unavailable"
    assert result["rustfs"]["status"] == "online"
    assert result["redis"]["queue"]["waiting"] is None
    redis.available = True
    def broken(**_):
        raise RuntimeError('storage unavailable')
    s3.list_objects_v2 = broken
    result = await system_snapshot(redis, store, queue)
    assert result["redis"]["status"] == "online"
    assert result["rustfs"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_expiring_worker_heartbeat(local_dashboard):
    redis, queue, store, s3 = local_dashboard
    enable_s3_list(s3)
    await publish_worker_state(redis, queue.consumer, "idle", None, 5)
    assert await redis.ttl(f"{WORKER_KEY_PREFIX}{queue.consumer}") > 0
    now = redis.clock()
    redis.clock = lambda: now + 21
    assert (await system_snapshot(redis, store, queue))["redis"]["workers"] == []


@pytest.mark.asyncio
async def test_telemetry_write_failure_is_best_effort(local_dashboard, monkeypatch):
    redis, queue, store, s3 = local_dashboard
    redis.available = False
    async def offline(*args, **kwargs):
        raise RedisConnectionError('simulated telemetry outage')
    monkeypatch.setattr(redis, 'set', offline)
    monkeypatch.setattr(redis, 'hincrby', offline)
    await record_cache_outcome(redis, 0)
    await publish_worker_state(redis, queue.consumer, 'extracting', 'pypdf', 5)
    assert await redis.get(CACHE_COUNTER_KEY) is None


@pytest.mark.asyncio
async def test_paginated_s3_object_counts(local_dashboard):
    redis, queue, store, s3 = local_dashboard
    enable_s3_list(s3)
    s3.objects = {f'uploads/{i:04}.pdf': b'abcdef' for i in range(1201)}
    stats = await store.statistics()
    assert stats == {'objects': 1201, 'bytes': 1201 * 6, 'truncated': False}


@pytest.mark.redis_integration
@pytest.mark.asyncio
async def test_real_redis_monitoring_contract(real_redis, settings):
    """Exercises INFO, DBSIZE, XINFO, SCAN, heartbeat expiry and counters on real Redis."""
    token = uuid4().hex
    stream = f'monitor:integration:{token}:jobs'
    group = f'monitor:integration:{token}:group'
    consumer = f'monitor-integration-{token[:8]}'
    queue = DocumentQueue(real_redis, stream, group, consumer)
    await queue.ensure_group()
    class Store:
        async def statistics(self):
            return {'objects': 0, 'bytes': 0, 'truncated': False}
    try:
        await publish_worker_state(real_redis, consumer, 'summarizing', 'gemini', 5)
        data = await system_snapshot(real_redis, Store(), queue)
        assert data['redis']['status'] == 'online'
        # Read Redis' effective limit; the full verification gate independently
        # checks the expected deployment policy (1536 MiB).
        configured_limit = await real_redis.config_get("maxmemory")
        assert data['redis']['memory_limit_bytes'] == int(configured_limit['maxmemory'])
        assert data['redis']['queue']['pending'] == 0
        assert data['redis']['queue']['waiting'] == 0
        assert data['redis']['workers']
        # The worker set is a bounded sample, but this specific key must exist.
        assert await real_redis.get(f'{WORKER_KEY_PREFIX}{consumer}')
        assert data['rustfs']['status'] == 'online'
    finally:
        await real_redis.delete(stream)
        await remove_worker_state(real_redis, consumer)

@pytest.mark.asyncio
async def test_real_worker_transitions_extraction_summary_idle(redis_fake, settings, sample_basic):
    """Report genuine metadata transitions while existing processing remains unchanged."""
    import asyncio
    import json
    from pathlib import Path
    from app.worker import DocumentWorker
    from app.document_processor import DocumentProcessor
    from app.schemas import ParserChoice
    from app.storage import StagedFile

    queue = DocumentQueue(redis_fake, 'monitor:worker:transitions:queue', 'transitions', 'worker-transition-aaaa1111')
    repo = DocumentRepository(redis_fake, 'monitor:transition:docs')
    await queue.ensure_group()
    doc_id = uuid4()
    file_path = settings.upload_dir / f'{doc_id}.pdf'
    file_path.write_bytes(sample_basic.read_bytes())
    doc = StagedFile(doc_id, 'example.pdf', ParserChoice.pypdf, file_path, file_path, file_path.stat().st_size)
    await queue.enqueue_batch([doc], repo)
    entered_summary = asyncio.Event()
    release_summary = asyncio.Event()

    class WaitingSummary:
        async def summarize(self, pages):
            entered_summary.set()
            await release_summary.wait()
            return 'Verified summary'

    worker = DocumentWorker(settings, queue, repo,
                            DocumentProcessor(settings, repo, summarizer=WaitingSummary()))
    job = (await queue.read('>'))[0]
    task = asyncio.create_task(worker.handle_message(*job))
    try:
        await asyncio.wait_for(entered_summary.wait(), timeout=2)
        await worker._publish_monitor()
        payload = json.loads(await redis_fake.get(f'{WORKER_KEY_PREFIX}{queue.consumer}'))
        assert payload['state'] == 'summarizing' and payload['parser'] == 'pypdf'
        assert len(payload['container']) == 8
    finally:
        release_summary.set()
        await asyncio.wait_for(task, timeout=2)
    idle = json.loads(await redis_fake.get(f'{WORKER_KEY_PREFIX}{queue.consumer}'))
    assert idle['state'] == 'idle' and idle['parser'] is None
    assert idle['container'] == payload['container']
    assert (await repo.get_meta(doc_id))['status'] == 'completed'
