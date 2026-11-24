"""Real OpenSearch full-text smoke, no PDFs, Redis, RustFS or paid AI required."""
import uuid
import pytest
from app.fulltext_search import PassageIndex, SearchRequest
from app.opensearch_client import OpenSearchClient


@pytest.mark.opensearch_integration
async def test_real_opensearch_collapse_phrase_and_run_filter(settings):
    client = OpenSearchClient(settings.opensearch_url)
    index = PassageIndex(client)
    doc_id = uuid.uuid4().hex + uuid.uuid4().hex
    run_one = str(uuid.uuid4())
    run_two = str(uuid.uuid4())
    tag = 'pdfsearchtest' + uuid.uuid4().hex[:12]
    try:
        await index.ensure()
        # Exercise the real analyzer/highlighter and grouping, not only mocked DSL.
        await index._bulk([
            (f'{run_one}-00001-00001', {'document_id':doc_id,'run_id':run_one,'extraction_ref':'f'*32,
                'parser':'pypdf','primary':True,'filename':'test.pdf','updated_at':'2026-11-23T00:00:00Z',
                'page':1,'passage_index':0,'heading':'First','text':f'Important {tag} value in section one.'}),
            (f'{run_one}-00002-00001', {'document_id':doc_id,'run_id':run_one,'extraction_ref':'f'*32,
                'parser':'pypdf','primary':True,'filename':'test.pdf','updated_at':'2026-11-23T00:00:00Z',
                'page':2,'passage_index':0,'heading':'Second','text':f'Another {tag} value in section two.'}),
            (f'{run_two}-00002-00001', {'document_id':doc_id,'run_id':run_two,'extraction_ref':'f'*32,
                'parser':'gemini','primary':False,'filename':'test.pdf','updated_at':'2026-11-23T00:00:00Z',
                'page':2,'passage_index':0,'heading':'Second','text':f'Other parser {tag} value.'}),
        ])
        preferred = await index.search(SearchRequest(query=tag))
        assert len(preferred.items) == 1
        assert preferred.items[0].document_id == doc_id
        assert preferred.items[0].matching_passages == 2
        assert len(preferred.items[0].passages) == 2
        assert all(p.parser.value == 'pypdf' for p in preferred.items[0].passages)
        all_runs = await index.search(SearchRequest(query=tag, scope='all'))
        assert len(all_runs.items) == 1
        assert all_runs.items[0].matching_passages == 3
        details = await index.passages(doc_id, SearchRequest(query=tag, scope='all'))
        assert details.total == 3
        assert {p.page for p in details.items} == {1,2}
    finally:
        try:
            await index.remove_document(doc_id)
        finally:
            await client.aclose()
