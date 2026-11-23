"""Stage 7 fault injection and mandatory real isolated-service integration."""
import asyncio
import hashlib
import os
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest

from app.archive_gate import archive_gate
from app.archive_maintenance import ArchiveMaintenance
from app.archive_store import ArchiveStore
from app.cache import page_key, result_key
from app.config import get_settings
from app.document_repository import DocumentRepository
from app.errors import ProcessingError
from app.index_manager import IndexManager
from app.library_api import router
from app.opensearch_client import OpenSearchClient
from app.run_repository import RunRepository
from tests.fake_redis import FakeRedis
from tests.test_library_api import api_harness, expect
from tests.test_persistent_pipeline import Harness


def maintenance(h):
    return ArchiveMaintenance(h.pipeline, h.queue, h.store)


async def exercise_delete(h, data):
    m = maintenance(h)
    docid = hashlib.sha256(data).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        _, a = await h.upload(http, data, 'first.pdf')
        await expect(http, 'DELETE', f'/runs/{a}', 409)
        await h.one()
        _, b = await h.upload(http, data, 'second.pdf')
        await h.one()
        assert h.summary.calls == 1
        assert (await h.docs.get(docid)).preferred_run_id == a
        page = page_key(uuid4().hex)
        await h.redis.set(page, 'test', ex=60)
        await expect(http, 'DELETE', f'/runs/{a}', 204)
        await expect(http, 'DELETE', f'/runs/{a}', 204)
        await expect(http, 'GET', f'/runs/{a}', 404)
        await expect(http, 'GET', f'/documents/{a}', 404)
        assert (await h.docs.get(docid)).preferred_run_id == b
        assert (await expect(http, 'GET', f'/runs/{b}')).json()['status'] == 'completed'
        assert (await expect(http, 'GET', f'/library/{docid}/original')).content == data
        assert not [key async for key in h.docs.archive.iter_keys(f'{docid}/runs/{a}/')]
        assert await h.docs.archive.run_deleted(a)
        assert await h.redis.get(page) is None
        assert await h.redis.get(result_key((await h.pipeline.get(b)).processing_fingerprint)) is None
        assert (await expect(http, 'GET', '/runs')).json()['items'][0]['run_id'] == str(b)
        revision = (await h.docs.get(docid)).revision
        headers = {'If-Match': f'"{revision}"'}
        await expect(http, 'DELETE', f'/library/{docid}', 428)
        await expect(http, 'DELETE', f'/library/{docid}', 412, headers={'If-Match': '"1"'})
        await expect(http, 'DELETE', f'/library/{docid}', 204, headers=headers)
        await expect(http, 'DELETE', f'/library/{docid}', 204, headers=headers)
        await expect(http, 'GET', f'/library/{docid}', 404)
        await expect(http, 'GET', f'/runs/{b}', 404)
        assert not [key async for key in h.docs.archive.iter_keys(f'{docid}/')]
        assert (await expect(http, 'GET', '/runs')).json()['items'] == []
        assert (await expect(http, 'GET', '/library')).json()['items'] == []
        assert (await m.recover())['runs'] == 0
        fresh, c = await h.upload(http, data, 'reupload.pdf')
        assert fresh['cache'] == 'miss' and c not in (a,b)
        await h.one()
        assert (await h.docs.get(docid)).revision > revision + 1
        await expect(http, 'DELETE', f'/library/{docid}', 412, headers=headers)
        assert (await expect(http, 'GET', f'/runs/{c}')).json()['status'] == 'completed'
        assert h.summary.calls == 2


@pytest.mark.asyncio
async def test_delete_preference_reupload_and_idempotency(api_harness, sample_basic):
    h, _ = api_harness
    await exercise_delete(h, sample_basic.read_bytes())


@pytest.mark.asyncio
async def test_delete_last_run_keeps_original_no_preference(api_harness, sample_basic):
    h, _ = api_harness
    data = sample_basic.read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        _, a = await h.upload(http, data, 'only.pdf')
        await h.one()
        await expect(http, 'DELETE', f'/runs/{a}', 204)
        docid = hashlib.sha256(data).hexdigest()
        record = await h.docs.get(docid)
        assert record.preferred_run_id is None and record.page_count is None
        assert (await expect(http, 'GET', f'/library/{docid}/original')).content == data


@pytest.mark.asyncio
async def test_interrupted_delete_resumes_and_admission_blocked(api_harness, sample_basic, monkeypatch):
    h, _ = api_harness
    m = maintenance(h)
    data = sample_basic.read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        _, a = await h.upload(http, data, 'one.pdf')
        await h.one()
        remove = h.docs.archive.remove_key
        async def broken(key):
            if key.endswith('/metadata.json'):
                raise ProcessingError('ARCHIVE_UNAVAILABLE', 'Injected failure', 503)
            await remove(key)
        monkeypatch.setattr(h.docs.archive, 'remove_key', broken)
        await expect(http, 'DELETE', f'/runs/{a}', 503)
        assert await m.journal() is not None
        response = await http.post('/api/v1/documents', files={'files': ('two.pdf',data,'application/pdf')}, data={'parser':'pypdf'})
        assert response.status_code == 503 and response.json()['error']['code']=='ARCHIVE_DELETION_PENDING'
        monkeypatch.setattr(h.docs.archive, 'remove_key', remove)
        await m.recover()
        assert await m.journal() is None
        await expect(http, 'GET', f'/runs/{a}', 404)
        assert h.summary.calls == 1


async def exercise_recovery(h, data, monkeypatch):
    m = maintenance(h)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        _, done = await h.upload(http, data, 'completed.pdf')
        await h.one()
        existing = await h.pipeline.get(done)
        _, partial = await h.upload(http, data+b'\n% partial\n', 'checkpoint.pdf')
        write = h.docs.archive.write_run_artifact
        async def unavailable(*args, **kwargs):
            if args[2] == 'summary.md':
                raise ProcessingError('ARCHIVE_UNAVAILABLE', 'Injected summary write failure', 503)
            return await write(*args, **kwargs)
        monkeypatch.setattr(h.docs.archive, 'write_run_artifact', unavailable)
        with pytest.raises(ProcessingError, match='ARCHIVE_UNAVAILABLE'):
            await h.one()
        monkeypatch.setattr(h.docs.archive, 'write_run_artifact', write)
        checkpoint = await h.pipeline.get(partial)
        assert checkpoint.has_extraction and checkpoint.status == 'summarizing'
        _, queued = await h.upload(http, data+b'\n% queued\n','queued.pdf')
        with pytest.raises(ProcessingError, match='ARCHIVE_BUSY'):
            await m.recover()
        # Simulate total loss of the dedicated test queue/coordination keys,
        # not an actual restart/flush of shared production Redis.
        if isinstance(h.redis, FakeRedis):
            h.redis.streams.pop(h.queue.stream, None)
            h.redis.groups.pop((h.queue.stream,h.queue.group),None)
        else:
            await h.redis.delete(h.queue.stream)
        for uid in (done, partial, queued):
            await h.forget(uid)
        await h.queue.ensure_group()
        calls = h.summary.calls
        assert (await m.recover())['recovered'] == 2
        assert h.summary.calls == calls
        assert await h.pipeline.get(done) == existing
        p = await h.pipeline.get(partial)
        assert p.status=='partial' and p.extraction_ref == checkpoint.extraction_ref and p.error_code=='PROCESSING_INTERRUPTED'
        q = await h.pipeline.get(queued)
        assert q.status=='failed' and not q.has_extraction
        await expect(http, 'POST', f'/runs/{partial}/retry-summary', 202)
        await h.one()
        assert (await h.pipeline.get(partial)).status=='completed'
        assert h.summary.calls == calls + 1
        # Immutable generation unreachable from canonical manifest is GC eligible.
        garbage = uuid4().hex
        await h.docs.archive.write_run_artifact(existing.document_id, done, 'summary.md', b'orphan', generation=garbage)
        assert (await m.recover(cleanup=True))['orphan_objects']==1
        assert await h.docs.archive.optional(h.docs.archive._artifact_key(existing.document_id,done,'summary.md',garbage)) is None
        assert await h.pipeline.read_summary(existing)


@pytest.mark.asyncio
async def test_lost_queue_recovery_and_orphan_gc(api_harness, sample_basic, monkeypatch):
    h, _ = api_harness
    await exercise_recovery(h, sample_basic.read_bytes(), monkeypatch)


@pytest.mark.asyncio
async def test_lock_is_cross_process_and_released(tmp_path):
    lock = tmp_path/'archive.lock'
    code = 'import fcntl,sys; f=open(sys.argv[1],"a"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
    async with archive_gate(path=lock):
        async with archive_gate(path=lock):
            with pytest.raises(ProcessingError, match='ARCHIVE_BUSY'):
                async with archive_gate(path=lock, exclusive=True):
                    pass
            assert subprocess.run([sys.executable,'-c',code,str(lock)],capture_output=True).returncode != 0
    assert subprocess.run([sys.executable,'-c',code,str(lock)],capture_output=True).returncode == 0


@pytest.mark.asyncio
async def test_stale_index_pruned(api_harness, sample_basic):
    from app.catalogue_models import RunManifest, DocumentManifest
    h, _ = api_harness
    docid = 'e'*64
    run_id = uuid4()
    doc = DocumentManifest(document_id=docid,sha256=docid,filename='stale.pdf',filenames=('stale.pdf',),file_size_bytes=1,s3_bucket=h.docs.archive.bucket,s3_object_key=f'{docid}/original.pdf')
    run = RunManifest(run_id=run_id,document_id=docid,filename='stale.pdf',parser='pypdf',pipeline_version='test',application_version='test',s3_prefix=f'{docid}/runs/{run_id}/')
    await h.docs.indexes.publish(doc)
    await h.docs.indexes.publish(run)
    assert (await maintenance(h).recover())['stale_index_records']==2
    assert await h.docs.archive.run_deleted(run_id)
    assert await h.docs.archive.document_revision_floor(docid)==2


@pytest.mark.pipeline_integration
@pytest.mark.asyncio
async def test_real_archive_maintenance(settings, sample_basic, monkeypatch):
    # Dedicated bucket, index namespace and otherwise empty Redis test DB 15.
    # Fail closed rather than flush any existing Redis DB or user archive.
    from redis.asyncio import Redis
    from redis.exceptions import RedisError
    real = get_settings()
    url = urlsplit(real.redis_url)
    redis = Redis.from_url(urlunsplit(url._replace(path='/15')),decode_responses=True,socket_timeout=3,socket_connect_timeout=3)
    ident = uuid4().hex
    store = ArchiveStore(real.model_copy(update={'s3_archive_bucket':'pdf-test-'+ident}))
    search = OpenSearchClient(real.opensearch_url)
    indexes = IndexManager(search,prefix='pdf-test-'+ident)
    docs = DocumentRepository(store,indexes)
    h = Harness(settings,docs,RunRepository(store,indexes,docs),redis)
    h.app.include_router(router)
    owns_db = False
    created = False
    try:
        try:
            await redis.ping()
            await search.request('GET','/_cluster/health')
            await store.prepare()
            created = True
        except (RedisError,ProcessingError,httpx.HTTPError):
            pytest.skip('Real Redis, RustFS, and OpenSearch required')
        assert await redis.dbsize()==0, 'Redis DB 15 reserved for isolated Stage 7 gate must be empty'
        owns_db = True
        await indexes.ensure()
        await h.queue.ensure_group()
        data = sample_basic.read_bytes()+f'\n% stage7-{ident}\n'.encode()
        await exercise_delete(h,data)
        await exercise_recovery(h,data+b'\n% recovery\n',monkeypatch)
    finally:
        if owns_db:
            # Only keys in this verified initially empty, dedicated test DB.
            while await redis.dbsize():
                cursor=0
                while True:
                    cursor, keys = await redis.scan(cursor=cursor,count=500)
                    if keys:
                        await redis.delete(*keys)
                    if not cursor:
                        break
        if created:
            for key in [k async for k in store.iter_keys()]:
                await store.remove_key(key)
            await asyncio.to_thread(store.client.delete_bucket,Bucket=store.bucket)
            for kind in ('documents','runs'):
                name,_=indexes.names(kind)
                try:
                    await search.request('DELETE',f'/{name}')
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
        await redis.aclose()
        await search.aclose()
        store.client.close()


@pytest.mark.asyncio
async def test_recovery_loop_retries_busy_and_detects_redis_epoch():
    from app.archive_maintenance import recovery_loop
    class Service:
        epoch = 'one'
        dirty = False
        events = []
        blocked = True
        async def info(self, section): return {'run_id': self.epoch}
        async def deletion_journal(self): return None
        async def reconciliation_needed(self): return self.dirty
        async def recover(self):
            if self.blocked:
                self.blocked = False
                raise ProcessingError('ARCHIVE_BUSY','busy',409)
            self.events.append(self.epoch)
            self.dirty = False
            return {'recovered': 0}
    service = Service()
    service.archive = service.redis = service
    task = asyncio.create_task(recovery_loop(service, interval=.001))
    async def until(n):
        async with asyncio.timeout(1):
            while len(service.events)<n: await asyncio.sleep(.001)
    try:
        await until(1)
        service.epoch = 'two'
        await until(2)
        service.dirty = True
        await until(3)
        assert service.events == ['one','two','two']
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
