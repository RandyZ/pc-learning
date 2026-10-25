"""Classic graph actions: orchestration only, no model protocol or storage client state."""
from __future__ import annotations
from langgraph.graph import END
from langgraph.runtime import Runtime
from math_agent.domain.errors import NeedsInput
from math_agent.rendering import RenderError
from math_agent.storage import write_json
from math_agent.observability.tracing import traced_node
from .context import ClassicContext
from math_agent.execution_limits import repair_allowed

@traced_node
def source(state, runtime: Runtime[ClassicContext]):
    runner = runtime.context.job
    runner.audit('graph_node', {'node': 'source'})
    return {'source': runner.extract(state.get('feedback')), 'observation': {}}

@traced_node
def solve(state, runtime: Runtime[ClassicContext]):
    runner = runtime.context.job
    runner.audit('graph_node', {'node': 'solve'})
    return {'candidate': runner.solve(state['source'], state.get('feedback'), state.get('candidate'))}

@traced_node
def math_repair(state, runtime: Runtime[ClassicContext]):
    runner = runtime.context.job
    runner.audit('graph_node', {'node': 'math_repair'})
    return {'candidate': runner.repair_math(state['source'], state['candidate'], state['feedback']), 'observation': {}}

@traced_node
def diagram(state, runtime: Runtime[ClassicContext]):
    runner = runtime.context.job
    runner.audit('graph_node', {'node': 'diagram'})
    ids = [q.id for q in state['source'].questions]
    targets = {i.get('target') for i in state['feedback']}
    allowed = {ids.index(q) for q in targets} if targets and targets <= set(ids) else None
    return {'candidate': runner.solve(state['source'], state['feedback'], state['candidate'], 'diagram', allowed_tabs=allowed)}

@traced_node
def observe(state, runtime: Runtime[ClassicContext]):
    runner = runtime.context.job
    runner.audit('graph_node', {'node': 'observe'})
    write_json(runner.directory / 'candidate.json', state['candidate'].model_dump())
    try:
        from math_agent.native_constructions import require
        require(state['source'], state['candidate'])
        observation = runner.observe(state['candidate'])
        return {'observation': observation, 'next': 'review'}
    except RenderError as exc:
        issues = getattr(exc, 'issues', None)
        return {'observation': {'status': 'fail', 'error': str(exc)}, 'next': 'repair', 'feedback': [{'stage': 'diagram', 'target': i['question_id'], 'detail': i['detail']} for i in issues] if issues else [{'stage': 'diagram', 'target': 'browser', 'detail': str(exc)}]}

@traced_node
def review(state, runtime: Runtime[ClassicContext]):
    runner = runtime.context.job
    runner.audit('graph_node', {'node': 'review'})
    value = runner.review(state['source'], state['candidate'], state['observation'])
    write_json(runner.directory / 'review.json', value.model_dump())
    if value.verdict == 'needs_input':
        raise NeedsInput('；'.join((i.detail for i in value.issues)) or '原题存在未确定信息')
    return {'review': value, 'next': END if value.verdict == 'pass' else 'repair', 'feedback': [i.model_dump() for i in value.issues]}

@traced_node
def repair(state, runtime: Runtime[ClassicContext]):
    runner = runtime.context.job
    count = state.get('repairs', 0)
    if not repair_allowed(runner.settings, count):
        raise ValueError('修复次数已用完，未发布网页。最后异议：' + str(state['feedback'])[:2200])
    stages = {i['stage'] for i in state['feedback']}
    next_stage = 'source' if 'source' in stages else 'math_repair' if 'math' in stages else 'diagram'
    runner.audit('repair', {'stage': next_stage, 'attempt': count + 1, 'feedback': state['feedback']})
    return {'repairs': count + 1, 'next': next_stage}
