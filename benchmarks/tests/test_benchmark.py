"""Offline safety + generation + scoring checks. No OpenRouter API key is needed."""
import csv
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from benchmarks import evaluate, generate, run

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures'


def test_all_cases_have_unique_ids_and_ground_truth():
    data=generate.cases(20261112)
    assert len(data)==12 and len({x['id'] for x in data})==12
    for d in data:
        assert d['pages']
        assert all(generate.expected_lines(p,i+1,len(d['pages'])) for i,p in enumerate(d['pages']))
    assert sum(len(d['pages']) for d in data)==15


def test_manifest_contains_36_real_pdfs_and_matching_hashes():
    manifest=json.loads((FIXTURE/'manifest.json').read_text())
    assert manifest['seed']==20261112
    assert len(manifest['documents'])==36
    for doc in manifest['documents']:
        pdf=FIXTURE/doc['pdf']
        truth=json.loads((FIXTURE/doc['truth']).read_text())
        assert pdf.is_file() and pdf.stat().st_size>500
        assert truth['sha256']==doc['sha256']
        assert len(PdfReader(pdf).pages)==doc['pages']


def test_scans_have_no_text_layer_and_clean_pdfs_do():
    for case in ['invoice','bilingual','serial','inventory','injection']:
        clean=PdfReader(FIXTURE/'pdf'/f'{case}__clean.pdf')
        scanned=PdfReader(FIXTURE/'pdf'/f'{case}__scan.pdf')
        hard=PdfReader(FIXTURE/'pdf'/f'{case}__hard.pdf')
        assert len(''.join(p.extract_text() or '' for p in clean.pages))>20
        assert not ''.join(p.extract_text() or '' for p in scanned.pages).strip()
        assert not ''.join(p.extract_text() or '' for p in hard.pages).strip()


def test_generation_is_deterministic(tmp_path):
    a=generate.generate(tmp_path/'one',seed=800,variants=('clean',))
    b=generate.generate(tmp_path/'two',seed=800,variants=('clean',))
    assert [d['sha256'] for d in a['documents']]==[d['sha256'] for d in b['documents']]
    assert a['documents'][1]['sha256']!=generate.generate(tmp_path/'three',seed=801,variants=('clean',))['documents'][1]['sha256']


def _reference_distance(seq1,seq2):
    prev=list(range(len(seq2)+1))
    for i,c in enumerate(seq1,1):
        cur=[i]
        for j,d in enumerate(seq2,1):cur.append(min(cur[-1]+1,prev[j]+1,prev[j-1]+(c!=d)))
        prev=cur
    return prev[-1]


def test_bitparallel_levenshtein_exact_on_random_strings_and_tokens():
    rand=random.Random(119)
    alphabet=['a','b','0','O','О','1']
    for _ in range(300):
        a=[rand.choice(alphabet) for _ in range(rand.randint(0,25))]
        b=[rand.choice(alphabet) for _ in range(rand.randint(0,25))]
        assert evaluate.distance(a,b)==_reference_distance(a,b)
        assert evaluate.distance(''.join(a),''.join(b))==_reference_distance(a,b)
    assert evaluate.distance('x'*1000+'a','x'*1000+'b')==1


def test_numeric_and_serial_critical_fields_exact():
    assert evaluate.has_exact('The total is $809.64','809.64')
    assert not evaluate.has_exact('The total is $809.84','809.64')
    assert not evaluate.has_exact('123.40','23.40')
    assert not evaluate.has_exact('SN-O0I1l5B0','SN-O0I1l5B8')
    assert evaluate.has_exact('Пловдив Лаборатория','Пловдив Лаборатория')


def test_order_and_table_diagnostics():
    assert evaluate._table_row_present('|Precision Bearing|12|18.75|225.00|',['Precision Bearing','12','18.75','225.00'])
    assert not evaluate._table_row_present('|Precision Bearing|12|18.75|224.00|',['Precision Bearing','12','18.75','225.00'])
    assert evaluate._reading_order('First Section\nSecond Section\nThird Section','First Section\nSecond Section\nThird Section')==1
    assert evaluate._reading_order('First Section\nSecond Section','Second Section\nFirst Section')==0


def test_score_exact_source_and_injection_only():
    truth=json.loads((FIXTURE/'ground_truth/injection__clean.json').read_text())
    row={'case':'injection','variant':'clean','route':'image','model':'local/test',
         'alias':'test','rep':1,'page':1,'status':'ok','response':truth['pages'][0],
         'cost_usd':0,'elapsed_s':0,'mock':True}
    score=evaluate.score_row(row,truth)
    assert score['cer']==0 and score['field_accuracy']==1
    assert score['injection_only_answer'] is False
    row['response']='MODEL_HIJACKED'
    assert evaluate.score_row(row,truth)['injection_only_answer'] is True


def test_route_plan_and_model_validation():
    manifest=json.loads((FIXTURE/'manifest.json').read_text())
    models=run._chosen(run.load_models(ROOT/'models.json'),'ling_vl,gemini_current','image')
    jobs=run.plan(manifest,models,ids=['inventory'],variants=['hard'],route='image',repeats=2)
    assert len(jobs)==12
    assert len({run.signature(j) for j in jobs})==12
    with pytest.raises(ValueError,match='Unknown model'):
        run._chosen(run.load_models(ROOT/'models.json'),'fake_model','image')
    with pytest.raises(ValueError,match='Unknown cases'):
        run.plan(manifest,models,ids=['missing'],variants=['scan'],route='image',repeats=1)


def test_single_page_pdf_and_png_transports():
    pdf=FIXTURE/'pdf/inventory__hard.pdf'
    by=run._single_page_pdf(pdf,1)
    import io
    assert len(PdfReader(io.BytesIO(by)).pages)==1
    assert run._image_bytes(pdf,1)[:8]==b'\x89PNG\r\n\x1a\n'


def _args(tmp_path,**override):
    data=dict(fixtures=FIXTURE,models_file=ROOT/'models.json',models='ling_vl,gemini_current',
              cases='invoice,serial',variants='scan',route='image',repeats=1,
              max_output_tokens=256,max_requests=None,max_budget_usd=None,
              output_dir=tmp_path/'output',resume=False,execute=False,mock=True,dry_run=False,on_http_404='skip-model')
    data.update(override)
    return SimpleNamespace(**data)


def test_mock_run_evaluate_and_resuming_does_not_duplicate(tmp_path):
    args=_args(tmp_path)
    out=run.execute(args)
    rows=(out/'responses.jsonl').read_text().splitlines()
    assert len(rows)==4
    stats=evaluate.evaluate(out,FIXTURE)
    assert len(stats)==2 and all(s['cer']==0 for s in stats)
    assert 'OFFLINE MOCK' in (out/'report.html').read_text()
    assert 'mock' in (out/'comparison.csv').read_text()
    args.resume=True
    run.execute(args)
    assert (out/'responses.jsonl').read_text().splitlines()==rows


def test_resume_rejects_incompatible_parameters(tmp_path):
    a=_args(tmp_path)
    run.execute(a)
    with pytest.raises(ValueError,match='Resume parameters'):
        run.execute(_args(tmp_path,resume=True,models='ling_vl'))


def test_paid_mode_requires_only_key(tmp_path, monkeypatch):
    args=_args(tmp_path,mock=False,execute=True)
    monkeypatch.delenv('OPENROUTER_API_KEY',raising=False)
    with pytest.raises(ValueError,match='OPENROUTER_API_KEY'):
        run.execute(args)
    assert not args.output_dir.exists()


def test_spending_never_stops_queued_calls(tmp_path,monkeypatch):
    class Response:
        status_code=200
        def __init__(self,data): self.data=data
        def raise_for_status(self): pass
        def json(self): return self.data
    class Client:
        def __init__(self,**kwargs): self.posts=0
        def get(self,*args,**kwargs):
            return Response({'data':[{'id':'inclusionai/ling-3.0-flash-vl',
                'architecture':{'input_modalities':['text','image']}}]})
        def post(self,*args,**kwargs):
            self.posts+=1
            return Response({'choices':[{'finish_reason':'stop','message':{'content':'transcription'}}],
                             'usage':{'cost':0.04},'model':'inclusionai/ling-3.0-flash-vl'})
        def close(self): pass
    client=Client()
    monkeypatch.setattr(run.httpx,'Client',lambda **kwargs:client)
    monkeypatch.setenv('OPENROUTER_API_KEY','fake-test-token')
    args=_args(tmp_path,mock=False,execute=True,models='ling_vl',max_budget_usd=.001)
    run.execute(args)
    assert client.posts==2
    records=[json.loads(x) for x in (args.output_dir/'responses.jsonl').read_text().splitlines()]
    assert len(records)==2 and all(row['cost_usd']==.04 for row in records)


def test_missing_cost_and_incomplete_response_continue(tmp_path,monkeypatch):
    class Client:
        def __init__(self): self.called=0
        def get(self,*args,**kwargs):
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'data':[{'id':'inclusionai/ling-3.0-flash-vl',
                'architecture':{'input_modalities':['image']}}]})
        def post(self,*args,**kwargs):
            self.called+=1
            if self.called==1:  # Simulate a truncated provider response: finish_reason=length.
                return SimpleNamespace(status_code=200,json=lambda:{'choices':[{'finish_reason':'length',
                    'message':{'content':'partial'}}], 'usage':{'cost':0.003}})
            return SimpleNamespace(status_code=200,json=lambda:{'choices':[{'finish_reason':'stop',
                'message':{'content':'hello'}}], 'usage':{}})
        def close(self): pass
    client=Client()
    monkeypatch.setattr(run.httpx,'Client',lambda **kwargs:client)
    monkeypatch.setenv('OPENROUTER_API_KEY','fake-test-token')
    a=_args(tmp_path,mock=False,execute=True,models='ling_vl')
    run.execute(a)
    assert client.called==2
    rows=[json.loads(x) for x in (a.output_dir/'responses.jsonl').read_text().splitlines()]
    assert len(rows)==2
    assert rows[0]['status']=='error' and rows[0]['cost_usd']==.003
    assert rows[0]['response']=='partial' and rows[0]['finish_reason']=='length'
    assert rows[1]['status']=='ok' and rows[1]['cost_usd'] is None


def test_html_escapes_untrusted_model_text():
    body=evaluate._render_html([], [{'case':'<script>alert(1)</script>','variant':'scan',
        'route':'image','alias':'<script>','rep':1,'page':1,'status':'error','error':'<img>'}], mock=False)
    assert '<script>' not in body and '&lt;script&gt;' in body


def test_http_rejection_includes_safe_message_and_redacts_secret():
    class Rejected:
        status_code=404
        def json(self):
            return {'error':{'code':404,'message':'No endpoints found that support base64 image input. sk-or-v1-SECRET  '+('a'*220)}}
    class Client:
        def post(self,*args,**kwargs):return Rejected()
    with pytest.raises(run.OpenRouterHTTPRejection) as exc:
        run._complete(Client(),'sk-or-v1-SECRET',{'model':'qwen/qwen3.8-flash','messages':[]},max_tokens=128)
    assert exc.value.status_code==404
    assert 'base64 image input' in str(exc.value)
    assert 'SECRET' not in str(exc.value)
    assert 'a'*160 not in str(exc.value)


def test_rejected_404_skips_only_unavailable_model_and_continues(tmp_path,monkeypatch):
    ids=['google/gemini-3.1-flash-lite','qwen/qwen3.8-flash','inclusionai/ling-3.0-flash-vl']
    class Reply:
        def __init__(self, status=200, body=None):self.status_code=status;self.body=body or {}
        def raise_for_status(self):
            if self.status_code!=200:raise RuntimeError('unexpected GET error')
        def json(self):return self.body
    class Client:
        def __init__(self):self.calls=[]
        def get(self,*args,**kwargs):
            return Reply(body={'data':[{'id':x,'architecture':{'input_modalities':['text','image']}} for x in ids]})
        def post(self,*args,**kwargs):
            model=kwargs['json']['model']
            self.calls.append(model)
            if model==ids[1]:return Reply(404,{'error':{'code':404,'message':'No endpoint supports base64 image input'}})
            return Reply(body={'choices':[{'finish_reason':'stop','message':{'content':'scanned text'}}],
                               'usage':{'cost':0.001},'model':model})
        def close(self):pass
    client=Client()
    monkeypatch.setattr(run.httpx,'Client',lambda **kwargs:client)
    monkeypatch.setenv('OPENROUTER_API_KEY','fake-for-test')
    a=_args(tmp_path,mock=False,execute=True,models='gemini_current,qwen_flash,ling_vl',
            max_budget_usd=1,on_http_404='skip-model')
    run.execute(a)
    rows=[json.loads(x) for x in (a.output_dir/'responses.jsonl').read_text().splitlines()]
    assert len(rows)==6
    assert len(client.calls)==5
    assert [x['status'] for x in rows].count('ok')==4
    assert [x['status'] for x in rows].count('error')==1
    assert [x['status'] for x in rows].count('unavailable')==1
    assert rows[1]['http_status']==404
    assert 'base64 image' in rows[1]['error']
    assert rows[1]['cost_usd'] is None and rows[4]['cost_usd'] is None


def test_resume_skips_completed_jobs_including_404(tmp_path,monkeypatch):
    ids=['google/gemini-3.1-flash-lite','qwen/qwen3.8-flash','inclusionai/ling-3.0-flash-vl']
    class Reply:
        def __init__(self, status=200, data=None):self.status_code=status;self.data=data or {}
        def raise_for_status(self):pass
        def json(self):return self.data
    class Client:
        def __init__(self):self.calls=[]
        def get(self,*args,**kwargs):
            return Reply(data={'data':[{'id':x,'architecture':{'input_modalities':['text','image']}} for x in ids]})
        def post(self,*args,**kwargs):
            model=kwargs['json']['model'];self.calls.append(model)
            if model==ids[1]:return Reply(404,{'error':{'message':'No endpoint supports base64 images'}})
            return Reply(data={'choices':[{'finish_reason':'stop','message':{'content':'transcribed'}}],
                               'usage':{'cost':0.001},'model':model})
        def close(self):pass
    client=Client()
    monkeypatch.setattr(run.httpx,'Client',lambda **kwargs:client)
    monkeypatch.setenv('OPENROUTER_API_KEY','dummy')
    a=_args(tmp_path,mock=False,execute=True,models='gemini_current,qwen_flash,ling_vl')
    run.execute(a)
    assert len(client.calls)==5
    a.resume=True
    run.execute(a)
    assert len(client.calls)==5  # Existing errors aren't retried automatically.
    rows=[json.loads(x) for x in (a.output_dir/'responses.jsonl').read_text().splitlines()]
    assert len(rows)==6
