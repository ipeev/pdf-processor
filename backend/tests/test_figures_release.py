"""No paid calls; run-owned durable figures, API, dedup and replay."""
from __future__ import annotations
import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.errors import ProcessingError
from app.figure_assets import validate_metadata, invalid
from app.library_api import router as library_router
from app.parsers.base import ParsedDocument, ParsedPage
from app.parsers.vision_figures import VisionFiguresParser
from app.schemas import ParserChoice
from tests.fake_redis import FakeRedis
from tests.test_persistent_pipeline import Harness
from tests.test_catalogue import repos
from tests.test_library_api import api_harness

ROOT = Path(__file__).parent / 'fixtures' / 'vision'

class FixtureVision:
    def __init__(self): self.calls=[]
    async def complete_vision_figures_page(self, image, *, page, max_tokens):
        self.calls.append(page)
        return (ROOT / f'page_{page:04d}.json').read_text()

async def upload(http, data, filename='figures.pdf'):
    res=await http.post('/api/v1/documents', files={'files': (filename, data, 'application/pdf')},
                        data={'parser': 'vision_figures'})
    assert res.status_code == 202, res.text
    return res.json()['documents'][0]


@pytest.mark.asyncio
async def test_live_pipeline_without_paid_calls_and_bundle(repos, settings):
    docs, runs, fake = repos
    h = Harness(settings, docs, runs, FakeRedis())
    h.app.include_router(library_router)
    client=FixtureVision()
    h.processor.parsers['vision_figures'] = VisionFiguresParser(client, settings)
    await h.queue.ensure_group()
    raw=(ROOT/'sample_figures.pdf').read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        first=await upload(http, raw)
        shared=await upload(http, raw, 'second.pdf')
        assert first['cache']=='miss' and shared['cache']=='shared'
        await asyncio.wait_for(h.one(), 15)
        await asyncio.wait_for(h.one(), 15)
        assert client.calls==[1,2]
        first_id=first['id']; second_id=shared['id']
        a=await h.pipeline.get(__import__('uuid').UUID(first_id))
        b=await h.pipeline.get(__import__('uuid').UUID(second_id))
        assert a.status=='completed' and b.status=='completed'
        assert a.extraction_ref != b.extraction_ref
        for uid in (first_id, second_id):
            figures=(await http.get(f'/api/v1/runs/{uid}/figures')).json()
            assert len(figures['figures'])==3
            pages=(await http.get(f'/api/v1/runs/{uid}/pages')).json()['pages']
            assert len(pages)==2
            assert sum(p['markdown'].count('](images/') for p in pages)==3
            for row in figures['figures']:
                png=await http.get(f'/api/v1/runs/{uid}/images/{row["filename"]}')
                assert png.status_code==200, png.text[:100] if png.status_code!=200 else ''
                assert png.headers['content-type'].startswith('image/png')
                assert hashlib.sha256(png.content).hexdigest()==row['sha256']
            bundle=await http.get(f'/api/v1/runs/{uid}/bundle.zip')
            assert bundle.status_code==200, bundle.text[:100] if bundle.status_code!=200 else ''
            with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
                assert z.namelist()[0]=='document.md'
                assert len([n for n in z.namelist() if n.startswith('images/')])==3
                for row in figures['figures']:
                    assert hashlib.sha256(z.read('images/'+row['filename'])).hexdigest()==row['sha256']
        traversal=await http.get(f'/api/v1/runs/{first_id}/images/nope.png')
        assert traversal.status_code==404
        hit=await upload(http, raw, 'third.pdf')
        assert hit['cache']=='hit'
        await asyncio.wait_for(h.one(), 15)
        assert client.calls==[1,2], 'cache hit should not call vision provider'
        c=await h.pipeline.get(__import__('uuid').UUID(hit['id']))
        assert c.status=='completed' and c.extraction_ref not in (a.extraction_ref,b.extraction_ref)
        # All 3 runs own independent asset keys for safe deletion of any one.
        assert len((await h.pipeline.read_figures(c))['figures'])==3


@pytest.mark.asyncio
async def test_vision_call_admission_and_uncertain_outcome(repos):
    docs, runs, fake = repos
    store = docs.archive
    doc_id='a'*64; rid=uuid4(); calls=[]
    async def answer():
        calls.append(True)
        return '{"markdown":"yes", "figures":[]}'
    response=await store.durable_vision_response(doc_id,rid,1,answer)
    assert response.startswith('{')
    assert await store.durable_vision_response(doc_id,rid,1,answer)==response
    assert calls==[True]
    async def broken():
        calls.append('failed')
        raise ProcessingError('PROVIDER_UNAVAILABLE','Transient provider outage')
    with pytest.raises(ProcessingError, match='PROVIDER_UNAVAILABLE'):
        await store.durable_vision_response(doc_id,rid,2,broken)
    with pytest.raises(ProcessingError, match='VISION_CALL_OUTCOME_UNKNOWN'):
        await store.durable_vision_response(doc_id,rid,2,answer)
    assert calls==[True,'failed']


def test_figures_metadata_rejects_orphans_and_remote_links():
    parsed=[ParsedPage(1,'![f](images/p0001-figure-1.png)')]
    entry=dict(filename='p0001-figure-1.png',page=1,sha256='a'*64,bytes=2,width=1,height=1,kind='diagram',caption='Figure 1')
    meta={'version':1,'figures':[entry],'warnings':[]}
    assert validate_metadata(meta,parsed)==meta
    for mutated in (
        {**meta,'figures':[{**entry,'filename':'../escape.png'}]},
        {**meta,'figures':[{**entry,'page':2}]},
        {**meta,'figures':[]},
    ):
        with pytest.raises(ProcessingError,match='RESULT_CORRUPT'):
            validate_metadata(mutated,parsed)
    with pytest.raises(ProcessingError,match='RESULT_CORRUPT'):
        validate_metadata(meta,[ParsedPage(1,'![bad](https://external.test/a.png)\n\n![f](images/p0001-figure-1.png)')])


def test_version_surface_and_frontend_fourth_choice():
    path=Path(__file__).resolve().parents[2]
    import tomllib
    import json
    backend_version = tomllib.loads((path/'backend/pyproject.toml').read_text())['project']['version']
    assert json.loads((path/'frontend/package.json').read_text())['version']==backend_version
    assert 'disabled: true' not in (path/'frontend/src/ui.ts').read_text().split("id: 'vision_figures'")[1].split('\n')[0]

@pytest.mark.asyncio
async def test_vision_archive_audit_backup_restore_and_corruption(api_harness, settings):
    """Archive housekeeping must include every committed PNG in exact backup."""
    from app.archive_operations import execute
    from tests.test_archive_maintenance import maintenance
    from tests.test_archive_operations import clear_isolated
    from uuid import UUID
    h, _=api_harness
    client=FixtureVision()
    h.processor.parsers['vision_figures']=VisionFiguresParser(client,settings)
    raw=(ROOT/'sample_figures.pdf').read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app),base_url='http://test') as http:
        item=await upload(http,raw)
        await asyncio.wait_for(h.one(),15)
    uid=UUID(item['id'])
    run=await h.pipeline.get(uid)
    m=maintenance(h)
    audited=await asyncio.wait_for(execute(m,'audit'),20)
    assert audited['runs']==1 and audited['index_verified']
    backup=io.BytesIO()
    result=await asyncio.wait_for(execute(m,'backup',backup),20)
    assert result['objects']>6
    with zipfile.ZipFile(io.BytesIO(backup.getvalue())) as zipfile_archive:
        image_names=[n for n in zipfile_archive.namelist() if '/images/' in n]
        assert len(image_names)==3
        assert any(n.endswith('figures.json') for n in zipfile_archive.namelist())
        assert any('/vision/admissions/' in n for n in zipfile_archive.namelist())
        assert any('/vision/responses/' in n for n in zipfile_archive.namelist())
    index=(await h.pipeline.read_figures(run))['figures'][0]
    key=m.archive.figure_key(run.document_id,uid,run.extraction_ref,index['filename'])
    # An altered PNG is caught by the committed run's SHA-256, not accepted by the image API.
    stored=await m.archive._read(key)
    await m.archive._write(key,b'\x89PNG\r\n\x1a\nCORRUPT','image/png',stored.etag)
    with pytest.raises(ProcessingError,match='RESULT_CORRUPT'):
        await h.pipeline.read_figure(run,index['filename'])
    with pytest.raises(ProcessingError,match='RESULT_CORRUPT'):
        await execute(m,'audit')
    damaged=await m.archive._read(key)
    await m.archive._write(key,stored.data,'image/png',damaged.etag)
    assert (await execute(m,'audit'))['runs']==1
    # Snapshot restoration includes raw replies and all figure assets.
    before={key: (await m.archive._read(key)).data async for key in m.archive.iter_keys()}
    await clear_isolated(m)
    restored=await asyncio.wait_for(execute(m,'restore',io.BytesIO(backup.getvalue())),20)
    assert restored['runs']==1 and restored['index_verified']
    after={key: (await m.archive._read(key)).data async for key in m.archive.iter_keys()}
    assert before==after
    recovered=await h.pipeline.get(uid)
    assert (await h.pipeline.read_figures(recovered))['figures'][0]['filename']==index['filename']

@pytest.mark.asyncio
@pytest.mark.pipeline_integration
async def test_real_rustfs_redis_opensearch_vision_integration(settings):
    """Ubuntu gate: real three-service persistence, fake vision model, no paid calls.

    Uses a unique source/document prefix and isolated OpenSearch namespace.
    Never invokes maintenance against the user's ordinary documents.
    """
    from redis.asyncio import Redis
    from app.config import get_settings
    from app.archive_store import ArchiveStore
    from app.document_repository import DocumentRepository as PermanentDocuments
    from app.run_repository import RunRepository
    from app.opensearch_client import OpenSearchClient
    from app.index_manager import IndexManager
    from uuid import UUID
    cfg=get_settings()
    redis=Redis.from_url(cfg.redis_url,decode_responses=True,socket_connect_timeout=1,socket_timeout=1)
    archive=ArchiveStore(cfg)
    search=OpenSearchClient(cfg.opensearch_url)
    indexes=IndexManager(search,prefix=f'pdf-test-{uuid4().hex}')
    connected=False
    h=None
    run_ids=[]
    doc_id=None
    try:
        try:
            await redis.ping()
            await archive.ready()
            await search.request('GET','/_cluster/health',timeout=3.0)
        except Exception:
            pytest.skip('Real Redis, RustFS and OpenSearch are required for Ubuntu gate')
        connected=True
        await indexes.ensure()
        docs=PermanentDocuments(archive,indexes)
        runs=RunRepository(archive,indexes,docs)
        h=Harness(settings,docs,runs,redis)
        h.app.include_router(library_router)
        client=FixtureVision()
        h.processor.parsers['vision_figures']=VisionFiguresParser(client,settings)
        await h.queue.ensure_group()
        raw=(ROOT/'sample_figures.pdf').read_bytes()+f'\n% integration-{uuid4()}\n'.encode()
        doc_id=hashlib.sha256(raw).hexdigest()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app),base_url='http://test') as http:
            a=await upload(http,raw,'ubuntu-a.pdf'); run_ids.append(UUID(a['id']))
            b=await upload(http,raw,'ubuntu-b.pdf'); run_ids.append(UUID(b['id']))
            await asyncio.wait_for(h.one(),40)
            await asyncio.wait_for(h.one(),40)
            assert len(client.calls)==2
            for uid in run_ids:
                run=await h.pipeline.get(uid)
                assert run.status=='completed'
                figures=await h.pipeline.read_figures(run)
                assert len(figures['figures'])==3
                for figure in figures['figures']:
                    assert (await http.get(f'/api/v1/runs/{uid}/images/{figure["filename"]}')).status_code==200
            assert len({(await h.pipeline.get(uid)).extraction_ref for uid in run_ids})==2
    finally:
        if h is not None:
            for uid in run_ids:
                try: await h.forget(uid)
                except Exception: pass
                try: await h.store.remove(uid)
                except Exception: pass
            try: await redis.delete(h.queue.stream)
            except Exception: pass
        if connected:
            if doc_id:
                try:
                    for key in [k async for k in archive.iter_keys(f'{doc_id}/')]:
                        await archive.remove_key(key)
                except Exception: pass
            for uid in run_ids:
                try: await archive.remove_key(f'run-ids/{uid}.json')
                except Exception: pass
            for kind in ('documents','runs'):
                try:
                    name,_=indexes.names(kind)
                    await search.request('DELETE',f'/{name}',timeout=4.0)
                except Exception: pass
        await search.aclose()
        archive.client.close()
        await redis.aclose()
