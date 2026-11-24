"""Offline v0.12.4 full-text tests: no Redis/RustFS/OpenRouter required."""
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID
import json
import pytest
from pydantic import ValidationError

from app.fulltext_search import (
    ALIAS, INDEX, PassageIndex, SearchRequest, query_clause, segments,
    read_passage, check_response, unavailable,
)
from app.schemas import JobState, ParserChoice

DOC = 'a' * 64
RUN1 = UUID('11111111-1111-4111-8111-111111111111')
RUN2 = UUID('22222222-2222-4222-8222-222222222222')


def test_passage_boundaries_and_image_sanitization():
    markdown = '# Architecture\n\n' + ' '.join('word' for _ in range(500)) + '\n![figure](images/p0001-figure-a.png)'
    parts = list(segments(markdown))
    assert [len(content.split()) for _, content in parts] == [220, 220, 60]
    assert all(h == 'Architecture' and 'images/' not in t for h, t in parts)
    assert list(segments('# One\nFirst line\n# Two\nSecond line')) == [('One', 'First line'), ('Two', 'Second line')]
    assert list(segments('   ')) == []


def test_request_constraints_and_no_user_defined_dsl():
    assert SearchRequest(query='  neural network  ').query == 'neural network'
    for options in ({'query':'X'}, {'query':'A\nB'}, {'query':'a' * 201},
                    {'query':'hello','offset':-1}, {'query':'hello','sort':'script'},
                    {'query':'hello','unknown':True}, {'query':'hello','limit':100}):
        with pytest.raises(ValidationError):
            SearchRequest.model_validate(options)
    options = SearchRequest(query='quality evaluation', match='all', scope='preferred',parser=ParserChoice.pypdf)
    clause = query_clause(options)
    assert {'term': {'primary':True}} in clause['bool']['filter']
    assert {'term': {'parser':'pypdf'}} in clause['bool']['filter']
    assert clause['bool']['must'][0]['bool']['should'][1]['match']['text']['operator'] == 'and'
    assert 'query_string' not in str(clause)
    assert query_clause(SearchRequest(query='neural network', match='phrase',scope='all'), DOC)['bool']['must'][0] == {'match_phrase': {'text': {'query': 'neural network'}}}


def test_safe_highlights_are_data_not_html():
    view = read_passage({'_source': {'run_id':str(RUN1),'parser':'pypdf','page':3,'passage_index':2,'text':'plain'},
        '_score':4.0,'highlight': {'text':['<mark>plain</mark> <script>alert(1)</script>']}})
    assert view.snippet.endswith('<script>alert(1)</script>')  # Frontend must render as plain text.
    assert view.page == 3


class FakeClient:
    def __init__(self):
        self.calls = []
        self.bulk = []
        self.documents = []
    async def request(self, method, path, **kwargs):
        self.calls.append((method,path,kwargs))
        if path == f'/{INDEX}/_mapping':return {INDEX: {'mappings': {
            'dynamic':'strict','_meta': {'schema_version':1,'purpose':'rebuildable-pdf-passages'},
            'properties': __import__('app.fulltext_search',fromlist=['MAPPING']).MAPPING['properties']}}}
        if path == f'/_alias/{ALIAS}':return {INDEX: {'aliases': {ALIAS: {}}}}
        if path.endswith('/_delete_by_query'):return {'deleted':10,'failures':[],'timed_out':False,'version_conflicts':0}
        if path == '/_bulk':
            data=kwargs['content'].decode().splitlines()
            self.bulk.extend([json.loads(x) for x in data]);return {'errors': False, 'items':[{}] * (len(data)//2)}
        if path.endswith('/_search'):
            assert kwargs['json']['collapse']['field'] == 'document_id'
            matching = {'_source': {'run_id':str(RUN1),'parser':'pypdf','page':3,'passage_index':0,'text':'hello'},'_score':9.3}
            return {'timed_out':False,'_shards':{'failed':0}, 'hits':{'hits':[
                {'_source':{'document_id':DOC,'filename':'paper.pdf'},'_score':9.3,
                 'inner_hits':{'passages':{'hits':{'total':{'relation':'eq','value':7},'hits':[matching]}}}},
            ]}}
        if method=='DELETE' and path == f'/{INDEX}':return {'acknowledged':True}
        raise AssertionError((method,path,kwargs))


@pytest.mark.asyncio
async def test_search_groups_runs_and_surfaces_passages():
    result = await PassageIndex(FakeClient()).search(SearchRequest(query='hello'))
    assert len(result.items) == 1
    assert result.items[0].matching_passages == 7
    assert result.items[0].passages[0].page == 3
    assert result.total_documents is None  # collapse cannot report unique total


@pytest.mark.asyncio
async def test_rebuild_document_primary_preference_and_no_provider_calls():
    client = FakeClient()
    search = PassageIndex(client)
    def run(uid,status):
        return SimpleNamespace(run_id=uid,status=status,has_extraction=True,extraction_ref='f'*32,
            parser=ParserChoice.pypdf,updated_at=datetime(2026,11,23,tzinfo=UTC))
    class Archive:
        async def iter_manifest_keys(self, doc_id):
            yield f'{doc_id}/runs/{RUN1}/metadata.json'
            yield f'{doc_id}/runs/{RUN2}/metadata.json'
    class Runs:
        async def get(self, doc_id, uid):return run(uid,JobState.completed if uid==RUN1 else JobState.partial)
    class Docs:
        async def get(self, doc_id):return SimpleNamespace(filename='paper.pdf',preferred_run_id=RUN1)
    class Pipeline:
        archive=Archive();runs=Runs();documents=Docs()
        async def read_extraction(self, item):
            return SimpleNamespace(pages=[SimpleNamespace(page=1,markdown='# Topic\nHello PDF extraction!')])
    counts = await search.rebuild_document(Pipeline(), DOC)
    assert counts['runs'] == 2 and counts['passages'] == 2
    sources = [client.bulk[n] for n in range(1,len(client.bulk),2)]
    assert sources[0]['primary'] is True and sources[1]['primary'] is False
    assert sources[0]['text'].startswith('Topic\nHello')
    assert {s['document_id'] for s in sources} == {DOC}
    assert client.calls[-1][1] == '/_bulk'
