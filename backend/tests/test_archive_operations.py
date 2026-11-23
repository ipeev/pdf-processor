"""Stage 8 isolated operational checks, including a real-service gate."""
import asyncio
import hashlib
import io
import json
import zipfile
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
import httpx
import pytest
from app.archive_monitor import ArchiveMonitor, inventory
from app.archive_operations import execute, validate_backup
from app.archive_gate import guarded
from app.archive_store import ArchiveStore
from app.config import get_settings
from app.document_repository import DocumentRepository
from app.errors import ProcessingError
from app.index_manager import IndexManager
from app.library_api import router
from app.opensearch_client import OpenSearchClient
from app.run_repository import RunRepository
from tests.test_library_api import api_harness, expect
from tests.test_archive_maintenance import maintenance
from tests.test_persistent_pipeline import Harness

async def populate(h, data):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        _, first = await h.upload(http, data, 'first.pdf')
        await h.one()
        _, second = await h.upload(http, data, 'alias.pdf')
        await h.one()
    return hashlib.sha256(data).hexdigest(), first, second

async def clear_isolated(m):
    for key in [k async for k in m.archive.iter_keys()]:
        await m.archive.remove_key(key)
    for kind in ('runs','documents'):
        name,_=m.indexes.names(kind)
        await m.indexes.client.request('DELETE',f'/{name}')

async def exercise(h, data):
    m = maintenance(h)
    doc, first, second = await populate(h, data)
    monitor = ArchiveMonitor(m.archive,m.indexes,{'state':'completed', 'checked_at':None, 'recovered':0},ttl=0)
    status = await monitor.snapshot()
    assert status['status']=='online' and status['consistency']=='counts_match', status
    assert (status['documents'],status['originals'],status['runs'],status['reused_original_runs']) == (1,1,2,1)
    assert status['original_bytes']==len(data) and status['artifact_bytes']>0
    assert status['run_status']['completed']==2 and status['run_cache']['hit']==1
    assert status['duplicate_submissions']==1
    assert status['bytes']==status['original_bytes']+status['artifact_bytes']+status['metadata_bytes']
    assert (await execute(m,'audit'))['runs']==2
    await m.indexes.remove('runs',str(second))
    assert (await monitor.snapshot())['consistency']=='count_mismatch'
    with pytest.raises(ProcessingError,match='ARCHIVE_INTEGRITY_ERROR'):
        await execute(m,'audit')
    assert (await execute(m,'rebuild'))['index_verified'] is True
    assert (await monitor.snapshot())['consistency']=='counts_match'
    calls = h.summary.calls
    output=io.BytesIO()
    assert (await execute(m,'backup',output))['objects']>0
    with pytest.raises(ProcessingError,match='empty permanent bucket'):
        await execute(m,'restore',io.BytesIO(output.getvalue()))
    # Only isolated test namespaces. Never delete the application archive.
    archive=m.archive
    before={key:(await archive._read(key)).data async for key in archive.iter_keys()}
    await h.forget(first)
    await h.forget(second)
    await clear_isolated(m)
    assert (await execute(m,'restore',io.BytesIO(output.getvalue())))['runs']==2
    after={key:(await archive._read(key)).data async for key in archive.iter_keys()}
    assert after==before
    assert (await h.docs.get(doc)).preferred_run_id==first
    assert h.summary.calls==calls
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app),base_url='http://test') as http:
        assert (await expect(http,'GET',f'/library/{doc}/original')).content==data
        assert (await expect(http,'GET',f'/runs/{second}')).json()['status']=='completed'
    assert not await archive.restore_pending()

@pytest.mark.asyncio
async def test_operations_roundtrip(api_harness,sample_basic):
    h,_=api_harness
    await exercise(h,sample_basic.read_bytes())

@pytest.mark.asyncio
async def test_monitor_bounded_cache_and_outages(api_harness,sample_basic):
    h,fake=api_harness
    m=maintenance(h)
    await populate(h,sample_basic.read_bytes())
    assert (await inventory(m.archive,max_pages=1))['truncated'] is True
    cache=ArchiveMonitor(m.archive,m.indexes,{},ttl=30)
    first=await cache.snapshot()
    fake.offline=True
    assert await cache.snapshot()==first
    fresh=ArchiveMonitor(m.archive,m.indexes,{},ttl=0)
    partial=await fresh.snapshot()
    assert partial['status']=='online' and partial['indexed_runs'] is None and partial['consistency']=='unknown'
    fake.offline=False
    m.archive.client.offline=True
    partial=await fresh.snapshot()
    assert partial['status']=='unavailable' and partial['indexed_runs']==2

@pytest.mark.asyncio
async def test_integrity_and_interrupted_restore(api_harness,sample_basic,monkeypatch):
    h,_=api_harness
    m=maintenance(h)
    doc,first,_=await populate(h,sample_basic.read_bytes())
    run=await h.pipeline.get(first)
    key=m.archive._artifact_key(doc,first,'document.md',run.extraction_ref)
    original=await m.archive._read(key)
    tag=await m.archive._write(key,b'corrupt','text/markdown',original.etag)
    with pytest.raises(ProcessingError,match='Markdown differs'):
        await execute(m,'audit')
    await m.archive._write(key,original.data,'text/markdown',tag)
    output=io.BytesIO()
    await execute(m,'backup',output)
    await clear_isolated(m)
    original_write=m.archive._write
    async def fail(key,*args,**kwargs):
        if key.endswith('metadata.json'): raise ProcessingError('ARCHIVE_UNAVAILABLE','injected',503)
        return await original_write(key,*args,**kwargs)
    monkeypatch.setattr(m.archive,'_write',fail)
    with pytest.raises(ProcessingError,match='ARCHIVE_UNAVAILABLE'):
        await execute(m,'restore',io.BytesIO(output.getvalue()))
    assert await m.archive.restore_pending()
    with pytest.raises(ProcessingError,match='ARCHIVE_RESTORE_PENDING'):
        await m.recover()
    with pytest.raises(ProcessingError,match='ARCHIVE_RESTORE_PENDING'):
        await guarded(h.pipeline,lambda: h.pipeline.get(first))

@pytest.mark.parametrize('bad',['checksum','traversal','extra','wrong_bucket'])
def test_invalid_backup_rejected(bad):
    data=b'pdf'
    key=hashlib.sha256(data).hexdigest()+'/original.pdf' if bad!='traversal' else '../outside'
    payload=io.BytesIO()
    with zipfile.ZipFile(payload,'w') as z:
        z.writestr('objects/'+key,data)
        if bad=='extra': z.writestr('unexpected',b'x')
        z.writestr('manifest.json',json.dumps({'format':1,'bucket':'wrong' if bad=='wrong_bucket' else 'pdf-documents',
            'objects':{key:{'size':len(data),'sha256':hashlib.sha256(data).hexdigest() if bad!='checksum' else '0'*64}}}))
    with zipfile.ZipFile(payload) as z, pytest.raises(ProcessingError):
        validate_backup(z,'pdf-documents')

@pytest.mark.pipeline_integration
@pytest.mark.asyncio
async def test_real_archive_operations(settings,sample_basic):
    from redis.asyncio import Redis
    from redis.exceptions import RedisError
    real=get_settings()
    url=urlsplit(real.redis_url)
    redis=Redis.from_url(urlunsplit(url._replace(path='/15')),decode_responses=True,socket_timeout=3,socket_connect_timeout=3)
    ident=uuid4().hex
    store=ArchiveStore(real.model_copy(update={'s3_archive_bucket':'pdf-test-'+ident}))
    search=OpenSearchClient(real.opensearch_url)
    indexes=IndexManager(search,prefix='pdf-test-'+ident)
    docs=DocumentRepository(store,indexes)
    h=Harness(settings,docs,RunRepository(store,indexes,docs),redis)
    h.app.include_router(router)
    owns_db=created=False
    try:
        try:
            await redis.ping()
            await search.request('GET','/_cluster/health')
            await store.prepare()
            created=True
        except (RedisError,ProcessingError,httpx.HTTPError):
            pytest.skip('Real Redis, RustFS and OpenSearch required')
        assert await redis.dbsize()==0, 'Dedicated Redis DB 15 must be empty'
        owns_db=True
        await indexes.ensure()
        await h.queue.ensure_group()
        await exercise(h,sample_basic.read_bytes()+f'\n% stage8-{ident}\n'.encode())
    finally:
        if owns_db:
            while await redis.dbsize():
                cursor=0
                while True:
                    cursor,keys=await redis.scan(cursor=cursor,count=500)
                    if keys: await redis.delete(*keys)
                    if not cursor: break
        if created:
            for key in [k async for k in store.iter_keys()]: await store.remove_key(key)
            await asyncio.to_thread(store.client.delete_bucket,Bucket=store.bucket)
            for kind in ('documents','runs'):
                name,_=indexes.names(kind)
                try: await search.request('DELETE',f'/{name}')
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code!=404: raise
        await redis.aclose()
        await search.aclose()
        store.client.close()
