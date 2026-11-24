"""Fourth parser Stage 0–2: no live OpenRouter calls and no archive writes."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from pathlib import Path
import json
from uuid import uuid4

import httpx
import pytest

from app.cache import fingerprint
from app.catalogue_models import RunManifest
from app.config import Settings
from app.errors import ProcessingError
from app.openrouter_client import OpenRouterClient
from app.parsers.vision_figures import (PROMPT_VERSION, VisionFiguresParser, decode_page_response,
                                        assemble_page, padded_box, FigureCandidate)
from app.schemas import ParserChoice

ROOT = Path(__file__).parent / 'fixtures' / 'vision'


def fixture(page=1):
    return (ROOT / f'page_{page:04d}.json').read_text()


def settings(**changes):
    return Settings(_env_file=None, **changes)


def template():
    return json.loads(fixture(1))


class FakeClient:
    def __init__(self, fail_page=None):
        self.calls = []
        self.fail_page = fail_page

    async def complete_vision_figures_page(self, png_bytes, *, page, max_tokens):
        self.calls.append((page, len(png_bytes), max_tokens))
        if page == self.fail_page:
            return '{"markdown": "truncated'
        return fixture(page)


def assert_invalid(content):
    with pytest.raises(ProcessingError, match='VISION_RESPONSE_INVALID'):
        decode_page_response(json.dumps(content) if isinstance(content, dict) else content, 8)


def test_fourth_enum_and_old_run_manifest():
    assert ParserChoice.vision_figures.value == 'vision_figures'
    source = {'run_id': str(uuid4()), 'document_id': 'a'*64, 'filename': 'file.pdf',
              'parser': 'gpt5_mini', 'pipeline_version': 'pdf-pipeline-v4',
              'application_version': '0.12.0', 's3_prefix': ''}
    source['s3_prefix'] = f"{'a'*64}/runs/{source['run_id']}/"
    assert RunManifest.model_validate(source).parser == ParserChoice.gpt5_mini
    source['parser'] = 'vision_figures'
    assert RunManifest.model_validate(source).parser == ParserChoice.vision_figures


def test_old_fingerprint_exactly_same():
    from hashlib import sha256
    data = 'a'*64
    s = settings()
    # Reference digest computed from the immutable v0.12.0 payload.
    import json as stdjson
    payload = dict(pdf_sha256=data, parser='pypdf', prompt_version='pdf-pipeline-v4',
        native_pdf_model=s.native_pdf_model, image_ocr_model=s.image_ocr_model,
        summary_model=s.summary_model, max_pages=s.max_pages_per_document,
        max_ai_pages=s.max_ai_pages_per_document, max_summary_input_chars=s.max_summary_input_chars,
        max_ai_output_tokens=s.max_ai_output_tokens_per_page, max_summary_tokens=s.max_summary_output_tokens,
        render_scale=s.render_scale, max_render_pixels=s.max_render_pixels,
        max_render_image_bytes=s.max_render_image_bytes)
    old = sha256(stdjson.dumps(payload, sort_keys=True, separators=(',',':')).encode()).hexdigest()
    assert fingerprint(data, ParserChoice.pypdf, s) == old
    assert fingerprint(data, ParserChoice.gemini, s) != fingerprint(data, ParserChoice.gpt5_mini, s)
    assert fingerprint(data, ParserChoice.vision_figures, s) != fingerprint(data, ParserChoice.gpt5_mini, s)
    assert fingerprint(data, ParserChoice.vision_figures, s) != fingerprint(data, ParserChoice.vision_figures, settings(vision_figures_model='minimax/minimax-m3.1-flash'))
    assert fingerprint(data, ParserChoice.vision_figures, s) != fingerprint(data, ParserChoice.vision_figures, settings(vision_figure_padding_norm=0))


def test_figure_validation_and_unique_markers():
    md, figs = decode_page_response(fixture(), 8)
    assert len(figs) == 2
    assert '[[FIGURE:figure-1]]' in md
    assert all(not f.excluded_table for f in figs)


@pytest.mark.parametrize('mutate', [
    lambda d: d.pop('markdown'),
    lambda d: d.update(markdown=''),
    lambda d: d.update(markdown='foo'),
    lambda d: d.update(markdown='[[FIGURE:figure-1]] [[FIGURE:figure-1]]'),
    lambda d: d['figures'][0].update(bbox_norm=[100, 200, 100, 400]),
    lambda d: d['figures'][0].update(bbox_norm=[True, 200, 400, 700]),
    lambda d: d['figures'][0].update(id='../escape'),
    lambda d: d['figures'][0].update(kind='unknown'),
    lambda d: d['figures'][0].update(nearby_text_bboxes_norm=[[0, 0, 1, 1]]*5),
    lambda d: d['figures'].append(d['figures'][0]),
    lambda d: d.update(markdown=d['markdown']+'\n[[FIGURE:not-a-figure]]'),
])
def test_bad_responses_rejected(mutate):
    data=template(); mutate(data); assert_invalid(data)


def test_truncated_and_figures_only_rejected():
    assert_invalid('{"markdown": "foo", "figures": [')
    assert_invalid({'figures': []})
    assert_invalid({'markdown': '__BLANK_PAGE__', 'figures': template()['figures']})


def test_table_excluded_even_with_wrong_kind_and_marker():
    data = template()
    data['figures'] = [data['figures'][0]]
    data['markdown'] = 'Before.\n\n[[FIGURE:figure-1]]\n\nFigure 1. Bench\n\n[[FIGURE:figure-2]]\n\nTable 1. Darknet-53.'
    data['figures'].append({'id': 'figure-2','kind': 'figure','caption':'Table 1. Darknet-53.',
                            'bbox_norm': [100, 200, 700, 400], 'caption_bbox_norm': None})
    from app.parsers.pdf_render import render_one_page
    import pypdfium2 as pdfium
    pdf=pdfium.PdfDocument(str(ROOT/'sample_figures.pdf'))
    try: png=render_one_page(pdf,0,settings())
    finally: pdf.close()
    page, assets, warns = assemble_page(json.dumps(data), png, 1, settings())
    assert len(assets)==1
    assert 'Table 1. Darknet-53.' in page.markdown
    assert '[[FIGURE' not in page.markdown
    assert 'excluded a data table' in ' '.join(warns)
    assert page.markdown.count('Table 1. Darknet-53.')==1


def test_table_without_marker_also_excluded():
    data={'markdown': '## Text\n\nTable 1. This is a data table.',
          'figures': [{'id':'figure-1', 'kind':'table', 'caption':'Table 1. This is a data table.',
                        'bbox_norm':[100,200,700,400]}]}
    md, figs = decode_page_response(json.dumps(data), 8)
    assert len(figs)==1 and figs[0].excluded_table


def test_guard_blocks_padding():
    f=FigureCandidate('figure-1','chart','Figure 1. A', (100,100,500,500),(100,510,500,550),())
    box, notes=padded_box(f,20)
    assert box[3] <= 509 and box[0]==80
    overlap=replace(f, caption_bbox_norm=(150,450,300,550))
    box, warnings=padded_box(overlap,20)
    assert box==overlap.bbox_norm and warnings


@pytest.mark.asyncio
async def test_offline_two_page_parser_extracts_figures_and_complete_md():
    fake=FakeClient()
    parsed=await VisionFiguresParser(fake, settings()).parse(ROOT/'sample_figures.pdf')
    assert len(fake.calls)==2
    assert len(parsed.pages)==2 and len(parsed.assets)==3
    assert all(a.data[:8] == b'\x89PNG\r\n\x1a\n' for a in parsed.assets)
    assert len({a.path for a in parsed.assets}) == 3
    assert parsed.pages[0].markdown.count('images/p0001-figure-1.png')==1
    assert parsed.pages[0].markdown.count('images/p0001-figure-2.png')==1
    assert parsed.pages[1].markdown.count('images/p0002-figure-1.png')==1
    assert all('[[FIGURE' not in p.markdown for p in parsed.pages)
    parsed.as_parsed_document().validate()


@pytest.mark.asyncio
async def test_invalid_page2_stops_processing():
    fake=FakeClient(fail_page=2)
    with pytest.raises(ProcessingError) as err:
        await VisionFiguresParser(fake,settings()).parse(ROOT/'sample_figures.pdf')
    assert err.value.code=='VISION_RESPONSE_INVALID'
    assert [p for p, *_ in fake.calls] == [1, 2, 2, 2]
    assert err.value.diagnostic['attempt'] == 3
    assert err.value.diagnostic['page']==2


@pytest.mark.asyncio
async def test_fail_early_page_limit_makes_zero_calls():
    fake=FakeClient()
    with pytest.raises(ProcessingError, match='VISION_PAGE_LIMIT'):
        await VisionFiguresParser(fake,settings(max_vision_figures_pages_per_document=1)).parse(ROOT/'sample_figures.pdf')
    assert not fake.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [429,500,503])
async def test_no_retry_on_http_errors(status):
    attempts=[]
    def handler(req):
        attempts.append(req)
        return httpx.Response(status, json={'error':'test'})
    s=settings(openrouter_api_key='test-key', openrouter_max_call_attempts=3)
    client=OpenRouterClient(s,httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(ProcessingError):
            await client.complete_vision_figures_page(b'PNG',page=1,max_tokens=2000)
    finally: await client.http.aclose()
    assert len(attempts)==1


@pytest.mark.asyncio
async def test_no_retry_on_transport_error():
    attempts=[]
    def handler(req):
        attempts.append(req)
        raise httpx.ConnectError('No network',request=req)
    s=settings(openrouter_api_key='test-key')
    client=OpenRouterClient(s,httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(ProcessingError):
            await client.complete_vision_figures_page(b'PNG',page=1,max_tokens=2000)
    finally: await client.http.aclose()
    assert len(attempts)==1


@pytest.mark.asyncio
async def test_one_request_payload_and_success():
    requests=[]
    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':fixture(1)}}]})
    s=settings(openrouter_api_key='test-key')
    client=OpenRouterClient(s,httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try: text=await client.complete_vision_figures_page(b'PNG',page=1,max_tokens=1234)
    finally: await client.http.aclose()
    assert text==fixture(1).strip()
    assert len(requests)==1
    assert requests[0]['model']=='anthropic/claude-haiku-5.5'
    assert requests[0]['max_tokens']==1234
    assert requests[0]['messages'][1]['content'][1]['type']=='image_url'
    assert 'Table 1' in requests[0]['messages'][0]['content']


def test_fourth_parser_is_enabled_with_atomic_images():
    import app
    from pathlib import Path
    source = (Path(app.__file__).parent / 'api.py').read_text()
    assert 'VISION_ARCHIVE_REQUIRED' in source
    source = (Path(app.__file__).parent / 'document_processor.py').read_text()
    assert 'VisionFiguresParser' in source
    source = (Path(app.__file__).parent / 'persistent_pipeline.py').read_text()
    assert 'durable_vision_page' in source and 'figures.json' in source


def test_marker_mid_sentence_is_relocated_before_single_caption():
    from app.parsers.vision_figures import _place_markers_by_caption, decode_page_response
    doc = template()
    doc['figures'] = doc['figures'][:1]
    doc['markdown'] = '''The bench was [[FIGURE:figure-1]] inspected carefully.

**Figure 1.** Three-channel bench.

Narrative after the figure.'''
    _, figs = decode_page_response(json.dumps(doc), 8)
    md, warnings = _place_markers_by_caption(doc['markdown'], figs, 1)
    assert 'was  inspected' in md
    assert '[[FIGURE:figure-1]]\n\n**Figure 1.' in md
    assert not warnings


def test_missing_caption_falls_back_without_split():
    from app.parsers.vision_figures import _place_markers_by_caption, decode_page_response
    doc = template(); doc['figures'] = doc['figures'][:1]
    doc['markdown'] = 'The very first [[FIGURE:figure-1]] sentence ends here.\n\nNo captions available.'
    _, figs = decode_page_response(json.dumps(doc), 8)
    md, warnings = _place_markers_by_caption(doc['markdown'], figs, 1)
    assert 'first  sentence' in md
    assert md.index('[[FIGURE:figure-1]]') > md.index('No captions available')
    assert 'Uncertain figure placement' in md
    assert warnings


def test_repeated_caption_label_uses_safe_fallback():
    from app.parsers.vision_figures import _place_markers_by_caption, decode_page_response
    doc = template(); doc['figures'] = doc['figures'][:1]
    doc['markdown'] = '[[FIGURE:figure-1]]\nFigure 1. A\nFigure 1. B'
    _, figs = decode_page_response(json.dumps(doc), 8)
    md, warnings = _place_markers_by_caption(doc['markdown'], figs, 1)
    assert len(warnings)==1
    assert md.rfind('[[FIGURE:figure-1]]') > md.rfind('Figure 1. B')


@pytest.mark.asyncio
async def test_page2_caption_preferred_over_narrative_starting_with_figure_number():
    parsed = await VisionFiguresParser(FakeClient(), settings()).parse(ROOT/'sample_figures.pdf')
    assert not any('no unique figure caption' in warning for warning in parsed.warnings)
    md = parsed.pages[1].markdown
    assert md.index('![Figure 3.') < md.index('**Figure 3.**')
    assert md.index('Figure 3 shows a synthetic inspection heatmap') < md.index('![Figure 3.')
    assert 'Uncertain figure placement' not in md


@pytest.mark.asyncio
async def test_existing_summary_gateway_still_retries_429(monkeypatch):
    import asyncio
    hits=[]
    async def no_wait(_seconds): return None
    monkeypatch.setattr(asyncio, 'sleep', no_wait)
    def handler(req):
        hits.append(req)
        return (httpx.Response(429, json={'error':'busy'}) if len(hits)==1 else
                httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'Summary OK'}}]}))
    s=settings(openrouter_api_key='test-key', openrouter_max_call_attempts=3)
    client=OpenRouterClient(s,httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try: assert await client.complete_text('prompt',max_tokens=100)=='Summary OK'
    finally: await client.http.aclose()
    assert len(hits)==2
