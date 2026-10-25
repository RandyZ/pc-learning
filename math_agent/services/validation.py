"""Validation: behavior-preserving services; runtime dependencies are explicit arguments."""
from __future__ import annotations
from math_agent.domain.presentation import compact_tool_evidence

def validated(job, stage, payload, validate, request_override=None):
    """Correct invalid structured output once, within the existing paid-call budget.

        Transport errors and truncation are deliberately outside the catch. Invalid
        evidence is never silently discarded, and only validated results advance.
        """
    context = dict(payload)
    for attempt in range(2):
        raw = job.ask(stage, context, request_override=request_override)
        try:
            return validate(raw)
        except ValueError as exc:
            job.audit('contract_error', {'stage': stage, 'attempt': attempt + 1, 'detail': str(exc)[:4000]})
            if attempt:
                raise
            context = {**payload, 'structured_output_correction': {'error': str(exc)[:24000], 'rejected_output': raw, 'instruction': '这是数学局部纠错输出：仅返回updates对象，按Q编号提交需更改小问的reason与完整proof；无关小问由服务端保留，不能返回solutions/spec/tabs或改题设。' if stage == 'math_repair' and job.settings.compact_tools else '这是数学纠错输出：仅返回完整solutions，不返回spec或tabs；保留真实异议，不能改题设或删小问。' if stage == 'math_repair' else '这是读题输出纠错：只修正本阶段source或source_review JSON，对照原题核对文字、条件和引用。不要输出solutions或tabs；保留真实疑问，不能为通过校验修改题意。' if stage in {'source', 'source_review'} else '这是复核/裁决输出纠错：修正完整的本阶段JSON，核对verdict与逐项核验、issues的一致性以及裁决编号/作用域。不要输出solutions或tabs；明确保留真实异议、未知项，不要为通过格式而编造或删除问题。' if stage in {'review', 'review_plan', 'graph_review', 'math_review', 'adjudicate', 'diagram_review', 'deferred_review'} else '按系统声明的DiagramPatch提交局部edits，不返回数学解答或整页字段。' if job.settings.compact_tools and stage == 'diagram' else '按系统声明的小问对象契约修正完整JSON；每问恰好一次，保留证明与真实异议。' if job.settings.compact_tools and stage == 'solve' else '修正完整JSON并重新提交。严格区分条件编号和小问编号，按source的小问数量与顺序组织solutions和tabs。不得删掉未决读题问题、异议或偷偷改变题设以通过校验。'}}
            if stage in {'review', 'diagram_review', 'graph_review', 'adjudicate', 'review_plan', 'math_review', 'deferred_review'}:
                fresh_evidence = compact_tool_evidence([r for r in job.records if r['kind'] == 'tool'][-24:])
                context['tool_evidence'] = fresh_evidence
                from math_agent.compact_review import ReviewShapeError
                if isinstance(exc, ReviewShapeError):
                    context['structured_output_correction']['final_output_only'] = True
                    context['structured_output_correction']['instruction'] += ' 这是纯编号覆盖纠正，本轮只输出修正后的完整JSON，不调用工具；若已有证据不足，明确保留unknown/异议，不能为凑编号编造通过。'
                context['structured_output_correction']['instruction'] += ' 已附上刚才真实执行的工具结果，保留这些证据；复用成功观察，只修正出错的格式/编号覆盖，不为格式纠正重复解题或重跑同一成功查询。若确有失败或未执行的必需检查才补做；真实异议和未知项仍须保留。'
    raise AssertionError('unreachable')
