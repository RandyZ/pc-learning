import ast
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import pytest
from math_agent.config import ROOT

def canonical_ast(value):
    # ast.dump's empty-field formatting changed in Python 3.13.
    if isinstance(value, ast.AST):
        return [type(value).__name__, sorted(
            (name, canonical_ast(item)) for name, item in ast.iter_fields(value)
            if item is not None and item != [])]
    if isinstance(value, list):
        return [canonical_ast(item) for item in value]
    return value


def body_digest(body):
    normalized = json.dumps(canonical_ast(ast.Module(body=body, type_ignores=[])),
                            ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()


BASELINE = json.loads(Path(__file__).with_name('baseline_bodies.json').read_text())

@pytest.mark.parametrize('name,spec', BASELINE['methods'].items())
def test_stage_body_is_ast_equivalent_to_fixed_baseline(name, spec):
    function = getattr(importlib.import_module(spec['module']), name)
    node = ast.parse(inspect.getsource(function)).body[0]
    assert body_digest(node.body) == spec['body_sha256']


def test_facade_has_no_hidden_legacy_runner():
    from math_agent.pipeline import Runner
    assert len(inspect.getsource(Runner).splitlines()) < 120
    assert not (ROOT/'math_agent/legacy_pipeline.py').exists()
    for name in ('ask','extract','solve','review','execute'):
        assert len(inspect.getsource(getattr(Runner,name)).splitlines()) <= 6


def test_topology_compiles_without_model_or_storage(monkeypatch):
    from math_agent.workflows.classic.graph import build_graph
    from math_agent.workflows.tool_loop.graph import build_graph as tool_graph
    from math_agent.provider import DeepSeek
    monkeypatch.setattr(DeepSeek,'__init__',lambda *a,**k:pytest.fail('compile cannot create a model'))
    graph=build_graph()
    assert set(graph.get_graph().nodes)=={'__start__','__end__','source','solve','math_repair','diagram','observe','review','repair'}
    assert set(tool_graph().get_graph().nodes)=={'__start__','__end__','model','tools','draft'}
    for model in (graph.get_input_jsonschema(), graph.get_output_jsonschema()):
        assert not {'provider','client','api_key','lock','job'} & set(model.get('properties',{}))
    with pytest.raises(ValueError):build_graph('unknown')


def test_compatibility_errors_keep_identity():
    from math_agent.pipeline import NeedsInput
    from math_agent.domain.errors import NeedsInput as domain_error
    assert NeedsInput is domain_error
