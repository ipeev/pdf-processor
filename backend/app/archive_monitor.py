"""Bounded, cached archive aggregates. No filenames, IDs or result bodies."""
import asyncio
import re
import time
from datetime import datetime, UTC

STATES = ('queued', 'processing', 'summarizing', 'completed', 'partial', 'failed')
CACHE = ('miss', 'hit', 'shared')
DOC = r'[0-9a-f]{64}'
RUN = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
ORIGINAL = re.compile(rf'{DOC}/original\.pdf')
DOCUMENT = re.compile(rf'{DOC}/metadata\.json')
RUN_MANIFEST = re.compile(rf'{DOC}/runs/{RUN}/metadata\.json')
ARTIFACT = re.compile(rf'{DOC}/runs/{RUN}/(?:artifacts/[0-9a-f]{{32}}/)?(?:pages\.json|document\.md|summary\.md|figures\.json|images/p[0-9]{{4}}-figure-[A-Za-z0-9_-]{{1,70}}\.png)')


VISION_OBJECT = re.compile(rf'{DOC}/runs/{RUN}/vision/(?:admissions|responses|failures)/p[0-9]{{4}}(?:-a0[23])?\.json')
VISION_CACHE = re.compile(rf'{DOC}/vision-cache/[0-9a-f]{{64}}\.json')

def blank():
    return {'status': 'unavailable', 'checked_at': None, 'truncated': False,
            **dict.fromkeys(('objects', 'bytes', 'originals', 'original_bytes', 'artifact_bytes', 'metadata_bytes',
                             'documents', 'runs', 'indexed_documents', 'indexed_runs', 'duplicate_submissions', 'reused_original_runs'), None),
            'run_status': dict.fromkeys(STATES), 'run_cache': dict.fromkeys(CACHE),
            'consistency': 'unknown', 'deletion_pending': None, 'reconciliation_needed': None,
            'restore_pending': None, 'recovery': {'state': 'unknown', 'checked_at': None, 'recovered': None}}


def utc():
    return datetime.now(UTC).isoformat()


def number(value):
    if type(value) is not int or value < 0 or value > 2**53-1:
        raise ValueError('Invalid archive metric')
    return value


async def inventory(archive, *, max_pages=10):
    counts = dict.fromkeys(('objects', 'bytes', 'originals', 'original_bytes', 'artifact_bytes', 'metadata_bytes', 'documents', 'runs'), 0)
    token = None
    run_docs = set()
    for _ in range(max_pages):
        args = {'Bucket': archive.bucket, 'Prefix': '', 'MaxKeys': 1000}
        if token is not None:
            args['ContinuationToken'] = token
        page = await asyncio.to_thread(archive.client.list_objects_v2, **args)
        entries = page.get('Contents', [])
        if len(entries) > 1000:
            raise ValueError('Oversized archive page')
        for item in entries:
            key, size = item['Key'], number(item['Size'])
            counts['objects'] += 1
            counts['bytes'] += size
            if ORIGINAL.fullmatch(key):
                counts['originals'] += 1
                counts['original_bytes'] += size
            elif ARTIFACT.fullmatch(key):
                counts['artifact_bytes'] += size
            else:
                counts['metadata_bytes'] += size
                counts['documents'] += int(bool(DOCUMENT.fullmatch(key)))
                if RUN_MANIFEST.fullmatch(key):
                    counts['runs'] += 1
                    run_docs.add(key.split('/')[0])
        if not page.get('IsTruncated'):
            return {**counts, 'truncated': False, 'reused_original_runs': counts['runs']-len(run_docs)}
        next_token = page.get('NextContinuationToken')
        if not next_token or next_token == token:
            raise ValueError('Invalid archive cursor')
        token = next_token
    return {**counts, 'truncated': True}


async def projections(indexes):
    async def query(kind, aggs=None):
        _, alias = indexes.names(kind)
        body = {'size': 0, 'track_total_hits': True, 'query': {'match_all': {}}}
        if aggs:
            body['aggs'] = aggs
        data = await indexes.client.request('POST', f'/{alias}/_search', json=body,
            params={'allow_partial_search_results': 'false'})
        if data.get('timed_out') is not False or data['_shards']['failed'] != 0 or data['hits']['total']['relation'] != 'eq':
            raise ValueError('Incomplete catalogue counts')
        return number(data['hits']['total']['value']), data.get('aggregations', {})
    doc, _ = await query('documents')
    runs, aggs = await query('runs', {name: {'filters': {'filters': {v: {'term': {field: v}} for v in values}}}
        for name, field, values in (('status', 'status', STATES), ('cache', 'cache', CACHE))})
    def extract(name, values):
        result = {v: number(aggs[name]['buckets'][v]['doc_count']) for v in values}
        if sum(result.values()) != runs:
            raise ValueError('Unexpected catalogue state')
        return result
    return {'indexed_documents': doc, 'indexed_runs': runs, 'run_status': extract('status', STATES), 'run_cache': extract('cache', CACHE)}


class ArchiveMonitor:
    def __init__(self, archive, indexes, recovery, *, ttl=30):
        self.archive, self.indexes, self.recovery = archive, indexes, recovery
        self.ttl, self.expires, self.value = ttl, 0, blank()
        self.lock = asyncio.Lock()

    async def snapshot(self):
        async with self.lock:
            if time.monotonic() >= self.expires:
                result = blank()
                async def storage():
                    try:
                        stats = await inventory(self.archive)
                        pending = await self.archive.deletion_journal() is not None
                        dirty = await self.archive.reconciliation_needed()
                        restore = await self.archive.restore_pending()
                        result.update(stats, status='online', deletion_pending=pending, reconciliation_needed=dirty, restore_pending=restore)
                    except Exception:
                        pass
                async def search():
                    try:
                        result.update(await projections(self.indexes))
                    except Exception:
                        pass
                async def bounded(fn):
                    try:
                        await asyncio.wait_for(fn(), timeout=7)
                    except TimeoutError:
                        pass
                await asyncio.gather(bounded(storage), bounded(search))
                if result['deletion_pending'] or result['reconciliation_needed'] or result['restore_pending']:
                    result['consistency'] = 'attention'
                elif result['status'] == 'online' and not result['truncated'] and result['indexed_documents'] is not None:
                    result['consistency'] = ('counts_match' if result['documents'] == result['originals'] == result['indexed_documents']
                        and result['runs'] == result['indexed_runs'] else 'count_mismatch')
                result['checked_at'] = utc()
                self.value, self.expires = result, time.monotonic()+self.ttl
            return {**self.value, 'duplicate_submissions': None if self.value['indexed_runs'] is None else
                    self.value['run_cache']['hit']+self.value['run_cache']['shared'], 'recovery': dict(self.recovery)}
