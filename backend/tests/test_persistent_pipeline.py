"""Offline provider, real pipeline, mandatory real services in release gate."""
import hashlib
from uuid import UUID, uuid4
import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router
from app.archive_store import ArchiveStore
from app.cache import active_key, fingerprint, result_key
from app.config import get_settings
from app.document_processor import DocumentProcessor
from app.document_repository import DocumentRepository
from app.errors import ProcessingError
from app.index_manager import IndexManager
from app.object_store import LocalTestObjectStore
from app.opensearch_client import OpenSearchClient
from app.persistent_pipeline import PersistentPipeline
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository as RedisRepository
from app.run_repository import RunRepository
from app.schemas import ParserChoice
from app.worker import DocumentWorker
from tests.fake_redis import FakeRedis
from tests.test_catalogue import repos


class Summary:
    def __init__(self):
        self.calls = 0
        self.failure = False

    async def summarize(self, pages):
        self.calls += 1
        if self.failure:
            raise ProcessingError('SUMMARY_TEST_FAILURE', 'Mock summary failure.')
        assert pages
        return 'Summary: α + $x^2$'


class Harness:
    def __init__(self, settings, docs, runs, redis, *, namespace=None):
        self.settings, self.docs, self.runs, self.redis = settings, docs, runs, redis
        namespace = namespace or f'persistent-test:{uuid4().hex}'
        self.repo = RedisRepository(redis, namespace)
        self.queue = DocumentQueue(redis, namespace + ':jobs', namespace + ':group', namespace + ':worker')
        self.store = LocalTestObjectStore(settings.upload_dir)
        self.pipeline = PersistentPipeline(settings, docs, runs, self.repo)
        self.summary = Summary()
        self.processor = DocumentProcessor(settings, self.repo, summarizer=self.summary)
        self.worker = DocumentWorker(settings, self.queue, self.repo, self.processor, object_store=self.store, archive_pipeline=self.pipeline)
        self.app = FastAPI()
        self.app.include_router(router)
        @self.app.exception_handler(ProcessingError)
        async def error(request: Request, exc: ProcessingError):
            return JSONResponse(status_code=exc.http_status, content={'error': {'code': exc.code, 'message': exc.message}})
        self.app.state.settings = settings
        self.app.state.redis = redis
        self.app.state.repository = self.repo
        self.app.state.queue = self.queue
        self.app.state.object_store = self.store
        self.app.state.pipeline_archive = self.pipeline
        self.ids, self.digests, self.documents = [], set(), set()

    async def upload(self, http, data, filename):
        doc_id = hashlib.sha256(data).hexdigest()
        self.documents.add(doc_id)
        self.digests.add(fingerprint(doc_id, ParserChoice.pypdf, self.settings))
        response = await http.post('/api/v1/documents', files={'files': (filename, data, 'application/pdf')}, data={'parser': 'pypdf'})
        assert response.status_code == 202, response.text
        entry = response.json()['documents'][0]
        self.ids.append(UUID(entry['id']))
        return entry, UUID(entry['id'])

    async def one(self):
        entries = await self.queue.read('>', 100)
        assert len(entries) == 1
        await self.worker.handle_message(*entries[0])
        return entries[0]

    async def forget(self, run_id):
        await self.redis.delete(*(self.repo.key(run_id, suffix) for suffix in ('meta', 'pages', 'summary')))

    async def clean(self, real=False):
        for uid in self.ids:
            await self.forget(uid)
            await self.store.remove(uid)
        await self.redis.delete(self.queue.stream, *(key for digest in self.digests for key in (active_key(digest), result_key(digest))))
        if real:
            for document_id in self.documents:
                # Unique test original and only its own objects; never clear a bucket.
                for page in self.docs.archive.client.get_paginator('list_objects_v2').paginate(Bucket=self.docs.archive.bucket, Prefix=document_id + '/'):
                    for entry in page.get('Contents', []):
                        await asyncio.to_thread(self.docs.archive.client.delete_object, Bucket=self.docs.archive.bucket, Key=entry['Key'])
            for uid in self.ids:
                await asyncio.to_thread(self.docs.archive.client.delete_object, Bucket=self.docs.archive.bucket, Key=f'run-ids/{uid}.json')


async def exercise(h, data):
    await h.queue.ensure_group()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        first, a = await h.upload(http, data, 'first.pdf')
        shared, b = await h.upload(http, data, 'renamed.pdf')
        assert first['cache'] == 'miss' and shared['cache'] == 'shared'
        assert a != b
        assert (await h.pipeline.get(a)).status == 'queued'
        assert (await h.pipeline.get(b)).cache_source_run_id == a
        await h.one()
        await h.one()
        hit, c = await h.upload(http, data, 'another.pdf')
        assert hit['cache'] == 'hit' and hit['status'] == 'queued'
        await h.forget(a)  # hits read the archived source, not its old Redis keys
        await h.one()
        assert h.summary.calls == 1
        doc_id = hashlib.sha256(data).hexdigest()
        doc = await h.docs.get(doc_id)
        assert set(doc.filenames) == {'first.pdf', 'renamed.pdf', 'another.pdf'}
        assert doc.preferred_run_id == a
        assert await h.docs.indexes.count_runs(doc_id) == 3
        for uid, name in ((a, 'first.pdf'), (b, 'renamed.pdf'), (c, 'another.pdf')):
            run = await h.pipeline.get(uid)
            assert run.status == 'completed' and run.extraction_ref and run.summary_ref
            await h.forget(uid)
            status = (await http.get(f'/api/v1/documents/{uid}')).json()
            assert status['filename'] == name and status['summary'] == 'Summary: α + $x^2$'
            pages = await http.get(f'/api/v1/documents/{uid}/pages')
            assert pages.status_code == 200 and len(pages.json()['pages']) == 2
            assert (await h.docs.archive.read_run_artifact(doc_id, uid, 'document.md', generation=run.extraction_ref)).data.startswith(b'# Page 1')
        target = h.settings.upload_dir / 'archive-original.pdf'
        await h.docs.archive.fetch_original(doc_id, target)
        assert target.read_bytes() == data
        # A different file's failed summary retains extraction and is retried
        # even after all its Redis coordination and result keys expired.
        h.summary.failure = True
        _, d = await h.upload(http, data + b'\n% partial\n', 'first.pdf')
        await h.one()
        partial = await h.pipeline.get(d)
        assert partial.status == 'partial' and partial.has_extraction and not partial.has_summary
        await h.forget(d)
        h.summary.failure = False
        retry = await http.post(f'/api/v1/documents/{d}/retry-summary')
        assert retry.status_code == 202, retry.text
        assert (await http.get(f'/api/v1/documents/{d}')).json()['status'] == 'summarizing'
        assert (await http.post(f'/api/v1/documents/{d}/retry-summary')).status_code == 409
        await h.one()
        completed = await h.pipeline.get(d)
        assert completed.status == 'completed' and completed.extraction_ref == partial.extraction_ref
        assert h.summary.calls == 3
        assert (await h.redis.xpending(h.queue.stream, h.queue.group))['pending'] == 0


@pytest.mark.asyncio
async def test_archive_pipeline_history_and_retry(repos, settings, sample_basic):
    docs, runs, _ = repos
    h = Harness(settings, docs, runs, FakeRedis())
    await exercise(h, sample_basic.read_bytes())


@pytest.mark.asyncio
async def test_stale_worker_and_terminal_replay(repos, settings, sample_basic):
    docs, runs, fake = repos
    h = Harness(settings, docs, runs, FakeRedis())
    await h.queue.ensure_group()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        _, uid = await h.upload(http, sample_basic.read_bytes(), 'one.pdf')
        message_id, fields = (await h.queue.read('>'))[0]
        old, new = uuid4().hex, uuid4().hex
        assert await h.queue.begin(message_id, uid, old, h.repo)
        first = await h.pipeline.claim(uid, old, 'extract')
        assert await h.queue.begin(message_id, uid, new, h.repo)
        second = await h.pipeline.claim(uid, new, 'extract')
        parsed = await h.processor.extract('pypdf', sample_basic)
        with pytest.raises(ProcessingError, match='JOB_LEASE_LOST'):
            await h.pipeline.save_extraction(first, old, parsed)
        second = await h.pipeline.save_extraction(second, new, parsed)
        completed = await h.pipeline.complete(second, new, 'committed')
        fake.offline = True
        with pytest.raises(ProcessingError, match='CATALOGUE_UNAVAILABLE'):
            await h.pipeline.project_terminal(completed, new)
        assert (await h.pipeline.status(uid)).summary == 'committed'
        assert (await h.redis.xpending(h.queue.stream, h.queue.group))['pending'] == 1
        fake.offline = False
        await h.worker.handle_message(message_id, fields)
        assert h.summary.calls == 0
        assert (await h.pipeline.get(uid)).summary_ref == completed.summary_ref
        assert (await h.redis.xpending(h.queue.stream, h.queue.group))['pending'] == 0


@pytest.mark.asyncio
@pytest.mark.pipeline_integration
async def test_real_persistent_pipeline(real_redis, settings, sample_basic):
    real = get_settings()
    store = ArchiveStore(real)
    search = OpenSearchClient(real.opensearch_url)
    indexes = IndexManager(search, prefix=f'pdf-test-{uuid4().hex}')
    docs = DocumentRepository(store, indexes)
    runs = RunRepository(store, indexes, docs)
    h = Harness(settings, docs, runs, real_redis)
    connected = False
    try:
        try:
            await store.ready()
            await search.request('GET', '/_cluster/health')
        except (ProcessingError, httpx.HTTPError):
            pytest.skip('Real RustFS and OpenSearch required')
        connected = True
        await indexes.ensure()
        await exercise(h, sample_basic.read_bytes() + f'\n% pipeline-test-{uuid4()}\n'.encode())
    finally:
        if connected:
            await h.clean(real=True)
            for kind in ('documents','runs'):
                name, _ = indexes.names(kind)
                try:
                    await search.request('DELETE', f'/{name}')
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
        await search.aclose()
        store.client.close()


@pytest.mark.asyncio
async def test_shared_waits_without_ai_and_survives_source_redis_expiry(repos, settings, sample_basic):
    docs, runs, _ = repos
    h = Harness(settings, docs, runs, FakeRedis())
    await h.queue.ensure_group()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        _, a = await h.upload(http, sample_basic.read_bytes(), 'owner.pdf')
        _, b = await h.upload(http, sample_basic.read_bytes(), 'shared.pdf')
        owner = (await h.queue.read('>'))[0]
        shared = (await h.queue.read('>'))[0]
        with pytest.raises(ProcessingError, match='SHARED_SOURCE_PENDING'):
            await h.worker.handle_message(*shared)
        assert (await h.pipeline.get(b)).worker_attempts == 0
        assert h.summary.calls == 0
        await h.worker.handle_message(*owner)
        await h.forget(a)
        await h.worker.handle_message(*shared)
        assert h.summary.calls == 1
        assert (await h.pipeline.get(b)).status == 'completed'
        assert (await h.redis.xpending(h.queue.stream, h.queue.group))['pending'] == 0


@pytest.mark.asyncio
async def test_retry_failure_is_not_reexecuted_after_lost_ack(repos, settings, sample_basic):
    docs, runs, _ = repos
    h = Harness(settings, docs, runs, FakeRedis())
    h.summary.failure = True
    await h.queue.ensure_group()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        _, uid = await h.upload(http, sample_basic.read_bytes(), 'one.pdf')
        await h.one()
        assert (await h.pipeline.get(uid)).status == 'partial'
        await h.forget(uid)
        await h.pipeline.retry_summary(uid, h.queue)
        mid, fields = (await h.queue.read('>'))[0]
        token = uuid4().hex
        assert await h.queue.begin(mid, uid, token, h.repo)
        await h.pipeline.process(uid, token, 'summary', h.processor)
        failed = await h.pipeline.get(uid)
        assert failed.status == 'partial' and failed.retry_id
        assert h.summary.calls == 2
        # Crash before ACK; a new worker must project the old outcome, not call AI.
        await h.worker.handle_message(mid, fields)
        assert h.summary.calls == 2
        assert (await h.pipeline.get(uid)).revision == failed.revision
        assert (await h.redis.xpending(h.queue.stream, h.queue.group))['pending'] == 0
        await h.pipeline.retry_summary(uid, h.queue)
        h.summary.failure = False
        await h.one()
        assert h.summary.calls == 3
        assert (await h.pipeline.get(uid)).status == 'completed'


@pytest.mark.asyncio
async def test_archive_failure_does_not_publish_completion_or_ack(repos, settings, sample_basic, monkeypatch):
    docs, runs, _ = repos
    h = Harness(settings, docs, runs, FakeRedis())
    await h.queue.ensure_group()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        _, uid = await h.upload(http, sample_basic.read_bytes(), 'one.pdf')
        original_write = docs.archive.write_run_artifact
        async def fail_summary_write(*args, **kwargs):
            if args[2] == 'summary.md':
                raise ProcessingError('ARCHIVE_UNAVAILABLE', 'Simulated storage outage.', 503)
            return await original_write(*args, **kwargs)
        monkeypatch.setattr(docs.archive, 'write_run_artifact', fail_summary_write)
        mid, fields = (await h.queue.read('>'))[0]
        with pytest.raises(ProcessingError, match='ARCHIVE_UNAVAILABLE'):
            await h.worker.handle_message(mid, fields)
        checkpoint = await h.pipeline.get(uid)
        assert checkpoint.status == 'summarizing' and checkpoint.has_extraction and not checkpoint.has_summary
        assert (await h.redis.xpending(h.queue.stream, h.queue.group))['pending'] == 1
        monkeypatch.setattr(docs.archive, 'write_run_artifact', original_write)
        async def never_extract(*args):
            pytest.fail('Committed extraction must not run again')
        monkeypatch.setattr(h.processor, 'extract', never_extract)
        await h.worker.handle_message(mid, fields)
        final = await h.pipeline.get(uid)
        assert final.status == 'completed' and final.extraction_ref == checkpoint.extraction_ref
        assert (await h.redis.xpending(h.queue.stream, h.queue.group))['pending'] == 0


@pytest.mark.asyncio
async def test_zip_and_failed_extraction_have_durable_independent_runs(repos, settings, sample_basic, sample_scanned):
    from io import BytesIO
    from zipfile import ZipFile
    docs, runs, _ = repos
    h = Harness(settings, docs, runs, FakeRedis())
    await h.queue.ensure_group()
    packed = BytesIO()
    with ZipFile(packed, 'w') as z:
        z.writestr('a.pdf', sample_basic.read_bytes())
        z.writestr('b.pdf', sample_basic.read_bytes())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        response = await http.post('/api/v1/documents', data={'parser': 'pypdf'}, files={'files': ('batch.zip', packed.getvalue(), 'application/zip')})
        assert response.status_code == 202, response.text
        entries = response.json()['documents']
        assert len(entries) == 2 and len({e['id'] for e in entries}) == 2
        await h.one()
        await h.one()
        for entry in entries:
            assert (await h.pipeline.get(UUID(entry['id']))).status == 'completed'
        assert h.summary.calls == 1
        _, failed = await h.upload(http, sample_scanned.read_bytes(), 'scan.pdf')
        await h.one()
        await h.forget(failed)
        record = await h.pipeline.get(failed)
        assert record.status == 'failed' and not record.has_extraction
        assert record.error_code == 'NO_EXTRACTABLE_TEXT'
        assert (await http.get(f'/api/v1/documents/{failed}/pages')).status_code == 409
        assert h.summary.calls == 1


@pytest.mark.asyncio
async def test_all_three_parsers_keep_separate_results(repos, settings, sample_basic):
    from tests.test_end_to_end import OfflineModel
    docs, runs, _ = repos
    h = Harness(settings, docs, runs, FakeRedis())
    model = OfflineModel()
    processor = DocumentProcessor(settings, h.repo, llm=model, summarizer=h.summary)
    h.worker.processor = processor
    await h.queue.ensure_group()
    data = sample_basic.read_bytes()
    doc_id = hashlib.sha256(data).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        for parser in ('pypdf', 'gemini', 'gpt5_mini'):
            response = await http.post('/api/v1/documents', data={'parser': parser}, files={'files': ('same.pdf', data, 'application/pdf')})
            assert response.status_code == 202, response.text
            entry = response.json()['documents'][0]
            assert entry['cache'] == 'miss'
            await h.one()
            run = await h.pipeline.get(UUID(entry['id']))
            assert run.status == 'completed' and run.parser.value == parser
            assert run.document_id == doc_id
        assert await h.docs.indexes.count_runs(doc_id) == 3
        assert h.summary.calls == 3
        assert model.pdf_calls == 2 and model.image_calls == 2
