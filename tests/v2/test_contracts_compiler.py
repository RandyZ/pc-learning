import copy
import pytest
from math_agent.domain.v2 import Draft,Operation,ChangeSet
from math_agent.contracts import Source
from math_agent.policies.v2 import validate_draft,apply_changes,prerequisite_ids,solutions
from math_agent.compilers.scene import compile_candidate,CompilationError


def test_stable_steps_translate_without_changing_proof(source_v2,draft_v2):
    d=Draft.model_validate(draft_v2);s=Source.model_validate(source_v2)
    validate_draft(s,d)
    assert solutions(s,d)[0].steps[0].basis_ids==['C1']
    candidate,mapping=compile_candidate(s,d,{})
    assert candidate.spec['tabs'][0]['conclusion']=='AM=1'
    assert mapping['objects']['Q1']['M']['plan_node']=='object:M'


@pytest.mark.parametrize('mutation', ['own_step','future_step','cycle','unknown_q','duplicate_op','missing_object'])
def test_invalid_plans_rejected(source_v2,draft_v2,mutation):
    q=draft_v2['questions']['Q1']
    if mutation=='own_step':q['computations'][0]['basis_ids']=['Q1:half']
    if mutation=='future_step':q['proof']['steps'][0]['basis_ids']=['Q1:later']
    if mutation=='cycle':q['computations'][0]['depends_on']=['half_length']
    if mutation=='unknown_q':draft_v2['questions']['Q2']=copy.deepcopy(q)
    if mutation=='duplicate_op':q['computations'].append(copy.deepcopy(q['computations'][0]))
    if mutation=='missing_object':q['scene']['required_visible'].append('Z')
    with pytest.raises(ValueError):
        d=Draft.model_validate(draft_v2);s=Source.model_validate(source_v2)
        validate_draft(s,d);compile_candidate(s,d,{})


@pytest.mark.parametrize('op,kwargs,native',[
    ('rotate_polygon',{'center':'O','direction':'counterclockwise','degrees':90},'rotation'),
    ('translate_polygon',{'vector':[1,2]},'translation'),
    ('reflect_group',{'mirror_line':'AB'},'reflection')])
def test_macros_generate_native_objects_and_visible_edges(source_v2,draft_v2,op,kwargs,native):
    scene=draft_v2['questions']['Q1']['scene']
    scene['objects'] += [{'id':'C','kind':'point','coords':[0,2],'label':'C'},
                         {'id':'O','kind':'point','coords':[0,0],'label':'O'}]
    scene['constructions']=[{'id':'tr','op':op,'vertices':['A','B','C'], 'output_vertices':['A1','B1','C1'],
                             'basis_ids':['C1'],**kwargs}]
    candidate,m=compile_candidate(Source.model_validate(source_v2),Draft.model_validate(draft_v2),{})
    objs={x['id']:x for x in candidate.spec['tabs'][0]['board']['objects']}
    assert objs['A1']['kind']==native and objs['A1']['parents'][0]=='A'
    assert len([x for x in objs.values() if x['kind']=='segment'])==6
    assert m['objects']['Q1']['A1']['plan_node']=='construction:tr'


def test_generated_id_collision_is_not_silently_replaced(source_v2,draft_v2):
    draft_v2['questions']['Q1']['scene']['constructions']=[{'id':'tr','op':'translate_polygon',
        'vertices':['A'],'output_vertices':['M'],'basis_ids':['C1'],'vector':[0,1],'connect':False}]
    with pytest.raises(CompilationError,match='collision'):
        compile_candidate(Source.model_validate(source_v2),Draft.model_validate(draft_v2),{})


def test_scene_dependency_is_explicit_and_failure_cannot_supply_coordinates(source_v2,draft_v2):
    draft_v2['questions']['Q1']['scene']['objects'][1]['coords']=['@half_length',0]
    d=Draft.model_validate(draft_v2)
    assert prerequisite_ids(d)=={'half_length'}
    with pytest.raises(ValueError,match='completed'):
        compile_candidate(Source.model_validate(source_v2),d,{})
    c,_=compile_candidate(Source.model_validate(source_v2),d,{'half_length':{
        'execution_status':'ok','result':{'scalar':True,'exact':'1'}}})
    assert c.spec['tabs'][0]['board']['objects'][1]['coords']==['1',0]


def test_patch_scope_revision_and_obligation_retention(source_v2,draft_v2):
    s=Source.model_validate(source_v2);d=Draft.model_validate(draft_v2)
    raw={'expected_candidate_revision':'a'*64,'reason':'correct','updates':{'Q1':{'proof':d.questions['Q1'].proof.model_dump()}}}
    with pytest.raises(ValueError,match='stale'):
        apply_changes(s,d,ChangeSet.model_validate(raw),'b'*64,[{'stage':'math','target':'Q1'}])
    with pytest.raises(ValueError,match='scope'):
        apply_changes(s,d,ChangeSet.model_validate(raw),'a'*64,[{'stage':'diagram','target':'Q1'}])
    raw['updates']={'Q1':{'computations':[]}}
    with pytest.raises(ValueError,match='drop'):
        apply_changes(s,d,ChangeSet.model_validate(raw),'a'*64,[{'stage':'math','target':'Q1'}])


def test_changed_proof_invalidates_transitive_dependents(source_v2,draft_v2):
    source_v2['questions'].append({'id':'Q2','text':'使用上一问'})
    q=copy.deepcopy(draft_v2['questions']['Q1']);q['computations']=[]
    q['proof']['steps'][0]['basis_ids']=['Q1:half']
    draft_v2['questions']['Q2']=q
    d=Draft.model_validate(draft_v2)
    proof=d.questions['Q1'].proof.model_dump();proof['steps'][0]['conclusion']='AM=1，已补充依据'
    change=ChangeSet(expected_candidate_revision='a'*64,reason='clarify',updates={'Q1':{'proof':proof}})
    _,receipt=apply_changes(Source.model_validate(source_v2),d,change,'a'*64,[{'stage':'math','target':'Q1'}])
    assert receipt['affected_questions']==['Q1','Q2']


def test_function_moving_point_has_explicit_domain_and_slider(source_v2,draft_v2):
    scene=draft_v2['questions']['Q1']['scene']
    scene['controls']={'slider':{'min':0.,'max':2.,'default':1.}}
    scene['constructions']=[{'id':'f','op':'function_with_moving_point','point_id':'P',
        'expression':'x^2','domain':[0.,2.],'basis_ids':['C1']}]
    candidate,_=compile_candidate(Source.model_validate(source_v2),Draft.model_validate(draft_v2),{})
    objects={o['id']:o for o in candidate.spec['tabs'][0]['board']['objects']}
    assert objects['P']['coords']==['t','t ** 2']
    scene['controls']['slider']['max']=3.
    with pytest.raises(CompilationError,match='domain'):
        compile_candidate(Source.model_validate(source_v2),Draft.model_validate(draft_v2),{})


def test_real_browser_compiled_rotation_coordinates(tmp_path,source_v2,draft_v2):
    import os,json
    if os.getenv('MATH_AGENT_BROWSER_TEST')!='1':pytest.skip('requires actual browser')
    from math_agent.rendering import render
    scene=draft_v2['questions']['Q1']['scene']
    scene['constructions']=[{'id':'rot','op':'rotate_polygon','vertices':['B'],'output_vertices':['B1'],
        'center':'A','degrees':90,'direction':'counterclockwise','connect':False,'basis_ids':['C1']}]
    candidate,_=compile_candidate(Source.model_validate(source_v2),Draft.model_validate(draft_v2),{})
    render(candidate.spec,tmp_path)
    result=json.loads((tmp_path/'export-check.json').read_text())
    point=result['samples'][0]['objects']['B1']
    assert point['x']==pytest.approx(0,abs=1e-8) and point['y']==pytest.approx(2)
