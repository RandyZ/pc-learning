"""Diagnostic policy tests use synthetic inputs and local HTTP/process doubles only."""
from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

from benchmarks import diagnostic_mode as mode
from benchmarks import suite
from math_agent.config import Settings
from math_agent.provider import DeepSeek, ModelError
from math_agent.parallel_budget import ParallelBudget, CallsExhausted, BudgetCancelled


def config(tmp_path, **values):
    return mode.create_settings(data_dir=tmp_path, api_key='offline-fixture-key', access_token='x'*40,
                                **values)


@pytest.mark.parametrize('variable', ['AGENT_DIAGNOSTIC_MODE', 'DIAGNOSTIC_MODE', 'AGENT_JOB_TIMEOUT_DISABLED'])
def test_environment_cannot_disable_production_deadline(tmp_path, monkeypatch, variable):
    monkeypatch.setenv(variable, 'true')
    settings=Settings(data_dir=tmp_path)
    assert not getattr(settings,'diagnostic_mode',False)
    assert settings.job_timeout==240 and settings.max_tokens==12000


def test_diagnostic_settings_keep_loop_limits_and_model_strength(tmp_path):
    settings=config(tmp_path,effort='high',max_calls=12,max_repairs=2,max_tokens=12000)
    assert settings.diagnostic_mode and settings.review_reserve_seconds==0
    assert settings.http_timeout==1200 and settings.max_calls==12 and settings.max_repairs==2
    assert settings.effort=='high' and settings.max_tokens==12000  # Inactive, not sent on the wire.
    with pytest.raises(ValueError):config(tmp_path,redis_url='redis://localhost:6379/0')
    with pytest.raises(ValueError):config(tmp_path,review_reserve_seconds=30)
    with pytest.raises(ValueError):config(tmp_path,api_style='glm')


@pytest.mark.parametrize('stage',['source','solve','review','diagram','math_repair'])
def test_chat_diagnostic_omits_output_cap_and_keeps_usage(tmp_path,stage):
    requests=[];events=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'model':'fixture','choices':[{'message':{'role':'assistant','content':'{}'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':3,'completion_tokens':2}})
    settings=config(tmp_path,solve_max_tokens=512,review_max_tokens=512)
    provider=DeepSeek(settings,lambda k,d:events.append((k,d)),transport=httpx.MockTransport(handler))
    provider.shared_budget=ParallelBudget(settings.max_calls)
    try:
        provider.request(stage,[{'role':'user','content':'JSON test'}])
        body=json.loads(requests[0].content)
        assert 'max_tokens' not in body
        assert requests[0].extensions['timeout']['read']==1200
        assert next(d for k,d in events if k=='model_budget')['max_tokens'] is None
        assert provider.shared_budget.snapshot()['usage']['known_completion_tokens']==2
    finally:provider.close()


def test_normal_transport_still_sends_configured_cap(tmp_path):
    seen=[]
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'{}'},'finish_reason':'stop'}]})
    provider=DeepSeek(Settings(data_dir=tmp_path,max_tokens=12000),lambda *_:None,transport=httpx.MockTransport(handler))
    try:provider.request('solve',[{'role':'user','content':'JSON'}])
    finally:provider.close()
    assert seen[0]['max_tokens']==12000


def test_responses_diagnostic_omits_cap(tmp_path):
    seen=[];events=[]
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"ok":true}'}]}],
            'usage':{'input_tokens':2,'output_tokens':3}})
    provider=DeepSeek(config(tmp_path,compact_tools=True,review_responses=True),lambda k,d:events.append((k,d)),
                       transport=httpx.MockTransport(handler))
    try:
        provider.request('review',[{'role':'user','content':'JSON'}],output_schema={
            'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False})
    finally:provider.close()
    assert 'max_output_tokens' not in seen[0]
    assert next(d for k,d in events if k=='model_budget')['max_tokens'] is None


def test_provider_default_truncation_still_fails_without_retry(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'{"unfinished":'},'finish_reason':'length'}],
            'usage':{'prompt_tokens':1,'completion_tokens':65536}})
    provider=DeepSeek(config(tmp_path),lambda *_:None,transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError,match='截断'):provider.request('solve',[{'role':'user','content':'JSON'}])
    finally:provider.close()
    assert len(calls)==1


def test_agent_loop_none_deadline_does_not_force_output_early():
    from math_agent.agent_loop import run_loop
    events=[]
    def request(*_):return {'role':'assistant','content':'{}'},'stop'
    assert run_loop(request,[],None,None,lambda k,d:events.append((k,d)),'solve',RuntimeError,
                    remaining_seconds=lambda:None,remaining_calls=lambda:10)=={}
    assert not any(k=='agent_finalize' for k,d in events)


def test_no_deadline_still_enforces_calls_and_cancellation():
    clock=[0.0]
    budget=ParallelBudget(1,clock=lambda:clock[0])
    clock[0]=100000.0
    budget.check()
    assert budget.remaining_seconds() is None
    with budget.request('solve'):pass
    with pytest.raises(CallsExhausted):
        with budget.request('review'):pass
    budget.cancel('manual stop')
    with pytest.raises(BudgetCancelled):budget.check()


def test_diagnostic_supervisor_does_not_consult_business_clock(tmp_path,monkeypatch):
    from math_agent import worker
    state={'status':'running'};calls=[];ticks=[]
    class Process:
        def poll(self):return 0 if state['status']=='completed' else None
    proc=Process()
    monkeypatch.setattr(mode.subprocess,'Popen',lambda *a,**k:(calls.append(k) or proc))
    monkeypatch.setattr(worker,'terminate',lambda p:calls.append('cleanup'))
    # A fake job takes over an hour on the business clock, but only three loop ticks.
    def tick(_):
        ticks.append(1200)
        if len(ticks)==3:state['status']='completed'
    monkeypatch.setattr(mode.time,'sleep',tick)
    store=SimpleNamespace(get=lambda _:state,finish=lambda *a,**k:pytest.fail('no deadline should fire'))
    mode.supervise(config(tmp_path,job_timeout=30),store,'a'*32,lambda:False,lambda:None)
    assert len(ticks)==3 and calls[-1]=='cleanup' and len(calls)==2
    assert calls[0]['start_new_session'] is True


def test_cancellation_before_launch_does_not_start_child(tmp_path,monkeypatch):
    monkeypatch.setattr(mode.subprocess,'Popen',lambda *a,**k:pytest.fail('must not launch'))
    changes=[]
    store=SimpleNamespace(finish=lambda *a,**kw:changes.append(a))
    mode.supervise(config(tmp_path),store,'j',lambda:True,lambda:None)
    assert changes[0][1]=='interrupted'


def test_prepare_only_has_no_paid_permission(tmp_path,monkeypatch):
    from benchmarks import run
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    monkeypatch.setenv('AGENT_WORKFLOW','classic')
    for key in ('AGENT_DATA_DIR','REDIS_URL'):monkeypatch.delenv(key,raising=False)
    out=tmp_path/'preview'
    assert run.main(['--profile','A1','--case-id','01','--text','fixture','--out',str(out),
                     '--diagnostic','--prepare-only'])==0
    manifest=json.loads((out/'manifest.json').read_text())
    assert manifest['effective_config']['job_timeout'] is None
    assert not list(out.rglob('diagnostic-permission.json'))


def test_effective_config_is_explicit_and_finite_json(tmp_path):
    from math_agent.observability.effective_config import effective
    result=effective(config(tmp_path,max_tokens=12000))
    assert result['job_timeout'] is None and result['max_tokens'] is None
    assert result['inactive_configured_limits']['max_tokens']==12000
    assert result['output_limit_policy']=='provider_default'
    assert 'offline-fixture-key' not in json.dumps(result,allow_nan=False)


def test_suite_diagnostic_preview_never_imports_runner_or_reads_key(tmp_path,monkeypatch,capsys):
    directory=tmp_path/'images';directory.mkdir()
    for name in ('01.png','01答案.png'):(directory/name).write_bytes(b'fixture')
    monkeypatch.setattr(suite,'load_env',lambda *a:pytest.fail('preview reads no credentials'))
    monkeypatch.setattr(suite,'launch',lambda *a:pytest.fail('preview starts no subprocess'))
    assert suite.main(['01','--data-dir',str(directory),'--diagnostic'])==0
    text=capsys.readouterr().out
    assert '诊断模式' in text and '模型调用为0' in text


def test_suite_propagates_mode_to_each_child_and_marks_report(tmp_path,monkeypatch):
    from test_comparison_suite import child_result
    directory=tmp_path/'images';directory.mkdir()
    for name in ('01.png','01答案.png'):(directory/name).write_bytes(b'fixture')
    monkeypatch.setattr(suite,'ROOT',tmp_path)
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-fixture-key')
    monkeypatch.setattr(suite,'preflight',lambda env,plan:None)
    calls=[]
    def launch(command,env,stop):
        calls.append(command)
        assert '--diagnostic' in command
        child_result(command,elapsed=650.)
        return 0,'fixture'
    monkeypatch.setattr(suite,'launch',launch)
    out=tmp_path/'results'
    assert suite.main(['01','--data-dir',str(directory),'--out',str(out),'--diagnostic','--run'])==0
    summary=json.loads((out/'summary.json').read_text())
    assert summary['test_mode']=='diagnostic' and len(calls)==2
    assert all(row['seconds']==650 and not row['completed_within_180s'] for row in summary['runs'])
    assert '不代表限时验收通过' in summary['note']


def test_v2_diagnostic_no_job_deadline_preserves_math_watchdog(make_runner,source_v2,draft_v2,review_v2):
    # Existing V2 fixture performs real math subprocesses; only model/browser are fixtures.
    r=make_runner([source_v2,draft_v2,review_v2],profile='parallel_reuse')
    values={f.name:getattr(r.settings,f.name) for f in fields(r.settings)}
    r.settings=mode.create_settings(**values)
    r.provider.settings=r.settings
    r.run()
    report=json.loads((r.directory/'report.json').read_text())
    assert report['status']=='completed',report.get('error')
    assert r.shared_budget.deadline is None and len(r.sent)==3
    assert all('max_tokens' not in body for body in r.sent)
    assert json.loads((r.directory/'run-manifest.json').read_text())['task_deadline_seconds'] is None


def test_real_diagnostic_process_is_cleaned_up_on_stop(tmp_path):
    import time
    from math_agent.storage import Store, write_json
    settings=config(tmp_path)
    store=Store(tmp_path);job,directory=store.prepare()
    write_json(directory/'request.json',{'text':'fixture','images':[],'force':True})
    store.enqueue(job);store.claim()
    ticks=[]
    start=time.monotonic()
    mode.supervise(settings,store,job,lambda:len(ticks)>=2,lambda:ticks.append(1),
        command=[sys.executable,'-c','import time;time.sleep(60)'])
    assert store.get(job)['status']=='interrupted'
    assert time.monotonic()-start<10
