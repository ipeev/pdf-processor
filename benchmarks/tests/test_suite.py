"""One-command multi-route benchmark regression tests (no paid calls)."""
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import suite


def _sample_models():
    return [
        {'alias': 'vision', 'id': 'example/vision', 'routes': ['image', 'pdf_native', 'summary']},
        {'alias': 'text', 'id': 'example/text', 'routes': ['summary', 'pdf_mistral']},
        {'alias': 'missing', 'id': 'example/missing', 'routes': ['image']},
    ]


def test_catalog_skips_missing_and_incompatible_without_aborting():
    catalog = {
        'example/vision': {'architecture': {'input_modalities': ['image', 'text']}},
        'example/text': {'architecture': {'input_modalities': ['text']}},
    }
    image, skip = suite.select_for_route(_sample_models(), catalog, 'image')
    assert [x['alias'] for x in image] == ['vision']
    assert [x['alias'] for x in skip] == ['missing']
    native, skip_native = suite.select_for_route(_sample_models(), catalog, 'pdf_native')
    assert native == []
    assert skip_native[0]['alias'] == 'vision' and 'file' in skip_native[0]['reason']
    summary, _ = suite.select_for_route(_sample_models(), catalog, 'summary')
    assert [x['alias'] for x in summary] == ['vision', 'text']
    mistral, _ = suite.select_for_route(_sample_models(), catalog, 'pdf_mistral')
    assert [x['alias'] for x in mistral] == ['text']


def test_full_and_screening_preserve_same_route_variants():
    assert suite.profile_scope('image', 'full') == ('', 'scan,hard')
    assert suite.profile_scope('summary', 'full') == ('', 'clean')
    assert suite.profile_scope('pypdf', 'full') == ('', 'clean,scan,hard')
    assert suite.profile_scope('image', 'screening') == ('invoice,serial,bilingual,receipt', 'scan,hard')
    assert suite.profile_scope('summary', 'screening') == ('letter,quarterly,project,invoice', 'clean')


def test_unsafe_costs_are_not_treated_as_free():
    assert not suite._unsafe_unknown_cost([{'cost_usd': .005, 'status':'ok'}])
    assert not suite._unsafe_unknown_cost([{'cost_usd':None,'status':'unavailable'}])
    assert not suite._unsafe_unknown_cost([{'cost_usd':None,'status':'error','http_status':404}])
    assert suite._unsafe_unknown_cost([{'cost_usd':None,'status':'error','http_status':429}])
    assert suite._unsafe_unknown_cost([{'cost_usd':None,'status':'error'}])
    assert suite._recorded_cost([{'cost_usd':.05}, {'cost_usd':None}, {'cost_usd':.02}]) == pytest.approx(.07)


def test_no_winner_when_fields_or_success_rate_below_threshold():
    base = {'successes':'4','success_rate':'1.0','critical_fields':'.99',
            'missing_cost_records':'0','avg_cost_usd':'0.01','mock':'False'}
    assert suite._qualified_ocr(base)
    assert not suite._qualified_ocr({**base, 'critical_fields':'.90'})
    assert not suite._qualified_ocr({**base, 'missing_cost_records':'1'})
    assert not suite._qualified_ocr({**base, 'mock':'True'})
    assert not suite._qualified_ocr({**base, 'successes':'3'})


def test_report_outputs_route_links_and_skipped_models(tmp_path):
    out = tmp_path / 'suite'
    route = out / 'image'
    route.mkdir(parents=True)
    fields = ['route','alias','successes','units','success_rate','critical_fields',
              'missing_cost_records','avg_cost_usd','avg_latency_s','cer',
              'summary_anchor_recall','mock']
    with (route / 'comparison.csv').open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerow(dict(zip(fields,['image','vision','4','4','1.0','.99','0','0.001','1.2','.02','','False'])))
    (route / 'responses.jsonl').write_text(json.dumps({'route':'image','status':'ok','cost_usd':.01})+'\n')
    suite._report(out,[{'route':'image','planned':4,'observed':1,'evaluated':True}],
                  [{'route':'image','alias':'missing','reason':'not listed'}],mode='paid',profile='full',
                  budget=1.0,stopped_reason='partial')
    assert (out/'selection.html').exists()
    assert (out/'comparison.csv').exists()
    assert 'image/report.html' in (out/'selection.html').read_text()
    assert 'missing' in (out/'selection.html').read_text()
    meta = json.loads((out/'suite.json').read_text())
    assert meta['stop_reason'] is None and meta['stage_warnings'] == ['partial']
    assert meta['reported_spend_usd'] == .01


def test_paid_mode_requires_only_key_before_creating_fixtures(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    args=SimpleNamespace(execute=True, mock=False, max_budget_usd=None,
                         max_requests=None, repeats=1)
    with pytest.raises(ValueError,match='OPENROUTER_API_KEY'):
        suite.execute(args)
    assert list(tmp_path.iterdir()) == []


def test_suite_continues_after_error_and_unknown_cost(tmp_path, monkeypatch):
    """A failed route and unknown-cost response must not prevent remaining routes."""
    from benchmarks import generate
    fixture=tmp_path/'fixtures'
    generate.generate(fixture,seed=20261112)
    # Restrict to two synthetic stages, not genuine OpenRouter calls.
    monkeypatch.setattr(suite, 'ROUTES', ('image', 'summary'))
    monkeypatch.setattr(suite, 'load_catalog', lambda key: {
        'example/vision': {'architecture': {'input_modalities':['image','text']}},
    })
    monkeypatch.setattr(suite, 'load_models', lambda path:[{
        'alias':'vision','id':'example/vision','routes':['image','summary']}])
    import subprocess
    def fake_run(cmd, check=False):
        out=Path(cmd[cmd.index('--output-dir')+1])
        out.mkdir(parents=True)
        route=cmd[cmd.index('--route')+1]
        records=([
            {'route':'image','status':'error','cost_usd':None,'error':'finish_reason=length','alias':'vision'}
        ] if route=='image' else [
            {'route':'summary','status':'ok','cost_usd':.002,'alias':'vision'}
        ])
        (out/'responses.jsonl').write_text('\n'.join(json.dumps(x) for x in records)+'\n')
        return SimpleNamespace(returncode=1 if route=='image' else 0)
    monkeypatch.setattr(suite.subprocess,'run',fake_run)
    # Evaluation isn't the subject of this test; use a lightweight route report.
    def fake_evaluate(cmd,check=False):
        return SimpleNamespace(returncode=0)
    # fake_run also handles evaluate, returning zero status.
    real_fake_run=fake_run
    def dispatch(cmd,check=False):
        return (SimpleNamespace(returncode=0) if 'benchmarks.evaluate' in cmd
                else real_fake_run(cmd,check=check))
    monkeypatch.setattr(suite.subprocess,'run',dispatch)
    monkeypatch.setenv('OPENROUTER_API_KEY','fake-test-token')
    args=SimpleNamespace(execute=True,mock=False, max_budget_usd=None,max_requests=None,
        repeats=1,output_dir=tmp_path/'suite',fixtures=fixture,seed=20261112,
        models_file=tmp_path/'models.json',profile='screening',max_output_tokens=512)
    result=suite.execute(args)
    assert (result/'summary'/'responses.jsonl').is_file()
    info=json.loads((result/'suite.json').read_text())
    assert info['reported_spend_usd']==.002
    assert info['failed_requests']==1
    assert len(info['sections'])==2
