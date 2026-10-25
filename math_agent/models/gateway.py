"""Gateway: behavior-preserving services; runtime dependencies are explicit arguments."""
from __future__ import annotations
import base64
from math_agent.prompts import messages
from math_agent.storage import write_json
from math_agent.tools import DEFINITIONS
from math_agent.scene_tools import PREVIEW, INSPECT
from math_agent.evidence import READ_EVIDENCE, RECORD_HYPOTHESIS
from math_agent.image_tools import VIEW_REGION

def ask(job, stage, payload=None, request_override=None):
    job.store.event(job.job_id, stage, {'source': '识别题设与图片来源', 'source_review': '独立核对读题疑问', 'solve': '解题与工具核验', 'math_repair': '修正数学证明', 'diagram': '修复构图与交互', 'review': '对照原图独立复核', 'review_plan': '集中收集复核证据', 'adjudicate': '审查失败断言的题设依据', 'reference_math': '整理参考解法', 'math_review': '核验数学解答', 'draw': '生成原生几何图', 'draw_patch': '局部修图', 'graph_review': '核验图文关系', 'diagram_review': '复用已审数学，核验变化画板', 'deferred_review': '核对修订后仍待处理的检查'}[stage])
    active_request = job.request if request_override is None else request_override
    if stage in {'source', 'source_review'}:
        active_request = {**active_request, 'images': [i for i in active_request['images'] if i['role'] == 'question'], 'ocr_evidence': [p for p in active_request.get('ocr_evidence', []) if p['role'] == 'question']}
    if stage in {'draw', 'draw_patch', 'graph_review'}:
        active_request = {**active_request, 'images': [i for i in active_request['images'] if i['role'] == 'question']}
    image_definitions = [VIEW_REGION] if active_request['images'] else []
    definitions = image_definitions if stage in {'source', 'source_review'} else DEFINITIONS + [READ_EVIDENCE] + image_definitions + ([INSPECT] if stage in {'review', 'review_plan', 'adjudicate', 'graph_review', 'diagram_review', 'deferred_review'} else [PREVIEW, RECORD_HYPOTHESIS])
    if stage in {'reference_math', 'math_review', 'math_repair'}:
        definitions = [d for d in DEFINITIONS if d['function']['name'] in {'algebra_batch', 'symbolic_geometry'}] + image_definitions + [READ_EVIDENCE]
    elif stage in {'draw', 'draw_patch'}:
        definitions = [PREVIEW, READ_EVIDENCE] + image_definitions
    elif stage in {'graph_review', 'diagram_review', 'deferred_review'}:
        definitions = [INSPECT, READ_EVIDENCE] + image_definitions
    if stage == 'diagram_review':
        from math_agent.review_reuse import query_tool
        definitions = [query_tool(payload['reuse_certificate']) if d['function']['name'] == 'inspect_scene' else d for d in definitions]
    if stage == 'review':
        from math_agent.rotation_probe import TOOL as ROTATION_PROBE
        definitions = definitions + [ROTATION_PROBE]
    from math_agent.scene_tools import inspection_definition
    definitions = [inspection_definition(d, query_only=stage in {'adjudicate', 'review_plan', 'deferred_review'}) if d['function']['name'] == 'inspect_scene' else d for d in definitions]
    correction = (payload or {}).get('structured_output_correction', {})
    if correction.get('final_output_only'):
        definitions = []
        job.audit('format_only_correction', {'stage': stage, 'detail': '仅修正原编号覆盖，使用已附真实证据，不追加工具往返'})
    if job.settings.compact_tools and stage == 'solve':
        definitions = [d for d in definitions if d['function']['name'] in {'algebra_batch', 'symbolic_geometry', 'view_image_region'}]
    if stage in {'review', 'graph_review'} and job.review_candidate is not None:
        from math_agent.evidence import assertion_basis_index
        payload = {**payload, 'assertion_basis_index': assertion_basis_index(job.ledger, job.review_candidate.solutions)}
    if stage in {'review', 'review_plan', 'adjudicate', 'graph_review', 'diagram_review', 'deferred_review'} and job.review_candidate is not None:
        from math_agent.jsxgraph.spec import POINTS
        payload = {**payload, 'inspection_point_index': {str(i): {'question_id': s.question_id, 'point_ids': [o['id'] for o in job.review_candidate.spec['tabs'][i]['board']['objects'] if o['kind'] in POINTS]} for i, s in enumerate(job.review_candidate.solutions)}}
    solve_contract = None
    if stage == 'solve' and job.settings.compact_tools:
        from math_agent.compact_output import output_schema
        solve_contract = payload.get('submission_patch_schema') or output_schema(payload['source'], require_basis=job.settings.parallel_questions)
    prompt = messages(stage, active_request, job.directory, payload, compact=job.settings.compact_tools, solve_schema=solve_contract)
    if stage in {'source', 'source_review'} and job.settings.native_constructions:
        from math_agent.native_constructions import INSTRUCTION
        prompt[0]['content'] += '\n' + INSTRUCTION
    if stage == 'source' and job.settings.parallel_questions:
        from math_agent.parallel_solve import SOURCE_PLAN_INSTRUCTION
        prompt[0]['content'] += '\n' + SOURCE_PLAN_INSTRUCTION
    if job.settings.compact_tools and stage == 'solve':
        prompt[0]['content'] += '\n本阶段工具以实际声明为准：仅可批量数学计算与局部原图读取，不调用预览或重复证据读取。source已完整提供。构图只在最终小问对象中输出一次，不先写整套图形作为工具参数。提交后服务器会实际渲染JSXGraph并独立复核；不能宣称尚未执行的浏览器检查已通过。采用参考解法并补齐必要依据，避免反复探索多个方案；关键计算尽量一次批量核验。'
    if job.settings.review_reserve_seconds and stage in {'solve', 'review', 'math_repair', 'diagram'}:
        prompt[0]['content'] += '\n本任务按阶段预留时间：尽快选择一种可核验的初中解法，不反复探索替代方法。关键计算合并一批，坐标和代数使用精确根式，避免浮点误差触发重算。完整证明优先，精简重复措辞；不得省略依据、边界或未决异议来凑预算。'
    if stage in {'review', 'review_plan', 'adjudicate', 'graph_review', 'diagram_review', 'deferred_review'}:
        shots = sorted((job.directory / 'output').glob('tab-*.png'))
        if stage == 'diagram_review':
            allowed = {f'tab-{i:02d}' for i in payload['reuse_certificate']['changed_tabs']}
            shots = [p for p in shots if p.stem in allowed]
        for path in shots if stage == 'review' and payload.get('parallel_review_scope') else shots[:3]:
            prompt[-1]['content'].extend([{'type': 'text', 'text': '实际JSXGraph截图：' + path.stem}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode()}}])
    if stage == 'review_plan':
        definitions = []
    tool_dispatch = lambda name, args: job.dispatch_stage_tool(stage, active_request, name, args)
    schema_args = {}
    if job.settings.solve_responses and stage in {'solve', 'diagram'}:
        from math_agent.compact_output import output_schema, native_schema
        from math_agent.diagram_patch import DiagramPatch
        schema_args['output_schema'] = native_schema(DiagramPatch.model_json_schema()) if stage == 'diagram' else solve_contract or output_schema(payload['source'])
        if stage == 'diagram' and payload.get('allowed_tabs') is not None:
            schema_args['output_schema']['$defs']['Edit']['properties']['tab']['enum'] = payload['allowed_tabs']
    if stage in {'review', 'deferred_review', 'math_repair'} and job.settings.review_responses:
        from math_agent.compact_review import output_schema
        if stage == 'review':
            schema_args['output_schema'] = output_schema(payload.get('review_output_source', payload['source']), payload.get('literal_passages'), flat=payload.get('flat_review', False))
        elif stage == 'math_repair':
            from math_agent.math_patch import output_schema as patch_schema
            schema_args['output_schema'] = patch_schema(payload['source'])
        else:
            from math_agent.deferred_review import DeferredVerdict
            schema_args['output_schema'] = DeferredVerdict.model_json_schema()
    from math_agent.submission import receive
    value = receive(job.provider, stage, prompt, job.audit, tools=definitions, dispatch=tool_dispatch, **schema_args)
    write_json(job.directory / f'model-{job.provider.calls:02d}-{stage}.json', value)
    return value
