"""Answer-assisted LangGraph: verify mathematics once, freeze it, draw and review separately."""
from __future__ import annotations
from .execution_limits import repair_allowed, graph_limit
import copy
import hashlib
import json
from typing import TypedDict
from pydantic import Field
from langgraph.graph import StateGraph, START, END
from langsmith import tracing_context
from .contracts import Strict, Solution, Source, Candidate, Review, check_candidate, check_review
from .scene_tools import PreviewSpec, PreviewTab
from .storage import write_json
from .rendering import RenderError, strict_spec


class MathDraft(Strict):
    solutions: list[Solution] = Field(min_length=1,max_length=12)


class TabUpdate(PreviewTab):
    tab: int = Field(ge=0,le=11)


class DrawingPatch(Strict):
    updates: list[TabUpdate] = Field(min_length=1,max_length=12)


def math_hash(source, draft):
    return hashlib.sha256(json.dumps({'source':source.model_dump(),'math':draft.model_dump()},
        sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def validate_math(raw, source):
    draft=MathDraft.model_validate(raw)
    # Reuse all existing solution numbering, basis ordering and coverage checks.
    check_candidate(Candidate(solutions=draft.solutions,spec={'problem_text':source.problem_text,
        'tabs':[{} for _ in source.questions]}),source)
    return draft


def assemble(source, draft, drawings):
    from .pipeline import canonical_page
    if len(drawings.tabs)!=len(source.questions):raise ValueError('构图小问数量必须与原题相同')
    spec={'format':'jsxgraph-v1','title':source.title,'problem_text':source.problem_text,
        'tabs':[{'tab_title':f'第{i+1}问',**tab.model_dump(exclude_none=True)} for i,tab in enumerate(drawings.tabs)]}
    candidate=canonical_page(Candidate(solutions=copy.deepcopy(draft.solutions),spec=spec),source)
    check_candidate(candidate,source);strict_spec(candidate.spec)
    return candidate


def patch_drawing(source,draft,previous,raw):
    patch=DrawingPatch.model_validate(raw)
    ids=[u.tab for u in patch.updates]
    if len(ids)!=len(set(ids)) or any(i>=len(previous.spec['tabs']) for i in ids):
        raise ValueError('修图tab编号重复或不存在')
    tabs=[{'board':t['board'],**({'controls':t['controls']} if t.get('controls') else {})} for t in previous.spec['tabs']]
    for update in patch.updates:tabs[update.tab]=update.model_dump(exclude={'tab'},exclude_none=True)
    return assemble(source,draft,PreviewSpec.model_validate({'tabs':tabs}))


class State(TypedDict,total=False):
    source: Source
    math: MathDraft
    math_review: Review
    certificate: str
    candidate: Candidate
    review: Review
    observation: dict
    feedback: list[dict]
    repairs: int
    next: str


def execute_reference_graph(runner):
    from .pipeline import NeedsInput
    if not any(i['role']=='answer' for i in runner.request['images']):
        raise ValueError('参考答案精简模式需要答案图片；没有自动转入其他付费流程')
    def enter(name):runner.audit('graph_node',{'node':name,'workflow':'reference'})
    def source(state):
        enter('source');value=runner.extract(state.get('feedback'))
        explicit=[word for word in ('旋转','平移') if word in value.problem_text]
        text_route=value.topic_family=='unknown' and bool(explicit)
        runner.audit('reference_scope',{'classification':value.topic_family,'explicit_terms':explicit,
            'text_fallback':text_route,'note':'仅选择执行路线，不证明题设或解答正确'})
        if value.topic_family!='rotation_translation' and not text_route:
            raise ValueError('参考答案精简版目前仅验收旋转/平移几何题；不自动回退旧流程')
        return {'source':value,'certificate':'','math_review':None}
    def math(state):
        enter('reference_math')
        draft=runner.validated('reference_math',{'source':state['source'].model_dump(),
            'previous':state['math'].model_dump() if state.get('math') else None,'feedback':state.get('feedback',[])},
            lambda raw:validate_math(raw,state['source']))
        write_json(runner.directory/'math-draft.json',draft.model_dump())
        runner.save_ledger(state['source'],Candidate(solutions=draft.solutions,spec={}))
        return {'math':draft,'certificate':'','math_review':None}
    def check_math(state):
        enter('math_review')
        def validate(raw):
            value=Review.model_validate(raw);check_review(value,state['source'])
            if any(i.stage=='diagram' for i in value.issues):raise ValueError('数学复核尚未构图，不能输出diagram异议')
            return value
        from .proof_dependencies import review_dependencies
        review=runner.validated('math_review',{'source':state['source'].model_dump(),
            'math':state['math'].model_dump(),
            'proof_dependencies':review_dependencies(state['source'],state['math'].solutions)},validate)
        write_json(runner.directory/'math-review.json',review.model_dump())
        if review.verdict=='needs_input':raise NeedsInput('；'.join(i.detail for i in review.issues) or '数学条件仍不明确')
        certificate=math_hash(state['source'],state['math']) if review.verdict=='pass' else ''
        runner.audit('math_certificate',{'sha256':certificate,'verdict':review.verdict,'note':'模型核验记录，不是形式化证明'})
        return {'math_review':review,'certificate':certificate,'next':'draw' if certificate else 'repair',
            'feedback':[i.model_dump() for i in review.issues]}
    def ensure(state):
        if not state.get('certificate') or state['certificate']!=math_hash(state['source'],state['math']):
            raise ValueError('数学核验版本失效，必须重新核验后才能构图或发布')
    def draw(state):
        enter('draw');ensure(state)
        candidate=runner.validated('draw',{'source':state['source'].model_dump(),'math':state['math'].model_dump()},
            lambda raw:assemble(state['source'],state['math'],PreviewSpec.model_validate(raw)))
        runner.save_ledger(state['source'],candidate)
        return {'candidate':candidate}
    def repair_drawing(state):
        enter('draw_patch');ensure(state)
        previous={'tabs':[{'board':t['board'],**({'controls':t['controls']} if t.get('controls') else {})} for t in state['candidate'].spec['tabs']]}
        candidate=runner.validated('draw_patch',{'source':state['source'].model_dump(),'math':state['math'].model_dump(),
            'previous_drawing':previous,'feedback':state['feedback']},
            lambda raw:patch_drawing(state['source'],state['math'],state['candidate'],raw))
        if candidate.solutions!=state['candidate'].solutions:raise ValueError('修图不能改变数学解答')
        return {'candidate':candidate}
    def observe(state):
        enter('observe');ensure(state)
        if state['candidate'].solutions!=state['math'].solutions:raise ValueError('候选页数学解答不匹配已核验版本')
        write_json(runner.directory/'candidate.json',state['candidate'].model_dump())
        try:
            from .native_constructions import require
            require(state['source'],state['candidate'])
            return {'observation':runner.observe(state['candidate']),'next':'graph_review'}
        except RenderError as exc:
            issues=getattr(exc,'issues',None)
            return {'next':'repair','feedback':([{'stage':'diagram','target':i['question_id'],'detail':i['detail']} for i in issues]
                                               if issues else [{'stage':'diagram','target':'browser','detail':str(exc)}])}
    def check_graph(state):
        enter('graph_review');ensure(state)
        if state['candidate'].solutions!=state['math'].solutions:raise ValueError('候选页数学解答不匹配已核验版本')
        runner.math_certificate={'sha256':state['certificate'],'review':state['math_review'].model_dump()}
        value=runner.review(state['source'],state['candidate'],state['observation'],stage='graph_review')
        write_json(runner.directory/'graph-review.json',value.model_dump())
        if value.verdict=='needs_input':raise NeedsInput('；'.join(i.detail for i in value.issues) or '图形条件仍不明确')
        return {'review':value,'next':END if value.verdict=='pass' else 'repair','feedback':[i.model_dump() for i in value.issues]}
    def repair(state):
        count=state.get('repairs',0)
        if not repair_allowed(runner.settings,count):raise ValueError('修复预算用完，未发布网页：'+str(state['feedback'])[:1800])
        stages={i['stage'] for i in state['feedback']}
        dest='source' if 'source' in stages else 'reference_math' if 'math' in stages else 'draw_patch'
        runner.audit('repair',{'stage':dest,'attempt':count+1,'feedback':state['feedback']})
        return {'repairs':count+1,'next':dest}
    graph=StateGraph(State)
    for name,fn in [('source',source),('reference_math',math),('math_review',check_math),('draw',draw),
                    ('draw_patch',repair_drawing),('observe',observe),('graph_review',check_graph),('repair',repair)]:graph.add_node(name,fn)
    graph.add_edge(START,'source');graph.add_edge('source','reference_math');graph.add_edge('reference_math','math_review')
    graph.add_conditional_edges('math_review',lambda s:s['next'],{'draw':'draw','repair':'repair'})
    graph.add_edge('draw','observe');graph.add_edge('draw_patch','observe')
    graph.add_conditional_edges('observe',lambda s:s['next'],{'graph_review':'graph_review','repair':'repair'})
    graph.add_conditional_edges('graph_review',lambda s:s['next'],{'repair':'repair',END:END})
    graph.add_conditional_edges('repair',lambda s:s['next'],{n:n for n in ['source','reference_math','draw_patch']})
    compiled=graph.compile();(runner.directory/'agent-graph.mmd').write_text(compiled.get_graph().draw_mermaid())
    with tracing_context(enabled=False):return compiled.invoke({'repairs':0,'feedback':[]},{'recursion_limit':graph_limit(runner.settings,12+runner.settings.max_repairs*8),'callbacks':[]})
