"""Stage 5 HTTP contracts; real service gate uses the existing offline harness."""
import asyncio
from datetime import datetime, UTC
import hashlib
import json
import re
from uuid import UUID, uuid4

import httpx
import pytest

from app.archive_store import ArchiveStore
from app.config import get_settings
from app.document_repository import DocumentRepository
from app.errors import ProcessingError
from app.index_manager import IndexManager
from app.library_api import router
from app.opensearch_client import OpenSearchClient
from app.run_repository import RunRepository
from tests.fake_redis import FakeRedis
from tests.test_catalogue import SearchFake, PagedS3
from tests.test_persistent_pipeline import Harness


def millis(s):
    return int(datetime.fromisoformat(s).timestamp() * 1000)


class QueryFake(SearchFake):
    """Local approximation of tested DSL, not proof of OpenSearch behavior."""
    def __call__(self, request):
        if self.offline or not request.url.path.endswith('/_search'):
            return super().__call__(request)
        name = request.url.path.strip('/').split('/')[0]
        index = next(v for k, v in self.indices.items() if name == k or name in v['aliases'])
        body = json.loads(request.content)
        assert request.url.params['allow_partial_search_results'] == 'false'
        items = [(k, v['_source']) for k, v in index['docs'].items()]
        for filter in body['query'].get('bool', {}).get('filter', []):
            op, fields = next(iter(filter.items()))
            key, value = next(iter(fields.items()))
            if op == 'term':
                items = [(k, v) for k, v in items if v[key] == value]
            elif op == 'terms':
                items = [(k, v) for k, v in items if v[key] in value]
            elif op == 'range':
                items = [(k, v) for k, v in items if all(
                    millis(v[key]) >= millis(bound) if which == 'gte' else millis(v[key]) <= millis(bound)
                    for which, bound in value.items())]
            elif op == 'wildcard':
                assert value['case_insensitive'] is True
                parts = re.findall(r'\\.|.|\n', value['value'])
                pattern = ''.join(re.escape(p[1]) if p.startswith('\\') else '.*' if p == '*' else '.' if p == '?' else re.escape(p) for p in parts)
                def matches(v):
                    names = v[key.removesuffix('.keyword')]
                    return any(re.fullmatch(pattern, n, re.I) for n in ([names] if isinstance(names, str) else names))
                items = [(k, v) for k, v in items if matches(v)]
            else:
                raise AssertionError(filter)
        result = {'timed_out': False, '_shards': {'failed': 0}}
        if body['size'] == 0 and 'documents' not in body.get('aggs', {}):
            result['hits'] = {'total': {'value': len(items), 'relation': 'eq'}, 'hits': []}
            result['aggregations'] = {name: {'buckets': {label: {'doc_count': sum(v[field] == val for _, v in items)}
                for label, condition in spec['filters']['filters'].items() for field, val in condition['term'].items()}}
                for name, spec in body.get('aggs', {}).items()}
        elif 'aggs' in body:
            comp = body['aggs']['documents']['composite']
            ids = sorted({v['document_id'] for _, v in items})
            ids = [i for i in ids if i > comp.get('after', {}).get('document_id', '')][:comp['size']]
            agg = {'buckets': [{'key': {'document_id': i}} for i in ids]}
            if ids:
                agg['after_key'] = {'document_id': ids[-1]}
            result['aggregations'] = {'documents': agg}
        elif body.get('version') is True and len(body['sort']) == 1:
            field = next(iter(body['sort'][0]))
            after = body.get('search_after', [''])[0]
            result['hits'] = {'hits': [{'_id': i, '_source': {k:v[k] for k in set(body['_source'])}, '_version': index['docs'][i]['_version'], 'sort':[v[field]]} for i,v in sorted(items) if i > after][:body['size']]}
        else:
            idfield = next(iter(body['sort'][1]))
            factor = -1 if body['sort'][0]['created_at'] == 'desc' else 1
            def key(sort): return (sort[0] * factor, sort[1])
            hits = [{'_id': i, '_source': v, 'sort': [millis(v['created_at']), v[idfield]]} for i, v in items]
            hits.sort(key=lambda h: key(h['sort']))
            if 'search_after' in body:
                hits = [h for h in hits if key(h['sort']) > key(body['search_after'])]
            result['hits'] = {'hits': hits[:body['size']]}
        return httpx.Response(200, json=result)


@pytest.fixture
async def api_harness(settings):
    store = ArchiveStore(settings, client=PagedS3())
    await store.prepare()
    fake = QueryFake()
    search = OpenSearchClient('http://opensearch:9200', transport=httpx.MockTransport(fake))
    indexes = IndexManager(search)
    await indexes.ensure()
    docs = DocumentRepository(store, indexes)
    h = Harness(settings, docs, RunRepository(store, indexes, docs), FakeRedis())
    h.app.include_router(router)
    await h.queue.ensure_group()
    try:
        yield h, fake
    finally:
        await search.aclose()


async def expect(http, method, path, code=200, **kwargs):
    r = await http.request(method, '/api/v1' + path, **kwargs)
    assert r.status_code == code, r.text
    return r


async def api_exercise(h, data):
    sha = hashlib.sha256(data).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        assert (await expect(http, 'GET', '/runs')).json() == {'items': [], 'next_cursor': None}
        first, a = await h.upload(http, data, 'first.pdf')
        assert first['document_id'] == sha
        initial = (await expect(http, 'GET', f'/runs/{a}')).json()
        assert initial['status'] == 'queued' and initial['summary'] is None
        await expect(http, 'GET', f'/runs/{a}/markdown', 409)
        await expect(http, 'PATCH', f'/library/{sha}/preferred-run', 409, json={'run_id': str(a)})
        shared, b = await h.upload(http, data, 'Other NAME.pdf')
        assert shared['cache'] == 'shared'
        await h.one(); await h.one()
        await h.forget(a); await h.forget(b)
        # Archived copies remain independently readable without the Redis keys.
        result = (await expect(http, 'GET', f'/runs/{b}')).json()
        assert result['filename'] == 'Other NAME.pdf' and result['summary'] == 'Summary: α + $x^2$'
        assert result['document_id'] == sha and result['status'] == 'completed'
        assert not {'worker_fence', 's3_prefix', 'retry_id', 'extraction_ref', 'summary_ref'} & result.keys()
        io = await expect(http, 'GET', f'/library/{sha}/original')
        assert io.content == data and io.headers['content-type'] == 'application/pdf'
        assert io.headers['cache-control'] == 'no-store'
        legacy = (await expect(http, 'GET', f'/documents/{b}/pages')).json()
        assert (await expect(http, 'GET', f'/runs/{b}/pages')).json() == legacy
        md = await expect(http, 'GET', f'/runs/{b}/markdown')
        run = await h.pipeline.get(b)
        saved = await h.docs.archive.read_run_artifact(sha, b, 'document.md', generation=run.extraction_ref)
        assert md.content == saved.data and md.headers['content-type'].startswith('text/markdown')
        assert (await expect(http, 'GET', f'/runs/{b}/summary')).text == result['summary']
        doc = (await expect(http, 'GET', f'/library/{sha}')).json()
        assert doc['preferred_run_id'] == str(a) and len(doc['filenames']) == 2
        doc = (await expect(http, 'PATCH', f'/library/{sha}/preferred-run', json={'run_id': str(b)})).json()
        assert doc['preferred_run_id'] == str(b)
        doc = (await expect(http, 'PATCH', f'/library/{sha}', json={'display_name': 'Νέο όνομα.pdf'})).json()
        assert doc['display_name'] == 'Νέο όνομα.pdf' and doc['preferred_run_id'] == str(b)
        assert set(doc['filenames']) == {'first.pdf', 'Other NAME.pdf', 'Νέο όνομα.pdf'}
        assert 's3_bucket' not in doc and 's3_object_key' not in doc
        after_rename = await expect(http, 'GET', f'/library/{sha}/original')
        assert after_rename.content == data and 'filename*=UTF-8\'\'' in after_rename.headers['content-disposition']
        assert (await expect(http, 'GET', f'/runs/{a}')).json()['filename'] == 'first.pdf'
        assert len((await expect(http, 'GET', '/library', params={'filename': 'other name', 'parser': 'pypdf', 'status': 'completed'})).json()['items']) == 1
        assert (await expect(http, 'GET', '/library', params={'status': 'failed'})).json()['items'] == []
        # A different PDF under the same name remains a distinct document.
        h.summary.failure = True
        _, c = await h.upload(http, data + b'\n% second PDF\n', 'first.pdf')
        await h.one(); await h.forget(c)
        second_sha = (await h.pipeline.get(c)).document_id
        assert second_sha != sha
        await expect(http, 'PATCH', f'/library/{sha}/preferred-run', 409, json={'run_id': str(c)})
        partial = (await expect(http, 'GET', f'/runs/{c}')).json()
        assert partial['status'] == 'partial' and partial['has_extraction'] and not partial['has_summary']
        await expect(http, 'GET', f'/runs/{c}/summary', 409)
        await expect(http, 'GET', f'/runs/{c}/markdown')
        # Cursor, including explicit invalidation if a filter changes.
        p1 = (await expect(http, 'GET', '/runs', params={'limit': 1})).json()
        assert p1['items'][0]['run_id'] == str(c) and p1['next_cursor']
        p2 = (await expect(http, 'GET', '/runs', params={'limit': 2, 'cursor': p1['next_cursor']})).json()
        assert {i['run_id'] for i in p2['items']} == {str(a), str(b)} and p2['next_cursor'] is None
        assert all('summary' not in i for i in p1['items'] + p2['items'])
        await expect(http, 'GET', '/runs', 422, params={'cursor': p1['next_cursor'], 'status': 'completed'})
        filtered = (await expect(http, 'GET', '/runs', params={'filename': 'other NAME', 'status': 'completed', 'parser': 'pypdf'})).json()
        assert [i['run_id'] for i in filtered['items']] == [str(b)]
        assert (await expect(http, 'GET', '/runs', params={'filename': '*'})).json()['items'] == []
        assert len((await expect(http, 'GET', '/runs', params={'created_from': '2000-01-01T00:00:00Z', 'created_to': '2100-01-01T00:00:00Z'})).json()['items']) == 3
        assert not (await expect(http, 'GET', '/runs', params={'created_from': '2100-01-01T00:00:00Z'})).json()['items']
        assert len((await expect(http, 'GET', f'/library/{sha}/runs')).json()['items']) == 2
        lib = (await expect(http, 'GET', '/library', params={'limit': 1, 'sort': 'oldest'})).json()
        assert lib['items'][0]['document_id'] == sha and lib['next_cursor']
        lib2 = (await expect(http, 'GET', '/library', params={'cursor': lib['next_cursor'], 'sort': 'oldest'})).json()
        assert lib2['items'][0]['document_id'] == second_sha and lib2['next_cursor'] is None
        assert (await expect(http, 'GET', '/library', params={'sha256': sha})).json()['items'][0]['document_id'] == sha
        assert (await expect(http, 'GET', '/library', params={'status': 'partial'})).json()['items'][0]['document_id'] == second_sha
        for params in ({'limit': 101}, {'cursor': 'garbage'}, {'status': 'bogus'}, {'unknown': 'x'}, {'filename': ' '}, {'created_from': '2020-01-01T00:00:00'}, {'created_from': '2100-01-01T00:00:00Z', 'created_to': '2000-01-01T00:00:00Z'}):
            await expect(http, 'GET', '/runs', 422, params=params)
        await expect(http, 'GET', '/runs/not-a-uuid', 422)
        await expect(http, 'GET', '/library/not-a-sha', 422)
        await expect(http, 'GET', f'/runs/{uuid4()}', 404)
        await expect(http, 'GET', '/library/' + '0'*64, 404)
        await expect(http, 'PATCH', f'/library/{sha}', 422, json={'display_name': '../bad'})
        await expect(http, 'DELETE', f'/runs/{uuid4()}', 404)
        h.summary.failure = False
        await expect(http, 'POST', f'/runs/{c}/retry-summary', 202)
        await expect(http, 'POST', f'/runs/{c}/retry-summary', 409)
        await h.one()
        assert (await expect(http, 'GET', f'/runs/{c}')).json()['status'] == 'completed'
        assert h.summary.calls == 3
        await expect(http, 'POST', f'/runs/{c}/retry-summary', 409)


@pytest.mark.asyncio
async def test_library_http_contract(api_harness, sample_basic):
    h, _ = api_harness
    await api_exercise(h, sample_basic.read_bytes())


@pytest.mark.asyncio
async def test_catalogue_outages_and_missing_artifacts(api_harness, sample_basic):
    h, fake = api_harness
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        _, uid = await h.upload(http, sample_basic.read_bytes(), 'one.pdf')
        await h.one()
        run = await h.pipeline.get(uid)
        fake.offline = True
        r = await expect(http, 'GET', '/runs', 503)
        assert r.json()['error']['code'] == 'CATALOGUE_UNAVAILABLE'
        await expect(http, 'GET', '/library', 503)
        await expect(http, 'GET', f'/runs/{uid}')
        await expect(http, 'GET', f'/library/{run.document_id}/original')
        fake.offline = False
        h.docs.archive.client.offline = True
        await expect(http, 'GET', '/runs', 503)
        await expect(http, 'GET', f'/runs/{uid}', 503)
        h.docs.archive.client.offline = False
        key = h.docs.archive._artifact_key(run.document_id, uid, 'document.md', run.extraction_ref)
        del h.docs.archive.client.objects[h.docs.archive.bucket, key]
        await expect(http, 'GET', f'/runs/{uid}/markdown', 503)


@pytest.mark.asyncio
@pytest.mark.pipeline_integration
async def test_real_library_http_contract(real_redis, settings, sample_basic):
    real = get_settings()
    store = ArchiveStore(real)
    search = OpenSearchClient(real.opensearch_url)
    indexes = IndexManager(search, prefix=f'pdf-test-{uuid4().hex}')
    docs = DocumentRepository(store, indexes)
    h = Harness(settings, docs, RunRepository(store, indexes, docs), real_redis)
    h.app.include_router(router)
    connected = False
    try:
        try:
            await store.ready()
            await search.request('GET', '/_cluster/health')
        except (ProcessingError, httpx.HTTPError):
            pytest.skip('Real RustFS and OpenSearch required')
        connected = True
        await indexes.ensure()
        await h.queue.ensure_group()
        await api_exercise(h, sample_basic.read_bytes() + f'\n% stage5-{uuid4()}\n'.encode())
    finally:
        if connected:
            await h.clean(real=True)
            for kind in ('documents', 'runs'):
                name, _ = indexes.names(kind)
                try:
                    await search.request('DELETE', f'/{name}')
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
        await search.aclose()
        store.client.close()


@pytest.mark.asyncio
async def test_cursor_ties_and_correlated_run_filters(api_harness, tmp_path):
    from app.catalogue_models import RunManifest, now
    from tests.test_catalogue import original
    h, _ = api_harness
    saved = await original(h.docs, tmp_path)
    await h.docs.register(saved, 'tie.pdf')
    timestamp = now()
    ids = []
    for parser, status in [('pypdf', 'queued'), ('gemini', 'failed'), ('pypdf', 'queued')]:
        uid = uuid4()
        ids.append(str(uid))
        await h.runs.create(RunManifest(run_id=uid, document_id=saved.document_id, filename='tie.pdf', parser=parser,
            status=status, pipeline_version='test', application_version='test', created_at=timestamp, updated_at=timestamp,
            completed_at=timestamp if status == 'failed' else None, error_code='TEST' if status == 'failed' else None,
            s3_prefix=f'{saved.document_id}/runs/{uid}/'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        observed = []
        cursor = None
        while True:
            params = {'limit': 1, **({'cursor': cursor} if cursor else {})}
            response = (await expect(http, 'GET', '/runs', params=params)).json()
            observed.extend(r['run_id'] for r in response['items'])
            cursor = response['next_cursor']
            if cursor is None:
                break
            assert len(observed) <= 3
        assert observed == sorted(ids)
        assert (await expect(http, 'GET', '/library', params={'parser': 'pypdf', 'status': 'failed'})).json()['items'] == []
        assert len((await expect(http, 'GET', '/library', params={'parser': 'gemini', 'status': 'failed'})).json()['items']) == 1


@pytest.mark.asyncio
async def test_archive_rename_commits_before_projection_and_can_replay(api_harness, sample_basic):
    h, fake = api_harness
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://test') as http:
        _, uid = await h.upload(http, sample_basic.read_bytes(), 'original.pdf')
        await h.one()
        doc_id = (await h.pipeline.get(uid)).document_id
        fake.offline = True
        await expect(http, 'PATCH', f'/library/{doc_id}', 503, json={'display_name': 'renamed.pdf'})
        doc = (await expect(http, 'GET', f'/library/{doc_id}')).json()
        assert doc['display_name'] == 'renamed.pdf'
        fake.offline = False
        refreshed = (await expect(http, 'PATCH', f'/library/{doc_id}', json={'display_name': 'renamed.pdf'})).json()
        assert refreshed['revision'] == doc['revision']
        assert (await expect(http, 'GET', '/library', params={'filename': 'renamed.pdf'})).json()['items'][0]['document_id'] == doc_id
