import pytest
from debug.classic_studio import make_graph
from math_agent.provider import DeepSeek


def test_studio_topology_is_safe_without_a_key(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    monkeypatch.setattr(DeepSeek,'__init__',lambda *a,**kw:pytest.fail('topology may not create a client'))
    graph=make_graph()
    assert 'source' in graph.get_graph().nodes
    with pytest.raises(ValueError,match='allow_paid'):
        graph.invoke({'repairs':0,'feedback':[]})


@pytest.mark.parametrize('value',['../../etc/passwd','not-a-job','A'*32])
def test_studio_rejects_arbitrary_paths(value):
    graph=make_graph({'configurable':{'debug_job_id':value,'allow_paid_model_calls':True}})
    with pytest.raises(ValueError,match='debug_job_id'):
        graph.invoke({'repairs':0,'feedback':[]})
