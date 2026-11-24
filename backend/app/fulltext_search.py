"""Rebuildable, provider-free passage index for committed RustFS extractions.

The permanent archive remains authoritative. OpenSearch contains only a derived
read model; no PDF extraction, OpenRouter request or stored embedding is needed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.catalogue_models import DocumentID
from app.errors import ProcessingError
from app.opensearch_client import OpenSearchClient
from app.schemas import JobState, ParserChoice

log = logging.getLogger(__name__)
INDEX = 'pdf-passages-v1'
ALIAS = 'pdf-passages'
PASSAGE_WORDS = 220
MAX_PASSAGES_PER_DOCUMENT = 20_000
MARKDOWN_IMAGE = re.compile(r'!\[[^\]\n]*\]\(images/[^)\n]+\)')
HEADING = re.compile(r'^#{1,6}\s+(.*)$')
ID = re.compile(r'^[0-9a-f]{64}$')

MAPPING = {
    'dynamic': 'strict',
    '_meta': {'schema_version': 1, 'purpose': 'rebuildable-pdf-passages'},
    'properties': {
        'document_id': {'type': 'keyword'},
        'run_id': {'type': 'keyword'},
        'extraction_ref': {'type': 'keyword'},
        'parser': {'type': 'keyword'},
        'primary': {'type': 'boolean'},
        'filename': {'type': 'text'},
        'updated_at': {'type': 'date'},
        'page': {'type': 'integer'},
        'passage_index': {'type': 'integer'},
        'heading': {'type': 'text'},
        'text': {'type': 'text', 'analyzer': 'standard'},
    },
}


def unavailable():
    return ProcessingError('SEARCH_UNAVAILABLE', 'Full-text search is unavailable. Rebuild the passage index after recovering OpenSearch.', 503)


def segments(markdown: str) -> Iterable[tuple[str, str]]:
    """Bounded, non-overlapping passages; retain page-local Markdown headings.

    Do not index picture binary/URLs or arbitrary HTML. Any text is still an
    untrusted PDF string: the UI must render returned snippets as text only.
    """
    markdown = MARKDOWN_IMAGE.sub(' ', markdown)
    heading = ''
    words: list[str] = []
    for line in markdown.splitlines():
        match = HEADING.match(line.strip())
        if match:
            if words:
                yield heading, ' '.join(words)
                words = []
            heading = match[1][:300]
            continue
        # Preserve content but not Markdown table alignment-only rows.
        if re.fullmatch(r'[\s|:\-]+', line):
            continue
        for word in line.split():
            words.append(word)
            if len(words) >= PASSAGE_WORDS:
                yield heading, ' '.join(words)
                words = []
    if words:
        yield heading, ' '.join(words)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=2, max_length=200)
    match: Literal['all', 'any', 'phrase'] = 'all'
    scope: Literal['preferred', 'all'] = 'preferred'
    parser: ParserChoice | None = None
    sort: Literal['relevance', 'newest'] = 'relevance'
    offset: int = Field(0, ge=0, le=2000, strict=True)
    limit: int = Field(10, ge=1, le=20, strict=True)

    @field_validator('query')
    @classmethod
    def valid_query(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Search text must contain at least two visible characters and no controls')
        return value


class PassageView(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: UUID
    parser: ParserChoice
    page: int = Field(ge=1)
    passage_index: int = Field(ge=0)
    snippet: str = Field(max_length=5000)
    score: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class DocumentHit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document_id: DocumentID
    filename: str
    score: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    matching_passages: int = Field(ge=1)
    passages: list[PassageView]


class SearchResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[DocumentHit]
    has_more: bool
    offset: int
    # Exact number of unique matching PDFs is not available from collapse.
    total_documents: None = None


class PassageResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[PassageView]
    total: int = Field(ge=0)
    has_more: bool


def query_clause(options: SearchRequest, document_id: str | None = None):
    clauses = []
    if options.scope == 'preferred':
        clauses.append({'term': {'primary': True}})
    if options.parser:
        clauses.append({'term': {'parser': options.parser.value}})
    if document_id:
        clauses.append({'term': {'document_id': document_id}})
    if options.match == 'phrase':
        query = {'match_phrase': {'text': {'query': options.query}}}
    else:
        mode = 'and' if options.match == 'all' else 'or'
        query = {'bool': {'should': [
            {'match_phrase': {'text': {'query': options.query, 'boost': 6}}},
            {'match': {'text': {'query': options.query, 'operator': mode}}},
        ], 'minimum_should_match': 1}}
    return {'bool': {'filter': clauses, 'must': [query]}}


def highlight():
    return {'fields': {'text': {'fragment_size': 210, 'number_of_fragments': 1}},
            'pre_tags': ['<mark>'], 'post_tags': ['</mark>'], 'require_field_match': True}


def check_response(data):
    if data.get('timed_out') is not False or data['_shards']['failed'] != 0:
        raise ValueError('Incomplete OpenSearch search')
    if not isinstance(data['hits']['hits'], list):
        raise ValueError('Malformed OpenSearch hits')


def read_passage(hit):
    source = hit['_source']
    raw = (hit.get('highlight') or {}).get('text') or []
    snippet = raw[0] if raw and isinstance(raw[0], str) else source['text'][:230]
    if not isinstance(snippet, str):
        raise ValueError('Invalid highlighted snippet')
    return PassageView(run_id=source['run_id'], parser=source['parser'], page=source['page'],
        passage_index=source['passage_index'], snippet=snippet[:5000], score=hit.get('_score'))


class PassageIndex:
    def __init__(self, client: OpenSearchClient):
        self.client = client

    async def ensure(self):
        try:
            try:
                data = await self.client.request('GET', f'/{INDEX}/_mapping')
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                try:
                    await self.client.request('PUT', f'/{INDEX}', timeout=30, json={
                        'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
                        'mappings': MAPPING, 'aliases': {ALIAS: {}}})
                except httpx.HTTPStatusError as race:
                    if race.response.status_code != 400 or 'resource_already_exists_exception' not in race.response.text:
                        raise
                data = await self.client.request('GET', f'/{INDEX}/_mapping')
            mapping = data[INDEX]['mappings']
            if any(mapping.get(k) != v for k, v in MAPPING.items()):
                raise ValueError('Incompatible passage mapping')
            aliases = await self.client.request('GET', f'/_alias/{ALIAS}')
            if set(aliases) != {INDEX} or aliases[INDEX]['aliases'].get(ALIAS) != {}:
                raise ValueError('Incompatible passage alias')
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise unavailable() from exc

    async def remove_document(self, document_id):
        if not ID.fullmatch(document_id):
            raise ValueError('Invalid document ID')
        await self.ensure()
        try:
            response = await self.client.request('POST', f'/{ALIAS}/_delete_by_query', timeout=60,
                params={'refresh': 'true', 'conflicts': 'proceed'},
                json={'query': {'term': {'document_id': document_id}}})
            if response.get('timed_out') or response.get('failures') or response.get('version_conflicts'):
                raise ValueError('Incomplete search purge')
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise unavailable() from exc

    async def _bulk(self, docs: list[tuple[str, dict]]):
        for offset in range(0, len(docs), 100):
            chunk = docs[offset:offset+100]
            body = ''.join(json.dumps({'index': {'_index': INDEX, '_id': key}}, separators=(',', ':'))+'\n'
                +json.dumps(value, ensure_ascii=False, separators=(',', ':'))+'\n' for key, value in chunk)
            try:
                response = await self.client.request('POST', '/_bulk', timeout=60,
                    params={'refresh': 'wait_for'}, headers={'Content-Type': 'application/x-ndjson'}, content=body.encode())
                if response.get('errors') is not False or len(response['items']) != len(chunk):
                    raise ValueError('Incomplete passage bulk indexing')
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise unavailable() from exc

    async def rebuild_document(self, pipeline, document_id):
        """Synchronize every archived extraction under this canonical PDF.

        Repeated invocation is safe. On failure the archive is never altered;
        rerun the offline CLI rebuild to recover the derived index.
        """
        doc = await pipeline.documents.get(document_id)
        runs = []
        async for key in pipeline.archive.iter_manifest_keys(document_id):
            parts = key.split('/')
            if len(parts) == 4 and parts[0] == document_id:
                run = await pipeline.runs.get(document_id, UUID(parts[2]))
                if run.has_extraction and run.extraction_ref and run.status in (JobState.completed, JobState.partial):
                    runs.append(run)
        primary = doc.preferred_run_id
        if primary not in {r.run_id for r in runs}:
            successful = [r for r in runs if r.status == JobState.completed]
            candidates = successful or runs
            primary = max(candidates, key=lambda r: (r.updated_at, str(r.run_id))).run_id if candidates else None
        entries: list[tuple[str, dict]] = []
        for run in runs:
            parsed = await pipeline.read_extraction(run)
            for page in parsed.pages:
                for index, (heading, passage) in enumerate(segments(page.markdown)):
                    if len(entries) >= MAX_PASSAGES_PER_DOCUMENT:
                        raise ProcessingError('SEARCH_INDEX_LIMIT', 'Document exceeds the passage-index safety limit.', 422)
                    entries.append((f'{run.run_id}-{page.page:05d}-{index:05d}', {
                        'document_id': document_id, 'run_id': str(run.run_id),
                        'extraction_ref': run.extraction_ref, 'parser': run.parser.value,
                        'primary': run.run_id == primary, 'filename': doc.filename,
                        'updated_at': run.updated_at.isoformat(), 'page': page.page,
                        'passage_index': index, 'heading': heading, 'text': (heading + '\n' + passage if heading else passage),
                    }))
        await self.remove_document(document_id)
        await self._bulk(entries)
        return {'document_id': document_id, 'runs': len(runs), 'passages': len(entries), 'primary_run_id': str(primary) if primary else None}

    async def rebuild_all(self, pipeline):
        # Caller MUST hold the exclusive archive gate and have an idle queue.
        # Dropping only the derived passage index removes deleted/orphan hits.
        try:
            try:
                await self.client.request('DELETE', f'/{INDEX}', timeout=30)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        except httpx.HTTPError as exc:
            raise unavailable() from exc
        await self.ensure()
        stats = {'documents': 0, 'runs': 0, 'passages': 0}
        known = set()
        async for key in pipeline.archive.iter_manifest_keys():
            parts = key.split('/')
            if len(parts) == 2:
                document_id = parts[0]
                known.add(document_id)
                counts = await self.rebuild_document(pipeline, document_id)
                stats['documents'] += 1
                stats['runs'] += counts['runs']
                stats['passages'] += counts['passages']
        # RustFS remains authoritative; old/orphan hits were removed by reset.
        return stats

    async def search(self, options: SearchRequest):
        await self.ensure()
        body = {'size': options.limit+1, 'from': options.offset, 'track_total_hits': False,
            'query': query_clause(options), 'collapse': {'field': 'document_id', 'inner_hits': {
                'name': 'passages', 'size': 3, 'sort': [{'_score': 'desc'}], 'highlight': highlight()}},
            'sort': [{'_score': 'desc'}, {'updated_at': 'desc'}] if options.sort == 'relevance' else
                    [{'updated_at': 'desc'}, {'_score': 'desc'}],
            'highlight': highlight()}
        try:
            data = await self.client.request('POST', f'/{ALIAS}/_search', timeout=30,
                params={'allow_partial_search_results': 'false'}, json=body)
            check_response(data)
            results = []
            for hit in data['hits']['hits'][:options.limit]:
                details = hit['inner_hits']['passages']['hits']
                count = details['total']['value'] if isinstance(details['total'], dict) else details['total']
                if not isinstance(count, int) or count < 1:
                    raise ValueError('Invalid match count')
                source = hit['_source']
                results.append(DocumentHit(document_id=source['document_id'], filename=source['filename'],
                    score=hit.get('_score'), matching_passages=count,
                    passages=[read_passage(p) for p in details['hits']]))
            return SearchResult(items=results, has_more=len(data['hits']['hits'])>options.limit, offset=options.offset)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise unavailable() from exc

    async def passages(self, document_id, options: SearchRequest):
        await self.ensure()
        body = {'size': options.limit, 'from': options.offset, 'track_total_hits': True,
            'query': query_clause(options, document_id),
            'sort': [{'_score': 'desc'}, {'page': 'asc'}, {'passage_index': 'asc'}],
            'highlight': highlight()}
        try:
            data = await self.client.request('POST', f'/{ALIAS}/_search', timeout=30,
                params={'allow_partial_search_results': 'false'}, json=body)
            check_response(data)
            total = data['hits']['total']
            if not isinstance(total, dict) or total['relation'] != 'eq' or type(total['value']) is not int:
                raise ValueError('Expected exact passage count')
            return PassageResult(items=[read_passage(h) for h in data['hits']['hits']], total=total['value'],
                has_more=options.offset+len(data['hits']['hits']) < total['value'])
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise unavailable() from exc
