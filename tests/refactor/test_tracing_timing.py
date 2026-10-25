import json
import pytest
from math_agent.observability.tracing import TraceSink
from math_agent.observability.timing import summarize

class Capture:
    def __init__(self):self.calls=[]
    def create_run(self,**kw):self.calls.append(('create',kw))
    def update_run(self,run_id,**kw):self.calls.append(('end',kw))


def test_default_tracing_never_creates_client(monkeypatch):
    monkeypatch.delenv('LANGSMITH_TRACING',raising=False)
    monkeypatch.setenv('LANGSMITH_API_KEY','not-a-real-secret')
    import langsmith
    monkeypatch.setattr(langsmith,'Client',lambda **kw:pytest.fail('default must stay offline'))
    assert TraceSink.from_env().client is None


def test_opt_in_creates_hiding_client(monkeypatch):
    monkeypatch.setenv('LANGSMITH_TRACING','true');monkeypatch.setenv('LANGSMITH_API_KEY','dummy')
    import langsmith
    values={}
    def client(**kwargs):values.update(kwargs);return Capture()
    monkeypatch.setattr(langsmith,'Client',client)
    assert TraceSink.from_env().client is not None
    assert values['hide_inputs'] and values['hide_outputs']


def test_sensitive_state_metadata_and_exception_never_uploaded():
    capture=Capture();trace=TraceSink(capture)
    secret='PRIVATE-TEST-CREDENTIAL';image='data:image/png;base64,PRIVATE-PIXELS'
    with pytest.raises(ValueError):
        with trace.span('job'):
            with trace.span('solve'):
                trace.completed('model',{'stage':'solve','seconds':1.5,'call':1,
                    'api_key':secret,'arguments':image,'error':secret,
                    'usage':{'completion_tokens':12,'completion_tokens_details':{'reasoning_tokens':7,'secret':secret},'extra':image}})
                raise ValueError(secret+image)
    text=json.dumps(capture.calls,default=str)
    assert secret not in text and image not in text
    creates=[row for kind,row in capture.calls if kind=='create']
    assert [row['name'] for row in creates]==['job','solve','model']
    assert creates[1]['parent_run_id']==creates[0]['id']
    assert creates[2]['parent_run_id']==creates[1]['id']
    assert all(row['inputs']=={} for row in creates)
    assert creates[2]['extra']['metadata']['usage']['completion_tokens']==12


def test_tracing_failure_does_not_retry_or_swallow_work():
    class Broken:
        def create_run(self,**kw):raise RuntimeError('network failure')
        def update_run(self,*a,**kw):raise RuntimeError('network failure')
    trace=TraceSink(Broken());work=[]
    with trace.span('job'):work.append(1)
    assert work==[1] and trace.failures==2
    with pytest.raises(KeyError):
        with trace.span('job'):raise KeyError('business failure')


def test_timing_uses_duration_and_parallel_union_not_cumulative_sum():
    report={'seconds':200,'audit':[
        {'kind':'model','stage':'solve','seconds':100,'duration_seconds':60,'usage':{'completion_tokens':30}},
        {'kind':'model','stage':'review','seconds':120,'duration_seconds':50,'usage':{'output_tokens':20,'output_tokens_details':{'reasoning_tokens':12}}},
        {'kind':'model','stage':'review','seconds':130,'error':'timeout'},
        {'kind':'model','stage':'review','seconds':130,'not_sent':True}]}
    value=summarize(report)
    assert value['model_wall_seconds_known']==80
    assert sum(s['model_seconds'] for s in value['model_stages'].values())==110
    assert value['model_stages']['review']['completion_tokens']==20
    assert value['model_stages']['review']['missing_duration']==1
    assert value['model_stages']['review']['missing_usage']==1
    assert value['model_stages']['review']['calls']==2
    assert value['model_stages']['review']['not_sent']==1
