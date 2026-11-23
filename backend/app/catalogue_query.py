"""Bounded metadata discovery; never accept raw OpenSearch DSL from callers."""
import asyncio
import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, ValidationError

from app.catalogue_models import DocumentID, DocumentManifest, RunManifest
from app.errors import ProcessingError
from app.index_manager import unavailable
from app.schemas import JobState, ParserChoice


class Filters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    limit: int = Field(50, ge=1, le=100)
    cursor: str | None = Field(None, min_length=1, max_length=512)
    filename: str | None = Field(None, min_length=1, max_length=200)
    parser: ParserChoice | None = None
    status: JobState | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    sort: Literal['newest', 'oldest'] = 'newest'

    @field_validator('filename')
    @classmethod
    def validate_name(cls, value):
        if value is not None and (not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError('Nonblank filename fragment required')
        return value

    @field_validator('created_from', 'created_to')
    @classmethod
    def timezone(cls, value):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError('Timezone required')
            return value.astimezone(UTC)
        return value

    @model_validator(mode='after')
    def dates(self):
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError('created_from must not follow created_to')
        return self


class RunFilters(Filters):
    document_id: DocumentID | None = None


class LibraryFilters(Filters):
    sha256: DocumentID | None = None


def invalid_cursor():
    return ProcessingError('INVALID_CURSOR', 'Invalid cursor or filters changed; restart the list.', 422)


def cursor_key(kind, filters):
    payload = json.dumps([kind, filters.model_dump(mode='json', exclude={'cursor', 'limit'})], sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def encode_cursor(key, values):
    return base64.urlsafe_b64encode(json.dumps([key, values], separators=(',', ':')).encode()).decode().rstrip('=')


def decode_cursor(token, key, manifest_type):
    if token is None:
        return None
    import re
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', token):
            raise ValueError()
        data = json.loads(base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True))
        if not isinstance(data, list) or len(data) != 2 or data[0] != key:
            raise ValueError()
        values = data[1]
        validate_sort(values, manifest_type)
        return values
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise invalid_cursor() from exc


def validate_sort(values, manifest_type):
    import re
    pattern = r'[0-9a-f]{64}' if manifest_type is DocumentManifest else r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    if (not isinstance(values, list) or len(values) != 2 or type(values[0]) is not int
            or not -(2**63) <= values[0] < 2**63 or not isinstance(values[1], str)
            or not re.fullmatch(pattern, values[1])):
        raise ValueError('Invalid sort tuple')


def filename_filter(field, text):
    # Treat user-supplied *, ?, and backslashes literally, not as query syntax.
    escaped = text.replace('\\', '\\\\').replace('*', '\\*').replace('?', '\\?')
    return {'wildcard': {field: {'value': '*' + escaped + '*', 'case_insensitive': True}}}


def exact(field, value):
    return {'term': {field: value}}


def query(filters):
    return {'bool': {'filter': filters}} if filters else {'match_all': {}}


def run_terms(filters):
    return [exact(k, getattr(filters, k).value) for k in ('parser', 'status') if getattr(filters, k) is not None]


async def matching_documents(indexes, filters):
    """Library parser/status matches any one run, not independent joins.

    Bound expansion explicitly; never silently truncate a library result.
    """
    _, alias = indexes.names('runs')
    out = []
    after = None
    while True:
        composite = {'size': 500, 'sources': [{'document_id': {'terms': {'field': 'document_id'}}}]}
        if after is not None:
            composite['after'] = after
        body = {'size': 0, 'query': query(run_terms(filters)), 'aggs': {'documents': {'composite': composite}}}
        data = await indexes.client.request('POST', f'/{alias}/_search', timeout=15., params={'allow_partial_search_results': 'false'}, json=body)
        check_search(data)
        agg = data['aggregations']['documents']
        buckets = agg['buckets']
        if not buckets:
            return out
        for item in buckets:
            value = item['key']['document_id']
            import re
            if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
                raise ValueError('Invalid document identity')
            out.append(value)
        if len(out) > 10000:
            raise ProcessingError('FILTER_TOO_BROAD', 'Library run filter matches too many documents; use the run history instead.', 422)
        next_after = agg.get('after_key')
        if next_after is None:
            return out
        if next_after == after:
            raise ValueError('Composite pagination did not advance')
        after = next_after


def check_search(data):
    if data.get('timed_out') is not False or data['_shards']['failed'] != 0:
        raise ValueError('Partial catalogue search')


async def search_page(indexes, kind, filters):
    manifest_type = DocumentManifest if kind == 'documents' else RunManifest
    id_field = 'document_id' if kind == 'documents' else 'run_id'
    key = cursor_key(kind, filters)
    after = decode_cursor(filters.cursor, key, manifest_type)
    terms = []
    if filters.filename:
        terms.append(filename_filter('filenames.keyword' if kind == 'documents' else 'filename.keyword', filters.filename))
    date_bounds = {op: val.isoformat() for op, val in (('gte', filters.created_from), ('lte', filters.created_to)) if val is not None}
    if date_bounds:
        terms.append({'range': {'created_at': date_bounds}})
    try:
        if kind == 'runs':
            if filters.document_id:
                terms.append(exact('document_id', filters.document_id))
            terms.extend(run_terms(filters))
        else:
            if filters.sha256:
                terms.append(exact('sha256', filters.sha256))
            if filters.parser or filters.status:
                async with asyncio.timeout(15):
                    matching = await matching_documents(indexes, filters)
                if not matching:
                    return [], None
                terms.append({'terms': {'document_id': matching}})
        _, alias = indexes.names(kind)
        payload = {'size': filters.limit + 1, 'track_total_hits': False, 'query': query(terms),
            'sort': [{'created_at': 'desc' if filters.sort == 'newest' else 'asc'}, {id_field: 'asc'}]}
        if after is not None:
            payload['search_after'] = after
        data = await indexes.client.request('POST', f'/{alias}/_search', timeout=15., params={'allow_partial_search_results': 'false'}, json=payload)
        check_search(data)
        hits = data['hits']['hits']
        if not isinstance(hits, list) or len(hits) > filters.limit + 1:
            raise ValueError('Invalid search response')
        items = []
        for hit in hits[:filters.limit]:
            manifest = manifest_type.model_validate(hit['_source'])
            validate_sort(hit['sort'], manifest_type)
            if hit['_id'] != str(getattr(manifest, id_field)) or hit['sort'][1] != hit['_id']:
                raise ValueError('Mismatched catalogue identity')
            items.append(manifest)
        next_cursor = encode_cursor(key, hits[filters.limit-1]['sort']) if len(hits) > filters.limit else None
        return items, next_cursor
    except (httpx.HTTPError, ValueError, KeyError, TypeError, ValidationError, TimeoutError) as exc:
        raise unavailable() from exc
