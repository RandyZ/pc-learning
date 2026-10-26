import copy
import subprocess
import pytest
from math_agent.domain.v2 import Draft
from math_agent.infrastructure.v2_math import evaluate
from math_agent.services.verification import ReceiptExecutor,backend


def op(**kwargs):
    return {'id':'o','op':'equivalent','target_step_id':'s','basis_ids':['C1'],
            'expression':'x/x','expected':'1','variables':{'x':'real'},**kwargs}


def test_domain_before_simplification_is_not_discarded():
    value=evaluate(op())
    assert value['verdict']=='unknown'
    assert value['result']['domain_restrictions']
    assert evaluate(op(variables={'x':'nonzero'}))['verdict']=='pass'
    with pytest.raises(ValueError):evaluate(op(op='substitute_check',substitutions={'x':0}))


@pytest.mark.parametrize('expression',["__import__('os')",'x.real','[x][0]','x**999','sqrt(-1)','1/0'])
def test_unsafe_or_undefined_expression_is_rejected(expression):
    with pytest.raises((ValueError,SyntaxError)):
        evaluate(op(expression=expression))


def test_open_interval_and_radical_conditions_are_explicit():
    d={'x':{'lower':0.,'upper':2.,'lower_inclusive':False}}
    assert evaluate(op(domains=d))['verdict']=='pass'
    assert evaluate(op(expression='sqrt(x)**2',expected='x'))['verdict']=='unknown'
    assert evaluate(op(expression='sqrt(x)**2',expected='x',variables={'x':'nonnegative'}))['verdict']=='pass'


def test_counterexample_stays_negative():
    result=evaluate(op(expression='x+1',expected='x'))
    assert result['execution_status']=='ok' and result['verdict']=='fail'


def test_endpoint_does_not_prove_extremum_or_evaluate_open_end():
    result=evaluate(op(op='endpoints',expected=None,expression='1/x',domains={
        'x':{'lower':0.,'upper':2.,'lower_inclusive':False}}))
    assert result['scope']=='calculation'
    assert result['result']['endpoints'][0]['status']=='not_evaluated'


def test_timeout_is_unknown_not_pass(monkeypatch):
    def timeout(*a,**kw):raise subprocess.TimeoutExpired('math',1)
    monkeypatch.setattr(subprocess,'run',timeout)
    assert backend([{'operation':op(),'inputs':{}}],1)[0]['verdict']=='unknown'


def test_exact_receipt_reuse_and_invalidation(draft_v2):
    runs=[];events=[]
    def run(requests,timeout):
        runs.append(requests)
        return [evaluate(r['operation'],r['inputs']) for r in requests]
    executor=ReceiptExecutor(lambda k,d:events.append((k,d)),lambda:None,lambda:100,reuse=True,execute=run)
    d=Draft.model_validate(draft_v2)
    a=executor.run('source','v1',d)
    b=executor.run('source','v2',d)
    assert len(runs)==1 and b['half_length']['reused_from']==a['half_length']['receipt_id']
    assert b['half_length']['candidate_revision']=='v2'
    executor.run('new-source','v3',d)
    assert len(runs)==2
    changed=d.model_copy(deep=True);changed.questions['Q1'].proof.result='changed'
    executor.run('new-source','v4',changed)
    assert len(runs)==3


def test_negative_receipts_can_be_reused_without_becoming_pass(draft_v2):
    draft_v2['questions']['Q1']['computations'][0].update(op='equivalent',expression='1',expected='2')
    d=Draft.model_validate(draft_v2);runs=[]
    def run(requests,timeout):
        runs.append(1);return [evaluate(r['operation'],r['inputs']) for r in requests]
    e=ReceiptExecutor(lambda *a:None,lambda:None,lambda:100,reuse=True,execute=run)
    e.run('s','r1',d);result=e.run('s','r2',d)
    assert len(runs)==1
    assert result['half_length']['verdict']=='fail' and result['half_length']['reused_from']


def test_stale_initial_receipt_cannot_join_new_revision(draft_v2):
    d=Draft.model_validate(draft_v2)
    e=ReceiptExecutor(lambda *a:None,lambda:None,lambda:100,execute=lambda r,timeout:[evaluate(x['operation']) for x in r])
    old=e.run('s','r1',d)
    with pytest.raises(ValueError,match='stale'):e.run('s','r2',d,initial=old)


def test_budget_expiry_prevents_execution(draft_v2):
    def check():raise TimeoutError('expired')
    e=ReceiptExecutor(lambda *a:None,check,lambda:0,execute=lambda *a,**k:pytest.fail('must not execute'))
    with pytest.raises(TimeoutError):e.run('s','r',Draft.model_validate(draft_v2))
