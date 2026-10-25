"""Synthetic output/patch regressions. Never call a paid endpoint."""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from math_agent.contracts import Source
from math_agent.domain.v2 import Draft, digest
from math_agent.json_framing import close_completed_containers, audit_closure
from math_agent.services.field_correction import variable_fields, pointer
from math_agent.services.solving import PlanSolver
from benchmarks.diagnostics import diagnose


def source():
    return Source(title='中点', problem_text='AB=2，M为中点，求AM',
        conditions=[{'id':'C1','text':'AB=2，M为中点','origin':'question_text','image_ids':[]}],
        questions=[{'id':'Q1','text':'求AM'}], reference_summary='', uncertainties=[])


def answer():
    return {'questions': {'Q1': {
        'proof': {'result':'AM=1', 'steps':[{'id':'half','premise':'AB=2，M为中点',
            'rule':'中点定义','conclusion':'AM=1','basis_ids':['C1']}],
            'cases':[], 'condition_ids':['C1'], 'domain_and_boundaries':'AB>0'},
        'computations': [{'id':'half_length','op':'evaluate','expression':'x/2','expected':'1',
            'variables':['x'],'substitutions':{'x':2},'basis_ids':['C1'],'target_step_id':'half'}],
        'scene': {'objects':[{'id':'A','kind':'point','coords':[0,0],'label':'A'}]}}}}


def correction(raw):
    with pytest.raises(ValidationError) as failure:
        Draft.model_validate(raw)
    return variable_fields(raw, failure.value)


def patch_for(plan):
    return {'base_sha256':plan.base_sha256, 'replacements':{
        pointer(path):{name:'real' for name in symbols} for path,symbols in plan.fields}}


@pytest.mark.parametrize('prefix,suffix', [('', '}'), (' \n', ' \n} \t')])
def test_single_surplus_root_preserves_exact_object_text(prefix,suffix):
    text=prefix+json.dumps({'x':[1,{'literal':'} and \\"quotes"'}], 'unknown':['pending']})
    repaired=close_completed_containers(text+suffix)
    assert repaired==text
    events=[];audit_closure(lambda k,v:events.append((k,v)), 'solve', 2, text+suffix, repaired, 'chat')
    assert events[0][0]=='json_surplus_root_closure'
    assert events[0][1]['removed_suffix']==suffix
    assert 'literal' not in json.dumps(events)


@pytest.mark.parametrize('raw', [
    '{}', '[{}]}', '{}{}', '{"x":1}}}', '{"x":1}]}', '{"x":1} explanation',
    '{"x":1,"x":2}}', '{"nested":{"x":1,"x":2}}}',
    '{"x":NaN}}', '{"x":Infinity}}', '{"x":-Infinity}}',
    '{"x":"unterminated}}', '{"x":[1,2}}', '{"x":1,}}',
])
def test_surplus_recovery_never_guesses_content(raw):
    assert close_completed_containers(raw) is None


def test_replacement_preserves_equations_proof_scene_and_negative_expectations():
    raw=answer();raw['questions']['Q1']['computations'][0]['expected']='999'
    before=copy.deepcopy(raw);plan=correction(raw)
    merged=plan.apply(raw,patch_for(plan))
    assert raw==before
    assert merged['questions']['Q1']['computations'][0]['expected']=='999'
    expected=copy.deepcopy(raw);expected['questions']['Q1']['computations'][0]['variables']={'x':'real'}
    assert merged==expected
    assert Draft.model_validate(merged)


@pytest.mark.parametrize('attack',['base','changed_base','missing','extra_path','full_draft','extra_field',
                                  'renamed','added','invalid_assumption','null','bool'])
def test_field_patch_fails_closed(attack):
    raw=answer();plan=correction(raw);patch=patch_for(plan);key=next(iter(patch['replacements']))
    if attack=='base':patch['base_sha256']='0'*64
    elif attack=='changed_base':raw['questions']['Q1']['proof']['result']='changed'
    elif attack=='missing':patch['replacements']={}
    elif attack=='extra_path':patch['replacements']['/questions/Q1/proof/result']='changed'
    elif attack=='full_draft':patch=copy.deepcopy(raw)
    elif attack=='extra_field':patch['extra']='not allowed'
    elif attack=='renamed':patch['replacements'][key]={'y':'real'}
    elif attack=='added':patch['replacements'][key]={'x':'real','y':'real'}
    elif attack=='invalid_assumption':patch['replacements'][key]={'x':'complex'}
    elif attack=='null':patch['replacements'][key]=None
    elif attack=='bool':patch['replacements'][key]={'x':True}
    with pytest.raises(ValueError):plan.apply(raw,patch)


@pytest.mark.parametrize('kind',['duplicate','bad_symbol','non_string','too_many','mixed_errors'])
def test_unrecognized_errors_do_not_expand_patch_authority(kind):
    raw=answer();op=raw['questions']['Q1']['computations'][0]
    if kind=='duplicate':op['variables']=['x','x']
    elif kind=='bad_symbol':op['variables']=['../x']
    elif kind=='non_string':op['variables']=[1]
    elif kind=='too_many':op['variables']=['x','y','z','w','v']
    elif kind=='mixed_errors':raw['questions']['Q1']['proof']['result']=17
    assert correction(raw) is None


class StubRunner:
    def __init__(self,directory,replies,native=False):
        self.directory=directory
        self.settings=SimpleNamespace(solve_responses=native,review_responses=False)
        self.request={'text':'AB=2，M为中点','images':[]}
        self.events=[];self.sent=[];self.replies=replies
        self.provider=SimpleNamespace(generate=self.generate)
    @property
    def model_calls(self):return len(self.sent)
    def audit(self,kind,detail):self.events.append((kind,detail))
    def dispatch_stage_tool(self,*a):raise AssertionError('correction must not execute tools')
    def generate(self,stage,messages,**kwargs):
        self.sent.append((stage,messages,kwargs))
        reply=self.replies[len(self.sent)-1]
        return reply(messages,kwargs) if callable(reply) else copy.deepcopy(reply)


def return_patch(messages,options):
    payload=json.loads(messages[1]['content'][0]['text'])['context']
    fields=payload['field_correction']
    assert 'previous' not in payload and 'rejected_output' not in payload
    assert all('scene' not in q for q in fields['proof_context'].values())
    assert options['tools']==[]
    assert '不返回整份Draft' in messages[0]['content']
    return {'base_sha256':fields['base_sha256'], 'replacements':{
        item['path']:{name:'real' for name in item['symbols_to_preserve']} for item in fields['fields']}}


@pytest.mark.parametrize('native',[False,True])
def test_solver_requests_only_authorized_fields_and_validates_merged_result(tmp_path,native):
    raw=answer();r=StubRunner(tmp_path,[raw,return_patch],native)
    draft=PlanSolver(r,lambda:None).propose(source())
    assert draft.questions['Q1'].computations[0].variables=={'x':'real'}
    assert r.model_calls==2
    assert json.loads((tmp_path/'model-01-v2-solve.json').read_text())==raw
    assert set(json.loads((tmp_path/'model-02-v2-solve.json').read_text()))=={'base_sha256','replacements'}
    merged=json.loads((tmp_path/'model-02-v2-solve-merged.json').read_text())
    assert merged['questions']['Q1']['proof']==raw['questions']['Q1']['proof']
    assert any(kind=='field_correction' for kind,_ in r.events)
    if native:
        from jsonschema import validate
        schema=r.sent[1][2]['output_schema']
        validate(json.loads((tmp_path/'model-02-v2-solve.json').read_text()),schema)
        assert set(schema['properties'])=={'base_sha256','replacements'}


def test_failed_patch_does_not_buy_a_third_solver_request(tmp_path):
    raw=answer();r=StubRunner(tmp_path,[raw,raw])
    with pytest.raises(ValueError,match='not a full Draft'):
        PlanSolver(r,lambda:None).propose(source())
    assert r.model_calls==2
    assert not any(kind=='field_correction' for kind,_ in r.events)


def test_full_dependency_gate_still_rejects_self_proof_after_field_patch(tmp_path):
    raw=answer();raw['questions']['Q1']['proof']['steps'][0]['basis_ids']=['Q1:half']
    r=StubRunner(tmp_path,[raw,return_patch])
    with pytest.raises(ValueError,match='self or unknown'):
        PlanSolver(r,lambda:None).propose(source())
    assert r.model_calls==2 and not any(kind=='field_correction' for kind,_ in r.events)


def test_shape_is_not_silently_normalized_or_assumed():
    with pytest.raises(ValidationError):Draft.model_validate(answer())
    schema=Draft.model_json_schema()['$defs']['Operation']['properties']['variables']
    assert 'NOT the variables array' in schema['description']


def test_json_failure_summary_reports_location_not_raw_output():
    report={'status':'failed','error':'模型响应结构或 JSON 格式无效', 'audit':[
        {'kind':'model','call':3,'stage':'solve','finish_reason':'stop'},
        {'kind':'model_output_error','call':3,'stage':'solve','position':11453,'content':'PRIVATE','error':'PRIVATE'}]}
    value=diagnose(report)
    assert value['code']=='MODEL_JSON_INVALID' and value['position']==11453
    assert 'PRIVATE' not in json.dumps(value)


def test_contract_paths_are_visible_but_values_are_private():
    detail='4 validation errors for Draft\nquestions.Q2.computations.3.variables\nPRIVATE secret-key\n'
    report={'status':'failed','error':detail,'audit':[
        {'kind':'model','call':2,'stage':'solve','finish_reason':'stop'},
        {'kind':'contract_error','stage':'solve','detail':detail}]}
    value=diagnose(report)
    assert value['code']=='MODEL_CONTRACT_INVALID'
    assert value['paths']==['questions.Q2.computations.3.variables']
    assert 'PRIVATE' not in json.dumps(value)


@pytest.mark.parametrize('later_status',['completed','failed'])
def test_handled_errors_are_not_reused_as_current_failure(later_status):
    report={'status':later_status,'error':'browser failed','audit':[
        {'kind':'model','call':1,'stage':'solve','finish_reason':'stop'},
        {'kind':'model_output_error','call':1,'stage':'solve','position':2},
        {'kind':'contract_error','stage':'solve','detail':'format issue'},
        {'kind':'model','call':2,'stage':'solve','finish_reason':'stop'}]}
    value=diagnose(report)
    assert not value if later_status=='completed' else value['code']=='TASK_FAILED'
