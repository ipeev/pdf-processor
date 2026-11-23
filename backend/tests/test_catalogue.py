"""Catalogue contracts and required real RustFS/OpenSearch integration."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.archive_store import ArchiveStore, original_key
from app.catalogue_models import DocumentManifest, RunManifest, now
from app.config import Settings, get_settings
from app.document_repository import DocumentRepository
from app.errors import ProcessingError
from app.index_manager import IndexManager, DOCUMENT, RUN
from app.opensearch_client import OpenSearchClient
from app.reconciliation import rebuild
from app.run_repository import RunRepository
from tests.test_archive_store import MemoryS3


class PagedS3(MemoryS3):
    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        self.head_bucket(Bucket=Bucket)
        keys = sorted(k for bucket, k in self.objects if bucket == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        end = start + 2  # enforce pagination, including pages containing only artifacts
        return {'Contents': [{'Key': key, 'Size': len(self.objects[(Bucket, key)][0])} for key in keys[start:end]], 'IsTruncated': end < len(keys),
                'NextContinuationToken': str(end)}


class SearchFake:
    def __init__(self):
        self.indices = {}
        self.offline = False

    def __call__(self, request):
        if self.offline:
            return httpx.Response(503, json={})
        parts = request.url.path.strip('/').split('/')
        name = parts[0]
        payload = json.loads(request.content) if request.content else {}
        def result(code, body): return httpx.Response(code, json=body)
        if name == '_alias':
            return result(200, {k: {'aliases': v['aliases']} for k, v in self.indices.items() if parts[1] in v['aliases']})
        if len(parts) == 1 and request.method == 'PUT':
            if name in self.indices:
                return result(400, {'error': 'resource_already_exists_exception'})
            self.indices[name] = {**payload, 'docs': {}}
            return result(200, {'acknowledged': True})
        if parts[-1] == '_count':
            docs = next(v['docs'] for v in self.indices.values() if name in v['aliases'])
            doc_id = payload['query']['term']['document_id']
            return result(200, {'count': sum(v['_source']['document_id'] == doc_id for v in docs.values())})
        if name not in self.indices:
            name = next((k for k, v in self.indices.items() if name in v['aliases']), name)
        if name not in self.indices:
            return result(404, {})
        index = self.indices[name]
        if len(parts) == 1 and request.method == 'DELETE':
            del self.indices[name]
            return result(200, {'acknowledged': True})
        if parts[-1] == '_mapping':
            return result(200, {name: {'mappings': index['mappings']}})
        if parts[-1] == '_settings':
            return result(200, {name: {'settings': {'index': index['settings']}}})
        if parts[1] == '_doc':
            key = parts[2]
            saved = index['docs'].get(key)
            if request.method == 'GET':
                return result(200, saved) if saved else result(404, {})
            if request.method == 'DELETE':
                if not saved:
                    return result(404, {})
                index['docs'].pop(key)
                index.setdefault('deleted_versions', {})[key] = saved['_version'] + 1
                return result(200, {'result': 'deleted'})
            assert request.url.params['version_type'] == 'external'
            assert request.url.params['require_alias'] == 'true'
            version = int(request.url.params['version'])
            assert set(payload) <= set(index['mappings']['properties'])
            if (saved and saved['_version'] >= version) or index.get('deleted_versions', {}).get(key, 0) >= version:
                return result(409, {})
            index['docs'][key] = {'_version': version, '_source': payload}
            return result(201, {'_version': version})
        raise AssertionError(request.url)


@pytest.fixture
async def repos():
    store = ArchiveStore(Settings(_env_file=None), client=PagedS3())
    await store.prepare()
    fake = SearchFake()
    search = OpenSearchClient('http://opensearch:9200', transport=httpx.MockTransport(fake))
    indexes = IndexManager(search)
    await indexes.ensure()
    documents = DocumentRepository(store, indexes)
    try:
        yield documents, RunRepository(store, indexes, documents), fake
    finally:
        await search.aclose()


def new_run(doc_id, filename='first.pdf', **fields):
    run_id = uuid4()
    return RunManifest(document_id=doc_id, run_id=run_id, filename=filename, parser=fields.pop('parser', 'pypdf'),
        pipeline_version='pdf-pipeline-v4', application_version='0.12.0a3', s3_prefix=f'{doc_id}/runs/{run_id}/', **fields)


async def original(documents, tmp_path):
    source = tmp_path / 'first.pdf'
    sample = Path(__file__).resolve().parents[2] / 'samples' / 'basic_text.pdf'
    source.write_bytes(sample.read_bytes() + f'\n% catalogue-test-{uuid4()}\n'.encode())
    return await documents.archive.put_original(source)


async def complete(runs, run):
    return await runs.update(run.document_id, run.run_id, expected_revision=run.revision,
        status='completed', page_count=2, has_extraction=True, has_summary=True, completed_at=now())


async def exercise(documents, runs, tmp_path):
    saved = await original(documents, tmp_path)
    doc = await documents.register(saved, 'first.pdf')
    same = await documents.register(saved, 'renamed.pdf')
    assert same.document_id == doc.document_id and same.filename == 'first.pdf'
    first = new_run(doc.document_id)
    other = new_run(doc.document_id, 'renamed.pdf', cache='hit', cache_source_run_id=first.run_id)
    await runs.create(first)
    await runs.create(other)
    await runs.create(other)  # exact idempotent replay
    assert await documents.indexes.count_runs(doc.document_id) == 2
    a = await complete(runs, first)
    b = await complete(runs, other)
    assert (await documents.get(doc.document_id)).preferred_run_id == a.run_id
    await documents.set_preferred(doc.document_id, b.run_id)
    assert (await documents.get(doc.document_id)).preferred_run_id == b.run_id
    assert (await runs.get(doc.document_id, a.run_id)).filename == 'first.pdf'
    assert (await runs.get(doc.document_id, b.run_id)).filename == 'renamed.pdf'
    assert (await runs.get(doc.document_id, b.run_id)).cache == 'hit'
    # Out-of-order index delivery must not overwrite a newer revision.
    await documents.indexes.publish(doc)
    await runs.indexes.publish(first)
    return doc.document_id, (a, b)


@pytest.mark.asyncio
async def test_identity_history_preference_and_replay(repos, tmp_path):
    docs, runs, fake = repos
    doc_id, results = await exercise(docs, runs, tmp_path)
    assert len(fake.indices['pdf-documents-v1']['docs']) == 1
    assert fake.indices['pdf-runs-v1']['docs'][str(results[0].run_id)]['_source']['status'] == 'completed'
    assert fake.indices['pdf-documents-v1']['docs'][doc_id]['_source']['preferred_run_id'] == str(results[1].run_id)
    # Empty indexes rebuilt solely from canonical metadata, not the old index.
    fake.indices.clear()
    assert await rebuild(docs, runs) == {'documents': 1, 'runs': 2}
    assert await docs.indexes.count_runs(doc_id) == 2
    assert fake.indices['pdf-documents-v1']['docs'][doc_id]['_source']['preferred_run_id'] == str(results[1].run_id)
    assert await rebuild(docs, runs) == {'documents': 1, 'runs': 2}


@pytest.mark.asyncio
async def test_concurrent_aliases_and_stale_run_revision(repos, tmp_path):
    docs, runs, _ = repos
    saved = await original(docs, tmp_path)
    await docs.register(saved, 'first.pdf')
    await asyncio.gather(*(docs.register(saved, f'alias-{i}.pdf') for i in range(5)))
    doc = await docs.get(saved.document_id)
    assert set(doc.filenames) == {'first.pdf', *(f'alias-{i}.pdf' for i in range(5))}
    run = await runs.create(new_run(doc.document_id))
    updates = await asyncio.gather(*(runs.update(doc.document_id, run.run_id, expected_revision=1, worker_attempts=n)
                                    for n in (1, 2)), return_exceptions=True)
    assert sum(isinstance(x, RunManifest) for x in updates) == 1
    assert sum(isinstance(x, ProcessingError) and x.code == 'CATALOGUE_CONFLICT' for x in updates) == 1
    with pytest.raises(ProcessingError, match='INVALID_PREFERRED_RUN'):
        await docs.set_preferred(doc.document_id, run.run_id)
    with pytest.raises(ProcessingError, match='INVALID_RUN_UPDATE'):
        await runs.update(doc.document_id, run.run_id, expected_revision=2, filename='changed.pdf')


@pytest.mark.asyncio
async def test_archive_survives_index_outage(repos, tmp_path):
    docs, runs, fake = repos
    saved = await original(docs, tmp_path)
    fake.offline = True
    with pytest.raises(ProcessingError, match='CATALOGUE_UNAVAILABLE'):
        await docs.register(saved, 'first.pdf')
    assert (await docs.get(saved.document_id)).filename == 'first.pdf'
    fake.offline = False
    assert await rebuild(docs, runs) == {'documents': 1, 'runs': 0}
    assert len(fake.indices['pdf-documents-v1']['docs']) == 1
    run = new_run(saved.document_id, 'outage.pdf')
    fake.offline = True
    with pytest.raises(ProcessingError, match='CATALOGUE_UNAVAILABLE'):
        await runs.create(run)
    assert await runs.get(saved.document_id, run.run_id) == run
    fake.offline = False
    assert await rebuild(docs, runs) == {'documents': 1, 'runs': 1}
    assert await docs.indexes.count_runs(saved.document_id) == 1


@pytest.mark.asyncio
async def test_projected_same_revision_conflict_and_schema_validation(repos, tmp_path):
    docs, _, fake = repos
    doc = await docs.register(await original(docs, tmp_path), 'first.pdf')
    wrong = doc.model_copy(update={'filename': 'other.pdf','filenames': ('other.pdf',)})
    with pytest.raises(ProcessingError, match='CATALOGUE_SCHEMA_MISMATCH'):
        await docs.indexes.publish(wrong)
    fake.indices['pdf-documents-v1']['mappings'] = {'dynamic': 'true'}
    with pytest.raises(ProcessingError, match='CATALOGUE_SCHEMA_MISMATCH'):
        await docs.indexes.ensure()


@pytest.mark.asyncio
async def test_corrupt_key_or_manifest_and_orphan_are_detected(repos):
    docs, runs, _ = repos
    doc_id = 'a' * 64
    run = new_run(doc_id)
    await docs.archive.write_run_metadata(doc_id, run.run_id, run.model_dump(mode='json'))
    with pytest.raises(ProcessingError, match='ARCHIVE_NOT_FOUND'):
        await rebuild(docs, runs)
    await docs.archive.write_document_metadata(doc_id, {'invalid': True})
    with pytest.raises(ProcessingError, match='ARCHIVE_MANIFEST_INVALID'):
        await rebuild(docs, runs)



@pytest.mark.asyncio
async def test_same_filename_different_content_and_all_run_states(repos, tmp_path):
    docs, runs, fake = repos
    a = await docs.register(await original(docs, tmp_path), 'same.pdf')
    b = await docs.register(await original(docs, tmp_path), 'same.pdf')
    assert a.document_id != b.document_id
    failed = await runs.create(new_run(a.document_id, 'same.pdf', parser='gemini'))
    failed = await runs.update(a.document_id, failed.run_id, expected_revision=1, status='failed',
        error_code='PROBE_FAILURE', completed_at=now())
    partial = await runs.create(new_run(a.document_id, 'same.pdf', parser='gpt5_mini'))
    partial = await runs.update(a.document_id, partial.run_id, expected_revision=1, status='partial',
        has_extraction=True, page_count=2, error_code='SUMMARY_FAILED', completed_at=now())
    shared = await runs.create(new_run(a.document_id, 'alias.pdf', cache='shared', cache_source_run_id=partial.run_id))
    await complete(runs, shared)
    assert await docs.indexes.count_runs(a.document_id) == 3
    assert await docs.indexes.count_runs(b.document_id) == 0
    assert (await docs.get(a.document_id)).preferred_run_id == shared.run_id
    with pytest.raises(ProcessingError, match='ARCHIVE_NOT_FOUND'):
        await docs.set_preferred(b.document_id, shared.run_id)
    with pytest.raises(ProcessingError, match='INVALID_RUN_TRANSITION'):
        await runs.update(a.document_id, failed.run_id, expected_revision=2, status='queued', completed_at=None)
    assert await rebuild(docs, runs) == {'documents': 2, 'runs': 3}
    assert {v['_source']['status'] for v in fake.indices['pdf-runs-v1']['docs'].values()} == {'completed', 'partial', 'failed'}


@pytest.mark.asyncio
async def test_missing_alias_does_not_autocreate_index(repos, tmp_path):
    docs, _, fake = repos
    fake.indices.pop('pdf-documents-v1')
    with pytest.raises(ProcessingError, match='CATALOGUE_UNAVAILABLE'):
        await docs.register(await original(docs, tmp_path), 'a.pdf')
    assert 'pdf-documents' not in fake.indices
    assert 'pdf-documents-v1' not in fake.indices


def test_mapping_matches_models_and_completed_invariants():
    assert set(DOCUMENT) == set(DocumentManifest.model_fields)
    assert set(RUN) == set(RunManifest.model_fields)
    with pytest.raises(ValidationError):
        new_run('a' * 64, status='completed', completed_at=now())
    with pytest.raises(ValidationError):
        new_run('a' * 64, cost_usd=float('nan'))


@pytest.mark.opensearch_integration
@pytest.mark.asyncio
async def test_real_catalogue_rebuild(tmp_path):
    settings = get_settings()
    store = ArchiveStore(settings)
    search = OpenSearchClient(settings.opensearch_url)
    indexes = IndexManager(search, prefix=f'pdf-test-{uuid4().hex}')
    restored = IndexManager(search, prefix=f'pdf-test-{uuid4().hex}')
    docs = DocumentRepository(store, indexes)
    runs = RunRepository(store, indexes, docs)
    doc_id = None
    connected = False
    try:
        try:
            await store.ready()
            await search.request('GET', '/_cluster/health')
        except (ProcessingError, httpx.HTTPError):
            pytest.skip('Real RustFS/OpenSearch required inside Compose')
        connected = True
        await indexes.ensure()
        # Know the cleanup scope even if a later assertion fails.
        saved = await original(docs, tmp_path)
        doc_id = saved.document_id
        initial = await docs.register(saved, 'first.pdf')
        first = await runs.create(new_run(doc_id))
        other = await runs.create(new_run(doc_id, 'renamed.pdf', cache='hit', cache_source_run_id=first.run_id))
        a, b = await complete(runs, first), await complete(runs, other)
        assert (await docs.get(doc_id)).preferred_run_id == a.run_id
        await docs.set_preferred(doc_id, b.run_id)
        await asyncio.gather(*(docs.register(saved, f'alias-{i}.pdf') for i in range(3)))
        assert len((await docs.get(doc_id)).filenames) == 5
        await indexes.publish(initial)
        await indexes.publish(first)
        assert await indexes.count_runs(doc_id) == 2
        restored_docs = DocumentRepository(store, restored)
        restored_runs = RunRepository(store, restored, restored_docs)
        assert await rebuild(restored_docs, restored_runs, document_id=doc_id) == {'documents': 1, 'runs': 2}
        assert await restored.count_runs(doc_id) == 2
        for kind, identifier, expected in [('documents', doc_id, await docs.get(doc_id)), ('runs', str(a.run_id), a), ('runs', str(b.run_id), b)]:
            name, _ = restored.names(kind)
            projected = await search.request('GET', f'/{name}/_doc/{identifier}')
            assert projected['_source'] == expected.model_dump(mode='json')
            assert projected['_version'] == expected.revision
    finally:
        try:
            if doc_id:
                async for key in store.iter_manifest_keys(doc_id):
                    await asyncio.to_thread(store.client.delete_object, Bucket=store.bucket, Key=key)
                await asyncio.to_thread(store.client.delete_object, Bucket=store.bucket, Key=original_key(doc_id))
            for manager in (indexes, restored) if connected else ():
                for kind in ('documents', 'runs'):
                    name, _ = manager.names(kind)
                    try:
                        await search.request('DELETE', f'/{name}')
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 404:
                            raise
        finally:
            await search.aclose()
            store.client.close()
