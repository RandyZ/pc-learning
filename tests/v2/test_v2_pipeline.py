import copy
import json
import os

import pytest
from math_agent.domain.v2 import Draft,digest
from math_agent.contracts import Source
from math_agent.compilers.scene import compile_candidate


@pytest.mark.parametrize('profile',['serial','parallel','parallel_reuse'])
def test_three_stage_http_pipeline_keeps_original_settings(make_runner,source_v2,draft_v2,review_v2,profile):
    r=make_runner([source_v2,draft_v2,review_v2],profile=profile)
    r.run()
    report=json.loads((r.directory/'report.json').read_text())
    assert report['status']=='completed', report.get('error')
    assert report['model_calls_this_job']==3
    assert report['workflow']=='evidence_v2'
    assert len(r.sent)==3
    assert r.sent[1]['thinking']['type']=='enabled'
    assert r.sent[1]['reasoning_effort']==r.settings.effort
    assert r.sent[1]['max_tokens']==r.settings.max_tokens
    assert (r.directory/'v2-source-map.json').is_file()
    assert (r.directory/'perf.json').is_file()
    assert 'offline-test-key' not in (r.directory/'effective-config.json').read_text()
    assert all(x['kind']!='v2_change' for x in r.records)


def test_missing_computation_decisions_fail_closed(make_runner,source_v2,draft_v2,review_v2):
    review_v2.pop('operation_reviews')
    r=make_runner([source_v2,draft_v2,review_v2]);r.run()
    assert r.store.get(r.job_id)['status']=='failed'
    assert r.model_calls==3
    assert not r.cache.data
    assert r.provider.review_bundle is None
    assert r.provider.operation_basis_map is None


def test_bad_source_uncertainty_does_not_solve(make_runner,source_v2):
    source_v2['uncertainties']=['长度不清楚']
    r=make_runner([source_v2]);r.run()
    assert r.store.get(r.job_id)['status']=='needs_input'
    assert r.model_calls==1


def test_native_browser_pipeline(make_runner,source_v2,draft_v2,review_v2):
    if os.getenv('MATH_AGENT_BROWSER_TEST')!='1':pytest.skip('requires installed Chromium')
    from math_agent.rendering import render
    r=make_runner([source_v2,draft_v2,review_v2],render=render)
    r.run()
    report=json.loads((r.directory/'report.json').read_text())
    assert report['status']=='completed', report.get('error')
    assert report['render_check']['sample_count']>=1
    assert (r.directory/'output/preview.png').stat().st_size>100


def test_cached_task_uses_no_model_and_revalidates(make_runner,source_v2,draft_v2,review_v2):
    r=make_runner([source_v2,draft_v2,review_v2]);r.run()
    assert r.store.get(r.job_id)['status']=='completed',r.store.get(r.job_id)
    other=make_runner([],cache=r.cache);other.run()
    report=json.loads((other.directory/'report.json').read_text())
    assert report['status']=='completed',report.get('error')
    assert report['cache_hit'] and report['model_calls_this_job']==0
    assert not other.sent


def test_corrupted_cache_fails_without_model_fallback(make_runner,source_v2,draft_v2,review_v2):
    r=make_runner([source_v2,draft_v2,review_v2]);r.run()
    cached=next(iter(r.cache.data.values()))
    cached['state']['draft']['questions']['Q1']['proof']['result']='tampered'
    other=make_runner([],cache=r.cache);other.run()
    assert other.store.get(other.job_id)['status']=='failed'
    assert not other.sent


def test_diagram_patch_does_not_resolve_or_change_math(make_runner,source_v2,draft_v2,review_v2):
    draft_v2['questions']['Q1']['scene']['required_visible'].append('Missing')
    original=copy.deepcopy(draft_v2['questions']['Q1']['proof'])
    def patch(body):
        ctx=json.loads(body['messages'][1]['content'][0]['text'])['context']
        scene=copy.deepcopy(draft_v2['questions']['Q1']['scene'])
        scene['required_visible'].remove('Missing')
        return {'expected_candidate_revision':ctx['candidate_revision'],'reason':'修正无关的可见对象要求',
                'updates':{'Q1':{'scene':scene}}}
    review_v2['revision_reviews']={'R1I1':{'verdict':'resolved','basis_ids':['C1'],
        'evidence':'仅移除了并非题设或证明对象的错误可见对象引用；A、B、M及中点关系仍完整。'}}
    r=make_runner([source_v2,draft_v2,patch,review_v2],profile='parallel_reuse');r.run()
    report=json.loads((r.directory/'report.json').read_text())
    assert report['status']=='completed',report.get('error')
    saved=json.loads((r.directory/'v2-draft.json').read_text())
    assert saved['questions']['Q1']['proof']==Draft.model_validate(draft_v2).questions['Q1'].proof.model_dump()
    assert len(r.sent)==4
    assert len([x for x in r.records if x['kind']=='v2_math_batch'])==1
    assert any(x.get('reused_from') for x in r.records if x['kind']=='v2_receipt')


def test_browser_infrastructure_failure_is_not_a_paid_repair(make_runner,source_v2,draft_v2):
    from math_agent.rendering import RenderError
    def unavailable(*args):raise RenderError('Page.goto: ERR_BLOCKED_BY_ADMINISTRATOR')
    r=make_runner([source_v2,draft_v2],render=unavailable);r.run()
    assert r.store.get(r.job_id)['status']=='failed'
    assert len(r.sent)==2
    assert not any(x['kind']=='repair' for x in r.records)


def test_native_responses_schema_keeps_extended_review_contract(make_runner,source_v2,draft_v2,review_v2):
    r=make_runner([source_v2,draft_v2,review_v2],solve_responses=True,review_responses=True)
    r.run()
    report=json.loads((r.directory/'report.json').read_text())
    assert report['status']=='completed',report.get('error')
    assert len(r.sent)==3
    assert r.sent[1]['text']['format']['strict']
    solve_schema=r.sent[1]['text']['format']['schema']
    assert solve_schema['properties']['questions']['required']==['Q1']
    assert solve_schema['$defs']['Proof']['required']==list(solve_schema['$defs']['Proof']['properties'])
    assert 'operation_reviews' in r.sent[2]['text']['format']['schema']['properties']


@pytest.mark.parametrize('review_responses',[False,True])
def test_operation_review_basis_schema_and_prompt_follow_public_order(
        make_runner,source_v2,draft_v2,review_v2,review_responses):
    proof=draft_v2['questions']['Q1']['proof']
    half=proof['steps'][0]
    half['basis_ids']=['Q1:first']
    proof['steps']=[
        {'id':'first','premise':'AB=2','rule':'已知','conclusion':'AB=2','basis_ids':['C1']},
        half,
        {'id':'last','premise':'AM=1','rule':'结论','conclusion':'AM=1','basis_ids':['Q1:half']}]
    draft_v2['questions']['Q1']['computations']=[
        {'id':'first_check','op':'simplify','target_step_id':'first','basis_ids':['C1'],'expression':'2'},
        {'id':'half_length','op':'simplify','target_step_id':'half','basis_ids':['Q1:first'],'expression':'2/2'},
        {'id':'last_check','op':'simplify','target_step_id':'last','basis_ids':['Q1:half'],'expression':'1'}]
    review_v2['operation_reviews']={
        'first_check':{'verdict':'pass','method':'receipt','basis_ids':['C1'],'evidence':'核对已知'},
        'half_length':{'verdict':'pass','method':'receipt','basis_ids':['Q1:S1'],'evidence':'核对中点计算'},
        'last_check':{'verdict':'pass','method':'receipt','basis_ids':['Q1:S2'],'evidence':'核对结论'}}
    r=make_runner([source_v2,draft_v2,review_v2],review_responses=review_responses)
    r.run()
    report=json.loads((r.directory/'report.json').read_text())
    assert report['status']=='completed',report.get('error')

    expected={
        'first_check':('first',['C1']),
        'half_length':('half',['C1','Q1:S1']),
        'last_check':('last',['C1','Q1:S1','Q1:S2'])}
    request=r.sent[2]
    prompt=(request['messages'][0]['content'] if not review_responses
            else request['input'][0]['content'])
    assert '目标自身及后续步骤不可引用' in prompt
    for oid,(target,allowed) in expected.items():
        assert f'"{oid}"' in prompt
        assert f'"target": "Q1:{target}"' in prompt
        assert json.dumps(allowed,ensure_ascii=False) in prompt
        if review_responses:
            schema=request['text']['format']['schema']['properties']['operation_reviews']['properties'][oid]
            assert schema['properties']['basis_ids']['items']['enum']==allowed
    assert r.provider.review_bundle is None
    assert r.provider.operation_basis_map is None
