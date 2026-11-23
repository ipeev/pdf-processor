"""Strict versioned metadata indexes; RustFS, not OpenSearch, is authoritative."""
from __future__ import annotations
import re
import httpx
from app.catalogue_models import DocumentManifest, RunManifest
from app.errors import ProcessingError
from app.opensearch_client import OpenSearchClient


def keyword(): return {'type': 'keyword'}
def date(): return {'type': 'date'}
def integer(): return {'type': 'long'}
def boolean(): return {'type': 'boolean'}
def filename(): return {'type': 'text', 'fields': {'keyword': {'type': 'keyword', 'ignore_above': 1024}}}

COMMON = {'schema_version': integer(), 'revision': integer(), 'created_at': date(), 'updated_at': date()}
DOCUMENT = {**COMMON, 'document_id': keyword(), 'sha256': keyword(), 'filename': filename(),
            'filenames': filename(), 'file_size_bytes': integer(), 'page_count': integer(),
            'preferred_run_id': keyword(), 's3_bucket': keyword(), 's3_object_key': keyword()}
RUN = {**COMMON, **{k: keyword() for k in ('run_id', 'document_id', 'upload_id', 'parser', 'extraction_model',
    'summary_model', 'pipeline_version', 'application_version', 'processing_fingerprint', 'status', 'cache',
    'cache_source_run_id', 'trace_id', 'error_code', 's3_prefix')}, 'filename': filename(),
    **{k: integer() for k in ('page_count', 'duration_ms', 'worker_attempts', 'input_tokens', 'output_tokens')},
    'started_at': date(), 'completed_at': date(), 'cost_usd': {'type': 'double'},
    'error_message': {'type': 'text', 'index': False}, 'has_extraction': boolean(), 'has_summary': boolean(),
    'worker_fence': {'type': 'keyword', 'index': False, 'doc_values': False},
    'retry_id': keyword(), 'extraction_ref': keyword(), 'summary_ref': keyword(), 'error_diagnostic': {'type': 'object', 'enabled': False}}


def unavailable():
    return ProcessingError('CATALOGUE_UNAVAILABLE', 'OpenSearch catalogue is unavailable; archived metadata is authoritative.', 503)


def incompatible():
    return ProcessingError('CATALOGUE_SCHEMA_MISMATCH', 'OpenSearch catalogue mapping or alias is incompatible.', 500)


class IndexManager:
    def __init__(self, client: OpenSearchClient, *, prefix: str = 'pdf'):
        # Alternate namespaces are exclusively for isolated tests/reconstruction.
        if prefix != 'pdf' and not re.fullmatch(r'pdf-test-[0-9a-f]{32}', prefix):
            raise ValueError('Invalid catalogue namespace')
        self.client = client
        self.prefix = prefix

    def names(self, kind):
        if kind not in ('documents', 'runs'):
            raise ValueError('Invalid index kind')
        alias = f'{self.prefix}-{kind}'
        return f'{alias}-v1', alias

    async def ensure(self, *, create=True):
        for kind, properties in (('documents', DOCUMENT), ('runs', RUN)):
            index, alias = self.names(kind)
            mapping = {'dynamic': 'strict', '_meta': {'schema_version': 1}, 'properties': properties}
            try:
                try:
                    response = await self.client.request('GET', f'/{index}/_mapping')
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404 or not create:
                        raise
                    try:
                        await self.client.request('PUT', f'/{index}', timeout=30.0, json={
                            'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
                            'mappings': mapping, 'aliases': {alias: {}}})
                    except httpx.HTTPStatusError as race:
                        if race.response.status_code != 400 or 'resource_already_exists_exception' not in race.response.text:
                            raise
                    response = await self.client.request('GET', f'/{index}/_mapping')
                actual = response[index]['mappings']
                # OpenSearch can omit the implicit 'object' type on a disabled
                # object mapper. Treat only that equivalent representation as
                # identical; all other schema/alias checks remain strict.
                if kind == 'runs':
                    fields = actual.get('properties', {})
                    if fields.get('error_diagnostic') == {'enabled': False}:
                        actual = {**actual, 'properties': {**fields, 'error_diagnostic': {'type': 'object', 'enabled': False}}}
                if any(actual.get(k) != v for k, v in mapping.items()):
                    raise incompatible()
                aliases = await self.client.request('GET', f'/_alias/{alias}')
                if set(aliases) != {index} or aliases[index]['aliases'].get(alias) != {}:
                    raise incompatible()
                settings = await self.client.request('GET', f'/{index}/_settings')
                config = settings[index]['settings']['index']
                if str(config['number_of_shards']) != '1' or str(config['number_of_replicas']) != '0':
                    raise incompatible()
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise unavailable() from exc

    async def publish(self, manifest: DocumentManifest | RunManifest):
        kind, identifier = ('documents', manifest.document_id) if isinstance(manifest, DocumentManifest) else ('runs', str(manifest.run_id))
        index, alias = self.names(kind)
        payload = manifest.model_dump(mode='json')
        try:
            try:
                await self.client.request('PUT', f'/{alias}/_doc/{identifier}', timeout=30.0, params={
                    'version': manifest.revision, 'version_type': 'external', 'refresh': 'wait_for', 'require_alias': 'true'}, json=payload)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
                current = await self.client.request('GET', f'/{index}/_doc/{identifier}')
                # An older projection cannot overwrite a newer revision. An
                # identical revision is idempotent, but different content is corruption.
                if current['_source']['document_id'] != manifest.document_id or current['_version'] < manifest.revision or (
                    current['_version'] == manifest.revision and current['_source'] != payload):
                    raise incompatible()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise unavailable() from exc

    async def count_runs(self, document_id: str) -> int:
        _, alias = self.names('runs')
        try:
            data = await self.client.request('POST', f'/{alias}/_count', json={'query': {'term': {'document_id': document_id}}})
            return OpenSearchClient._count(data['count'])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise unavailable() from exc

    async def remove(self, kind, identifier):
        """Exclusive maintenance only. Avoid incrementing absent-record versions."""
        index, _ = self.names(kind)
        try:
            try:
                await self.client.request('GET', f'/{index}/_doc/{identifier}')
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return
                raise
            await self.client.request('DELETE', f'/{index}/_doc/{identifier}', params={'refresh': 'true'})
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise unavailable() from exc

    async def inventory(self, kind):
        """Exact IDs and external versions, scanned with stable keyword order."""
        _, alias = self.names(kind)
        field = 'document_id' if kind == 'documents' else 'run_id'
        after = None
        id_pattern = r'[0-9a-f]{64}' if kind == 'documents' else r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
        try:
            while True:
                body = {'size': 250, 'version': True, 'query': {'match_all': {}},
                        '_source': list(dict.fromkeys(['document_id', field])), 'sort': [{field: 'asc'}]}
                if after is not None:
                    body['search_after'] = [after]
                result = await self.client.request('POST', f'/{alias}/_search', timeout=15.,
                    params={'allow_partial_search_results': 'false'}, json=body)
                if result.get('timed_out') is not False or result['_shards']['failed'] != 0:
                    raise ValueError('Partial catalogue inventory')
                hits = result['hits']['hits']
                if not isinstance(hits, list) or len(hits) > 250:
                    raise ValueError('Invalid catalogue inventory')
                for hit in hits:
                    key, doc, version = hit['_id'], hit['_source']['document_id'], hit['_version']
                    if (not re.fullmatch(id_pattern, key) or not re.fullmatch(r'[0-9a-f]{64}', doc)
                        or hit['_source'][field] != key or hit['sort'] != [key]
                        or (after is not None and key <= after) or type(version) is not int or not 0 < version < 2**63-2):
                        raise ValueError('Invalid catalogue identity/version')
                    after = key
                    yield key, doc, version
                if len(hits) < 250:
                    break
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise unavailable() from exc
