import pytest
from benchmarks.profiles import overrides
from benchmarks.compare import summarize
from benchmarks.run import main
from benchmarks.evaluators.math_verify import compare


def test_profiles_never_override_model_strength_or_budget():
    for name in ('A1','B0','B1','B2'):
        assert not any(k.startswith('DEEPSEEK_') or k in {'AGENT_MAX_MODEL_CALLS','AGENT_JOB_TIMEOUT'} for k in overrides(name))
    assert overrides('B0')['AGENT_V2_PROFILE']=='serial'


def test_paid_run_requires_explicit_authorization(tmp_path):
    out=tmp_path/'must-not-exist'
    with pytest.raises(SystemExit):main(['--profile','B0','--case-id','x','--text','x','--out',str(out)])
    assert not out.exists()


def test_prepare_only_does_not_call_model(tmp_path,monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    # Keep environment mutation local to this test, like a standalone CLI process.
    for name in ('AGENT_DATA_DIR','REDIS_URL','AGENT_WORKFLOW',*overrides('B0')):monkeypatch.setenv(name,'')
    assert main(['--profile','B0','--case-id','x','--text','x','--out',str(tmp_path/'isolated'),'--prepare-only'])==0
    assert (tmp_path/'isolated/manifest.json').exists()
    assert not (tmp_path/'isolated/result.json').exists()


def test_failure_and_unreviewed_stay_in_denominator():
    base={'profile':'B0','case_id':'c','input_hash':'x','end_to_end':True,'model_calls':3,'end_to_end_seconds':100}
    good={**base,'status':'completed','quality':dict.fromkeys(('math','proof','diagram','interaction','coverage'),True)}
    unreviewed={**base,'status':'completed','quality':None}
    failure={**base,'status':'failed','quality':None}
    out=summarize([good,unreviewed,failure])['groups']['B0']
    assert out['confirmed_correct_within_180s_rate']==1/3
    assert out['quality_unreviewed']==2 and out['failed_or_incomplete']==1
    assert out['cost_per_correct_including_failed_attempts'] is None


def test_optional_evaluator_has_explicit_unparseable_status():
    assert compare('', '1')['status']=='unparseable'


def test_studio_schema_load_does_not_create_runner(monkeypatch):
    from debug import studio
    monkeypatch.setattr(studio,'Runner',lambda *a,**k:pytest.fail('schema load must not create a model client'))
    graph=studio.graph({})
    assert 'check_math' in graph.get_graph().nodes
    with pytest.raises(ValueError,match='requires'):
        studio.graph({'configurable':{'studio_task':'test','allow_paid_model_calls':False}})
