"""Reduced regressions from a real failed run; no private logs or paid requests."""
import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from math_agent.contracts import Source
from math_agent.domain.v2 import ChangeSet, Draft, Operation
from math_agent.infrastructure.v2_math import evaluate
from math_agent.policies.v2 import (
    apply_changes, normalize_step_references, question_dependencies,
    solutions, validate_draft,
)


def operation(**updates):
    return {"id": "o1", "op": "evaluate", "target_step_id": "later",
            "basis_ids": ["C1"], "expression": "1/(-1)", **updates}


def sample():
    source = Source.model_validate({"title": "常量计算", "problem_text": "计算常量并核验结果。",
        "conditions": [{"id": "C1", "text": "计算常量", "origin": "question_text", "image_ids": []}],
        "questions": [{"id": "Q1", "text": "计算常量"}], "reference_summary": "", "uncertainties": []})
    raw = {"questions": {"Q1": {
        "proof": {"result": "结果为负一", "condition_ids": ["C1"], "cases": [],
            "domain_and_boundaries": "分母非零", "steps": [
                {"id": "earlier", "premise": "分母为负一", "rule": "实数运算", "conclusion": "分母不为零", "basis_ids": ["C1"]},
                {"id": "later", "premise": "分母非零", "rule": "除法", "conclusion": "结果为负一", "basis_ids": ["earlier"]}]},
        "computations": [operation(expected="-1", basis_ids=["earlier"])],
        "scene": {"objects": [{"id": "A", "kind": "point", "coords": [0, 0], "label": "A"}],
                  "required_visible": ["A"], "step_links": {"later": ["A"]}}}}}
    return source, raw


@pytest.mark.parametrize('expr,expected', [('1/(-1)', '-1'), ('-1+4','3'), ('-(3)','-3'), ('3/3','1'), ('3-4','-1')])
@pytest.mark.parametrize('with_expected', [False, True])
def test_constants_need_no_fake_substitution(expr, expected, with_expected):
    op = operation(expression=expr, **({'expected': expected} if with_expected else {}))
    result = evaluate(op)
    assert result['execution_status'] == 'ok' and result['verdict'] == 'pass'
    assert result['result']['exact'] == expected  # Not the assertion's zero residual.
    assert result['result']['scalar'] is True
    if with_expected:
        assert result['result']['expected'] == expected and result['result']['difference'] == '0'


@pytest.mark.parametrize('kind', ['simplify', 'evaluate'])
def test_incorrect_expected_remains_a_blocking_assertion(kind):
    result = evaluate(operation(op=kind, expected='99'))
    assert result['verdict'] == 'fail'
    assert result['result']['exact'] == '-1' and result['result']['expected'] == '99'
    assert result['result']['scalar'] is False
    assert result['scope'] == 'symbolic_under_assumptions'
    from math_agent.compilers.scene import _resolve
    with pytest.raises(ValueError, match='finite real scalar'):
        _resolve({'coords': ['@o1', 0]}, {'o1': result})


@pytest.mark.parametrize('expr,expected,variables', [
    ('(m^2+2*m)/m', 'm+2', {'m': 'nonzero'}),
    ('(m+2)*(m+6)', 'm^2+8*m+12', {'m': 'nonzero'}),
    ('(x-6)^2+8*(x-6)+12', 'x^2-4*x', {'x': 'real'}),
    ('x^2+2*x-(-x/2+p)', 'x^2+5/2*x-p', {'x':'real','p':'real'}),
    ('x^2-4*x-(-x/2+p)', 'x^2-7/2*x-p', {'x':'real','p':'real'}),
])
def test_symbolic_expected_is_checked_not_removed(expr, expected, variables):
    result = evaluate(operation(op='simplify', expression=expr, expected=expected, variables=variables))
    assert result['verdict'] == 'pass' and result['result']['difference'] == '0'


def test_domain_unknown_does_not_become_success():
    result = evaluate(operation(op='simplify', expression='x/x', expected='1', variables={'x':'real'}))
    assert result['verdict'] == 'unknown' and result['result']['domain_restrictions']
    assert not result['result']['scalar']


@pytest.mark.parametrize('expr', ['1/0', 'sqrt(-1)', "__import__('os')", 'unknown-unknown'])
def test_constants_still_pass_through_the_safe_parser(expr):
    with pytest.raises(ValueError):
        evaluate(operation(expression=expr))


@pytest.mark.parametrize('expected', ['sqrt(-1)', '1/0', 'unknown'])
def test_bad_expected_is_not_ignored(expected):
    with pytest.raises(ValueError):
        evaluate(operation(expected=expected))


def test_evaluate_requires_variable_bindings_but_accepts_input_refs():
    with pytest.raises(ValueError, match='every declared variable'):
        Operation.model_validate(operation(expression='x+1', variables={'x':'real'}))
    result = evaluate(operation(expression='x+1', variables={'x':'real'}, substitutions={'x':2}, expected='3'))
    assert result['result']['exact'] == '3' and result['verdict'] == 'pass'
    result = evaluate(operation(expression='a+1', input_refs={'a':'up'}, depends_on=['up']), {'a':'2'})
    assert result['result']['exact'] == '3'


def test_other_operation_contracts_remain_strict():
    with pytest.raises(ValueError, match='explicit substitutions'):
        Operation.model_validate(operation(op='substitute_check', expected='-1'))
    with pytest.raises(ValueError, match='require expected'):
        Operation.model_validate(operation(op='equivalent'))
    with pytest.raises(ValueError, match='not supported'):
        Operation.model_validate(operation(op='solve', expression=None, equations=['x=1'], variables={'x':'real'}, expected='1'))
    with pytest.raises(ValueError, match='not accepted'):
        Operation.model_validate(operation(op='simplify', variables={'x':'real'}, substitutions={'x':1}))


def test_unique_refs_normalize_without_mutation_then_compile():
    from math_agent.compilers.scene import compile_candidate
    source, raw = sample()
    parsed = Draft.model_validate(raw)
    saved = parsed.model_dump()
    draft, changes = normalize_step_references(source, parsed)
    assert parsed.model_dump() == saved
    assert len(changes) == 2
    validate_draft(source, draft)
    assert draft.questions['Q1'].computations[0].basis_ids == ['Q1:earlier']
    assert solutions(source, draft)[0].steps[1].basis_ids == ['Q1:S1']
    assert question_dependencies(draft) == {'Q1':set()}
    candidate, _ = compile_candidate(source, draft, {})
    assert len(candidate.spec['tabs']) == 1
    again, changes = normalize_step_references(source, draft)
    assert not changes and again == draft


@pytest.mark.parametrize('ref', ['later', 'unknown'])
def test_self_and_unknown_refs_still_fail(ref):
    source, raw = sample()
    raw['questions']['Q1']['computations'][0]['basis_ids'] = [ref]
    draft, _ = normalize_step_references(source, Draft.model_validate(raw))
    with pytest.raises(ValueError, match='assume its own target'):
        validate_draft(source, draft)


def test_future_reference_is_not_accepted_by_qualification():
    source, raw = sample()
    raw['questions']['Q1']['proof']['steps'][0]['basis_ids'] = ['later']
    draft, _ = normalize_step_references(source, Draft.model_validate(raw))
    with pytest.raises(ValueError, match='future, self or unknown'):
        validate_draft(source, draft)


def test_ambiguous_bare_reference_is_not_guessed():
    source, raw = sample()
    source = source.model_copy(update={'questions':source.questions + [source.questions[0].model_copy(update={'id':'Q2'})]})
    raw['questions']['Q2'] = copy.deepcopy(raw['questions']['Q1'])
    raw['questions']['Q2']['computations'] = []
    draft, changes = normalize_step_references(source, Draft.model_validate(raw))
    assert not changes
    with pytest.raises(ValueError):
        validate_draft(source, draft)


def test_unique_cross_question_ref_updates_dependency_graph():
    source, raw = sample()
    source = source.model_copy(update={'questions':source.questions + [source.questions[0].model_copy(update={'id':'Q2'})]})
    q2 = copy.deepcopy(raw['questions']['Q1'])
    q2['proof']['steps'] = [{**q2['proof']['steps'][1], 'id':'last', 'basis_ids':['later']}]
    q2['computations'] = []
    q2['scene']['step_links'] = {'last':['A']}
    raw['questions']['Q2'] = q2
    draft, _ = normalize_step_references(source, Draft.model_validate(raw))
    validate_draft(source, draft)
    assert question_dependencies(draft)['Q2'] == {'Q1'}


@pytest.mark.parametrize('kind', ['simplify','evaluate'])
def test_patch_cannot_drop_new_assertions(kind):
    source, raw = sample()
    raw['questions']['Q1']['computations'][0]['op'] = kind
    draft, _ = normalize_step_references(source, Draft.model_validate(raw))
    op = draft.questions['Q1'].computations[0].model_dump()
    op['expected'] = None
    change = ChangeSet(expected_candidate_revision='a'*64, reason='incorrectly remove assertion',
        updates={'Q1':{'computations':[op]}})
    with pytest.raises(ValueError, match='downgraded'):
        apply_changes(source, draft, change, 'a'*64, [{'stage':'math','target':'Q1'}])


def test_plan_solver_does_not_spend_a_correction_call_on_valid_plan():
    from math_agent.services.solving import PlanSolver
    source, raw = sample()
    before = copy.deepcopy(raw)
    calls, audit = [], []
    class SavedPlanSolver(PlanSolver):
        def _request(self, *args, **kwargs):
            calls.append(kwargs)
            assert len(calls) == 1, 'valid plan must not trigger a second model-equivalent call'
            return raw
    solver = SavedPlanSolver(SimpleNamespace(audit=lambda k,d:audit.append((k,d))), lambda:None)
    draft = solver.propose(source)
    validate_draft(source, draft)
    assert raw == before
    assert draft.questions['Q1'].computations[0].expected == '-1'
    assert audit[0][0] == 'reference_normalization'
    assert audit[0][1]['before_sha256'] != audit[0][1]['after_sha256']
    assert not any(kind == 'contract_error' for kind,_ in audit)


def test_bounded_worker_exercises_real_math_and_negative_assertion():
    requests = [{'operation':operation(expected='-1')}, {'operation':operation(expected='2')}]
    proc = subprocess.run([sys.executable, '-m', 'math_agent.infrastructure.v2_math', '8', '512'],
        input=json.dumps(requests), capture_output=True, text=True, timeout=10,
        cwd=Path(__file__).resolve().parents[2])
    assert proc.returncode == 0, proc.stderr
    values = json.loads(proc.stdout)
    assert [v['verdict'] for v in values] == ['pass', 'fail']
    assert values[0]['result']['exact'] == '-1' and not values[1]['result']['scalar']


def test_model_schema_describes_supported_operation_shapes():
    schema = Operation.model_json_schema()
    assert 'optional for simplify/evaluate' in schema['properties']['expected']['description']
    assert 'constant expressions' in schema['properties']['substitutions']['description']
