"""Validation mode is opt-in outside evaluate.py; no real credentials or model calls."""
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from math_agent.config import Settings
from math_agent.execution_limits import snapshot, repair_allowed, task_timeout
from math_agent.evaltools.budgets import resolve_output_limit
from math_agent.evaltools.inputs import EvaluationError
from math_agent.provider import DeepSeek, ModelError
from math_agent.parallel_budget import ParallelBudget, BudgetCancelled


@pytest.fixture
def cfg(settings):
    return replace(settings, validation_mode=True, validation_max_tokens=393216)


def chat(content='{}', finish='stop', tools=None):
    message = {'role': 'assistant', 'content': content}
    if tools:
        message['tool_calls'] = tools
        message['reasoning_content'] = 'private reasoning retained only in tool history'
    return {'choices': [{'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}


def test_default_production_policy_and_config_ceilings(settings):
    assert not settings.validation_mode
    limits = snapshot(settings)
    assert limits['mode'] == 'production'
    assert limits['task_timeout_seconds'] == settings.job_timeout == 240
    assert limits['model_call_limit'] == settings.max_calls
    assert limits['repair_limit'] == settings.max_repairs
    assert limits['tool_round_limit'] == 4
    assert settings.max_tokens == 12000
    with pytest.raises(ValueError):
        replace(settings, max_tokens=393216)
    assert replace(settings, validation_mode=True, max_tokens=393216).max_tokens == 393216


@pytest.mark.parametrize('stage', ['source','source_review','solve','review','diagram','math_repair','adjudicate','deferred_review'])
def test_validation_overrides_all_stage_output_caps_without_lowering_effort(cfg, stage):
    cfg = replace(cfg, source_max_tokens=8000, max_tokens=12000,
                  solve_max_tokens=6000, review_max_tokens=4000, review_reserve_seconds=80)
    seen = []
    def handler(req):
        seen.append((json.loads(req.content), req.extensions['timeout']))
        return httpx.Response(200, json=chat())
    client = DeepSeek(cfg, lambda *a: None, httpx.MockTransport(handler))
    client.started -= 1000  # Beyond production job and reserve budgets.
    client.calls = 100      # Beyond production model-call budget.
    try:
        assert client.generate(stage, []) == {}
        payload, timeout = seen[0]
        assert payload['max_tokens'] == 393216
        assert timeout['read'] is None and timeout['connect'] == 15
        if payload['thinking']['type'] == 'enabled':
            assert payload['reasoning_effort'] == cfg.effort
        assert client.calls == 101
        assert client.remaining_seconds() is None
    finally:
        client.close()


@pytest.mark.parametrize('stage', ['solve','review','math_repair','diagram','deferred_review'])
def test_responses_uses_same_discovered_output_limit(cfg, stage):
    cfg = replace(cfg, compact_tools=True, solve_responses=True, review_responses=True)
    seen = []
    def handler(req):
        seen.append((str(req.url), json.loads(req.content), req.extensions['timeout']))
        return httpx.Response(200, json={'status':'completed','output':[
            {'type':'message','content':[{'type':'output_text','text':'{}'}]}]})
    client = DeepSeek(cfg, lambda *a: None, httpx.MockTransport(handler))
    try:
        assert client.generate(stage, [], output_schema={'type':'object','properties':{},'additionalProperties':False}) == {}
        assert seen[0][0].endswith('/responses')
        assert seen[0][1]['max_output_tokens'] == 393216
        assert seen[0][2]['read'] is None
    finally:
        client.close()


def test_production_still_refuses_exhausted_calls(settings):
    sent=[]
    client=DeepSeek(settings,lambda *a:None,httpx.MockTransport(lambda req:sent.append(req)))
    client.calls=settings.max_calls
    try:
        with pytest.raises(ModelError, match='预算'):
            client.generate('solve', [])
        assert not sent
    finally:client.close()


def test_validation_tool_loop_can_finish_after_old_four_round_limit(cfg):
    seen=[];audit=[]
    def handler(req):
        body=json.loads(req.content);seen.append(body)
        assert body.get('tool_choice') != 'none'
        if len(seen) <= 6:
            return httpx.Response(200, json=chat(None,'tool_calls',[
                {'id':f'c{len(seen)}','type':'function','function':{'name':'calculate','arguments':'{}'}}]))
        assert sum(m['role']=='tool' for m in body['messages']) == 6
        return httpx.Response(200, json=chat('{"done":true}'))
    client=DeepSeek(replace(cfg,max_calls=1),lambda *a:audit.append(a),httpx.MockTransport(handler))
    try:
        assert client.generate('solve', [], tools=[{'function':{'name':'calculate'}}],
                               dispatch=lambda *a:{'ok':True}) == {'done':True}
        assert client.calls==7
        assert not any(k=='agent_finalize' for k,d in audit)
    finally:client.close()


@pytest.mark.parametrize('kind',['length','http','network','invalid_json'])
def test_validation_does_not_retry_or_publish_invalid_responses(cfg, kind):
    sent=[]
    def handler(req):
        sent.append(req)
        if kind=='http':return httpx.Response(429)
        if kind=='network':raise httpx.ReadTimeout('private-secret',request=req)
        return httpx.Response(200,json=chat('{' if kind=='invalid_json' else '{}', 'length' if kind=='length' else 'stop'))
    client=DeepSeek(cfg,lambda *a:None,httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError):client.generate('solve', [])
        assert len(sent)==1
    finally:client.close()


def test_unresolved_validation_limit_fails_before_generation(cfg):
    sent=[]
    client=DeepSeek(replace(cfg,validation_max_tokens=0),lambda *a:None,httpx.MockTransport(lambda req:sent.append(req)))
    try:
        with pytest.raises(ModelError,match='尚未解析'):client.generate('solve', [])
        assert not sent
    finally:client.close()


def test_model_metadata_lookup_is_only_get_and_keeps_key_private(cfg):
    seen=[]
    def handler(req):
        seen.append(req)
        return httpx.Response(200,json={'data':[{'id':cfg.model,'max_output_tokens':393216,'context_window':1048576}]})
    assert resolve_output_limit(cfg,transport=httpx.MockTransport(handler))==393216
    assert len(seen)==1 and seen[0].method=='GET' and seen[0].url.path=='/models'
    assert seen[0].headers['Authorization']=='Bearer '+cfg.api_key


@pytest.mark.parametrize('value',[None,0,-1,True,500,'393216',1048577])
def test_bad_metadata_never_silently_uses_production_cap(cfg,value):
    transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'data':[
        {'id':cfg.model,'max_output_tokens':value,'context_window':1048576}]}))
    with pytest.raises(EvaluationError,match='--max-output-tokens'):
        resolve_output_limit(cfg,transport=transport)


@pytest.mark.parametrize('status',[302,401,403,429,500])
def test_metadata_errors_do_not_echo_secrets(cfg,status):
    transport=httpx.MockTransport(lambda req:httpx.Response(status,text=cfg.api_key))
    with pytest.raises(EvaluationError) as exc:resolve_output_limit(cfg,transport=transport)
    assert cfg.api_key not in str(exc.value)


def test_override_is_explicit_and_makes_no_metadata_request(cfg):
    transport=httpx.MockTransport(lambda req:pytest.fail('metadata must not be requested'))
    assert resolve_output_limit(cfg,96000,transport=transport)==96000
    with pytest.raises(EvaluationError):resolve_output_limit(cfg,True,transport=transport)


def test_shared_budget_no_cap_keeps_counting_and_cancel():
    budget=ParallelBudget(None)
    for i in range(35):
        with budget.request('solve') as lease:
            assert lease.call_id==i+1
            lease.record_usage({'prompt_tokens':1,'completion_tokens':1})
    assert budget.remaining_calls is None and budget.remaining_seconds() is None
    assert budget.snapshot()['calls']==35
    assert budget.snapshot()['remaining_calls'] is None
    assert 'Infinity' not in json.dumps(budget.snapshot(),allow_nan=False)
    budget.cancel('operator cancelled')
    with pytest.raises(BudgetCancelled):
        with budget.request('review'):pass


def test_validation_can_still_use_an_explicit_deadline(cfg):
    limited=replace(cfg,validation_timeout=180)
    assert task_timeout(limited)==180
    client=DeepSeek(limited,lambda *a:None,httpx.MockTransport(lambda req:pytest.fail('already expired')))
    client.started-=200
    try:
        with pytest.raises(ModelError):client.generate('solve', [])
    finally:client.close()


@pytest.mark.parametrize('mode,expected',[(False,'failed'),(True,'interrupted')])
def test_worker_timeout_production_and_manual_cancel_validation(settings,monkeypatch,mode,expected):
    from math_agent import worker
    cfg=replace(settings,validation_mode=mode)
    state={'status':'running'};beats=[]
    store=SimpleNamespace(get=lambda j:state, finish=lambda j,status,**kw:state.update(status=status,**kw))
    process=SimpleNamespace(poll=lambda:None)
    monkeypatch.setattr(worker.subprocess,'Popen',lambda *a,**kw:process)
    monkeypatch.setattr(worker,'terminate',lambda p:None)
    times=iter([0,1000,1001,1002])
    monkeypatch.setattr(worker.time,'monotonic',lambda:next(times))
    monkeypatch.setattr(worker.time,'sleep',lambda s:None)
    worker.supervise(cfg,store,'test',lambda:len(beats)>=2,lambda:beats.append(True))
    assert state['status']==expected


def test_classic_graph_does_not_stop_at_production_repair_or_recursion_budget(cfg,sample,tmp_path):
    from math_agent.contracts import Source,Candidate,Review
    from math_agent.flow import execute_graph
    source=Source.model_validate(sample[0]);candidate=Candidate.model_validate(sample[1])
    review=Review.model_validate(sample[2]);attempts=[]
    def revise(*a):
        attempts.append(1)
        if len(attempts)>6:return review
        return review.model_copy(update={'verdict':'revise','issues':[
            __import__('math_agent.contracts',fromlist=['Issue']).Issue(stage='diagram',target='Q1',detail='repair') ]})
    runner=SimpleNamespace(settings=replace(cfg,max_repairs=0),directory=tmp_path,audit=lambda *a:None,
        extract=lambda *a:source,solve=lambda *a,**kw:candidate,observe=lambda c:{},review=revise)
    state=execute_graph(runner)
    assert state['review'].verdict=='pass' and state['repairs']==6 and len(attempts)==7


def test_cli_defaults_validation_and_production_is_explicit(monkeypatch,tmp_path):
    from math_agent.evaltools import cli
    from math_agent.evaltools import runner
    captured=[]
    monkeypatch.setattr(cli,'discover',lambda p:{1:'case'})
    monkeypatch.setattr(cli,'select',lambda *a:['case'])
    monkeypatch.setattr(runner,'run_batch',lambda *a,**kw:(captured.append(kw) or (tmp_path,0)))
    assert cli.main(['run','1','--dry-run'])==0
    assert captured[-1]['budget_mode']=='validation'
    assert cli.main(['run','1','--budget-mode','production','--dry-run'])==0
    assert captured[-1]['budget_mode']=='production'


def test_eval_policy_propagates_to_subprocess_environment_and_manifest(tmp_path,monkeypatch):
    from PIL import Image
    from math_agent.evaltools import runner
    from math_agent.evaltools.inputs import Case
    path=tmp_path/'01.png';Image.new('RGB',(20,20)).save(path)
    root=tmp_path/'project';root.mkdir()
    (root/'.env').write_text('DEEPSEEK_API_KEY=fake\nDEEPSEEK_MAX_TOKENS=12000\nAGENT_JOB_TIMEOUT=240\n')
    monkeypatch.setattr(runner,'resolve_output_limit',lambda *a:pytest.fail('dry-run has no HTTP'))
    monkeypatch.setenv('AGENT_VALIDATION_MODE','false')
    before=dict(os.environ)
    try:
        out,code=runner.run_batch([Case(1,path,())],root,dry_run=True,budget_mode='validation')
        assert code==0 and os.environ['AGENT_VALIDATION_MODE']=='true'
        data=json.loads((out/'manifest.json').read_text())
        assert data['budget_mode']=='validation'
        limits=data['settings']['execution_limits']
        assert limits['task_timeout_seconds'] is None and limits['model_call_limit'] is None
        assert limits['model_output_tokens']=='auto'
        assert data['settings']['max_tokens']==12000  # Recorded as configured, not effective.
        assert 'fake' not in (out/'manifest.json').read_text()
        runner.run_batch([Case(1,path,())],root,dry_run=True,budget_mode='production')
        assert os.environ['AGENT_VALIDATION_MODE']=='false'
    finally:
        os.environ.clear();os.environ.update(before)


def test_validation_cache_policy_is_distinct(cfg,settings):
    from math_agent.cache import key_for
    req={'text':'same','images':[]}
    assert key_for(cfg,req)!=key_for(settings,req)
    assert key_for(cfg,req)!=key_for(replace(cfg,validation_max_tokens=64000),req)


def test_shared_validation_job_context_has_no_production_deadline(cfg,tmp_path):
    from math_agent.application.context import JobContext
    from math_agent.storage import Store,write_json
    cfg=replace(cfg,data_dir=tmp_path,compact_tools=True,parallel_questions=True)
    store=Store(tmp_path);job,folder=store.prepare()
    write_json(folder/'request.json',{'text':'offline','images':[],'force':True})
    fake=lambda *a:SimpleNamespace(calls=0)
    runtime=JobContext(cfg,job,provider_factory=fake,cache_factory=fake)
    assert runtime.shared_budget.max_calls is None
    assert runtime.shared_budget.deadline is None
    assert runtime.provider.shared_budget is runtime.shared_budget
