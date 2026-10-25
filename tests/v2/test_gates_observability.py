import copy
import json
import threading
from contextlib import nullcontext
from types import SimpleNamespace
import pytest
from math_agent.domain.v2 import Draft,digest
from math_agent.contracts import Source
from math_agent.compilers.scene import compile_candidate
from math_agent.services.reviewing import operation_issues
from math_agent.observability.timing import summarize,union_seconds
from math_agent.observability.langsmith import SafeTrace


def test_circular_or_negative_review_cannot_publish(source_v2,draft_v2,review_v2):
    source=Source.model_validate(source_v2);draft=Draft.model_validate(draft_v2)
    candidate,_=compile_candidate(source,draft,{})
    bundle={'receipts':{'half_length':{'execution_status':'ok','verdict':'fail'}}}
    decisions=review_v2['operation_reviews']
    with pytest.raises(ValueError,match='verified receipt'):
        operation_issues(source,candidate,draft,bundle,decisions)
    decisions['half_length']['method']='independent_proof'
    issues,_=operation_issues(source,candidate,draft,bundle,decisions)
    assert issues and issues[0].stage=='math'
    decisions['half_length']['basis_ids']=['Q1:S1']
    with pytest.raises(ValueError,match='own target'):
        operation_issues(source,candidate,draft,bundle,decisions)


def test_unknown_requires_explicit_alternative_proof(source_v2,draft_v2,review_v2):
    source=Source.model_validate(source_v2);draft=Draft.model_validate(draft_v2)
    candidate,_=compile_candidate(source,draft,{})
    bundle={'receipts':{'half_length':{'execution_status':'unsupported','verdict':'unknown'}}}
    decisions=review_v2['operation_reviews']
    with pytest.raises(ValueError):operation_issues(source,candidate,draft,bundle,decisions)
    decisions['half_length']['method']='independent_proof'
    issues,parsed=operation_issues(source,candidate,draft,bundle,decisions)
    assert not issues and parsed['half_length']['method']=='independent_proof'


def test_graph_uses_real_barrier_and_separate_branch_fields(monkeypatch):
    from math_agent.workflows.evidence_v2 import graph as g
    from math_agent.workflows.evidence_v2.nodes import preparation,verification,review
    barrier=threading.Barrier(2,timeout=3)
    monkeypatch.setattr(preparation,'extract_source',lambda s,c:{})
    monkeypatch.setattr(preparation,'propose_solution',lambda s,c:{})
    monkeypatch.setattr(preparation,'prepare_execution',lambda s,c:{'route':'execute'})
    def math(s,c):barrier.wait();return {'math_evidence':{'done':True}}
    def diagram(s,c):barrier.wait();return {'diagram_evidence':{'done':True}}
    monkeypatch.setattr(verification,'check_math',math)
    monkeypatch.setattr(verification,'compile_and_observe',diagram)
    def join(s,c):
        assert s['math_evidence']['done'] and s['diagram_evidence']['done']
        return {'route':'review'}
    monkeypatch.setattr(verification,'join_evidence',join)
    monkeypatch.setattr(review,'independent_review',lambda s,c:{'route':'publish'})
    monkeypatch.setattr(review,'publish',lambda s,c:{'completed':True})
    ctx=SimpleNamespace(check=lambda:None,runner=SimpleNamespace(audit=lambda *a:None),trace=SimpleNamespace(span=lambda *a:nullcontext()))
    result=g.build_graph('parallel').invoke({},context=ctx,config={'max_concurrency':2})
    assert result['completed']


def test_stale_branch_is_rejected():
    from math_agent.workflows.evidence_v2.nodes.verification import join_evidence
    with pytest.raises(ValueError,match='revisions'):
        join_evidence({'candidate_revision':'new','math_evidence':{'candidate_revision':'old'},
                       'diagram_evidence':{'candidate_revision':'new'}},None)


def test_duration_not_cumulative_and_parallel_union():
    data={'audit':[
        {'kind':'model','stage':'solve','seconds':30,'duration_seconds':30,'usage':{'completion_tokens':50,'completion_tokens_details':{'reasoning_tokens':30}}},
        {'kind':'model','stage':'review','seconds':90,'duration_seconds':60,'usage':{'output_tokens':20}},
        {'kind':'model','stage':'review','seconds':90,'duration_seconds':30,'usage':{}},
        {'kind':'model','stage':'legacy','seconds':200},
        {'kind':'model_request_started','request_id':'not-finished'}]}
    result=summarize(data)
    assert result['model_work_seconds_sum']==120
    assert result['model_interval_union_seconds']==90
    assert result['model_stages']['solve']['output_tokens']==50
    assert result['model_stages']['solve']['reasoning_tokens']==30
    assert result['model_stages']['legacy']['missing_duration']==1
    assert result['unknown_completion_requests']==['not-finished']


def test_trace_disabled_never_constructs_client(monkeypatch):
    import langsmith
    monkeypatch.delenv('LANGSMITH_TRACING',raising=False)
    monkeypatch.setattr(langsmith,'Client',lambda **kw:pytest.fail('must stay offline'))
    trace=SafeTrace('job','serial')
    with trace.span('solve'):trace.completed('model',{'seconds':1,'stage':'solve'})
    trace.close(True)
    assert trace.root is None


def test_trace_does_not_export_raw_state_errors_or_credentials(monkeypatch):
    import langsmith
    captured=[]
    class Run:
        def __init__(self,**kw):captured.append(kw)
        def post(self):pass
        def create_child(self,**kw):return Run(**kw)
        def end(self,**kw):captured.append(kw)
        def patch(self):pass
    monkeypatch.setenv('LANGSMITH_TRACING','true');monkeypatch.setenv('LANGSMITH_API_KEY','not-a-real-key')
    monkeypatch.setattr(langsmith,'Client',lambda **kw:SimpleNamespace(flush=lambda:None))
    monkeypatch.setattr(langsmith,'RunTree',Run)
    trace=SafeTrace('job','serial')
    with pytest.raises(ValueError):
        with trace.span('review'):
            trace.completed('model',{'seconds':1,'call':'SENSITIVE-CONTENT','stage':'solve',
                'content':'PRIVATE', 'usage':{'prompt_tokens':3,'secret':'PRIVATE'},'error':'PRIVATE'})
            raise ValueError('PRIVATE')
    trace.close(False)
    text=str(captured)
    assert 'PRIVATE' not in text and 'SENSITIVE-CONTENT' not in text and 'not-a-real-key' not in text
    assert 'ValueError' in text and 'prompt_tokens' in text


def test_trace_failure_is_not_a_model_retry(monkeypatch):
    import langsmith
    monkeypatch.setenv('LANGSMITH_TRACING','true');monkeypatch.setenv('LANGSMITH_API_KEY','fake')
    def broken(**kw):raise RuntimeError('network failure')
    monkeypatch.setattr(langsmith,'Client',broken)
    trace=SafeTrace('job','serial')
    with trace.span('solve'):pass
    trace.close(False)


def test_previous_issue_requires_explicit_revision_resolution(source_v2,draft_v2):
    from math_agent.services.reviewing import revision_issues
    source=Source.model_validate(source_v2);draft=Draft.model_validate(draft_v2)
    candidate,_=compile_candidate(source,draft,{})
    bundle={'prior_evidence':[{'issues':[{'stage':'math','target':'Q1','detail':'negative observation'}]}]}
    with pytest.raises(ValueError,match='every prior issue'):revision_issues(source,candidate,bundle,{})
    issues,_=revision_issues(source,candidate,bundle,{'R1I1':{
        'verdict':'unknown','basis_ids':['C1'],'evidence':'仍缺少依据'}})
    assert issues[0].stage=='math'
