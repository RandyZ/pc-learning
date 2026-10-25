"""Solving: behavior-preserving services; runtime dependencies are explicit arguments."""
from __future__ import annotations
import copy
from math_agent.contracts import Candidate, MathRepair, check_candidate
from math_agent.domain.presentation import canonical_page

def solve(job, source, feedback=None, previous=None, stage='solve', _whole=False, allowed_tabs=None):
    if job.settings.parallel_questions and stage == 'solve' and (not _whole):
        from math_agent.parallel_solve import solve
        return solve(job, source, feedback)
    if job.settings.compact_tools and stage == 'solve':
        return job.solve_compact(source, feedback)

    def validate(raw):
        from math_agent.compact_output import unpack
        if job.settings.compact_tools and stage == 'diagram':
            from math_agent.diagram_patch import apply_patch
            candidate = apply_patch(raw, previous, job.audit, allowed_tabs=allowed_tabs)
        else:
            candidate = unpack(raw, source) if job.settings.compact_tools and stage == 'solve' else Candidate.model_validate(raw)
        if stage == 'diagram' and candidate.solutions != previous.solutions:
            raise RuntimeError('构图修复试图改写数学解答，已拦截；需要回到解题阶段')
        canonical_page(candidate, source, job.audit)
        check_candidate(candidate, source)
        if stage == 'diagram':
            from math_agent.submission import inspect, SubmissionError
            issues = inspect(candidate, source)
            if issues:
                raise SubmissionError(issues, candidate)
        return candidate
    candidate = job.validated(stage, {'source': source.model_dump(), 'feedback': feedback or [], 'previous': previous.model_dump() if previous else None, **({'allowed_tabs': sorted(allowed_tabs)} if allowed_tabs is not None else {})}, validate)
    job.save_ledger(source, candidate)
    return candidate

def solve_compact(job, source, feedback=None):
    payload = {'source': source.model_dump(), 'feedback': feedback or []}
    raw = job.ask('solve', payload)
    from math_agent.question_plan import build_plan
    plan = build_plan(source, job.question_plan_metadata) if job.settings.parallel_questions else None
    return job.finish_compact(raw, source, feedback, require_basis=job.settings.parallel_questions, dependency_plan=plan)

def finish_compact(job, raw, source, feedback=None, require_basis=False, dependency_plan=None):
    """One common submission/recovery gate for whole and merged branch output."""
    from math_agent.submission import decode, SubmissionError, patch_schema, apply_patch, allowed_fields

    def decode_submission(value):
        return decode(value, source, job.audit, require_basis=require_basis, dependency_plan=dependency_plan)
    payload = {'source': source.model_dump(), 'feedback': feedback or []}
    try:
        candidate = decode_submission(raw)
    except ValueError as exc:
        job.audit('contract_error', {'stage': 'solve', 'attempt': 1, 'detail': str(exc)[:12000]})
        issues = exc.issues if isinstance(exc, SubmissionError) else None
        if issues:
            try:
                allowed = allowed_fields(issues)
            except ValueError:
                allowed = None
        else:
            allowed = None
        if allowed and exc.candidate is not None and all((i['stage'] == 'diagram' for i in issues)):
            from math_agent.diagram_patch import apply_patch as diagram_patch
            ids = [q.id for q in source.questions]
            tabs = {ids.index(q) for q in allowed}
            correction = job.ask('diagram', {'source': source.model_dump(), 'previous': exc.candidate.model_dump(), 'feedback': issues, 'allowed_tabs': sorted(tabs)})
            candidate = diagram_patch(correction, exc.candidate, job.audit, allowed_tabs=tabs)
            canonical_page(candidate, source, job.audit)
            from math_agent.submission import inspect
            remaining = inspect(candidate, source, require_basis=require_basis, dependency_plan=dependency_plan)
            if remaining:
                raise SubmissionError(remaining, candidate)
        elif allowed:
            correction = job.ask('solve', {**payload, 'submission_patch_schema': patch_schema(issues, source.model_dump(), require_basis=require_basis), 'structured_output_correction': {'issues': issues, 'rejected_output': raw, 'instruction': '只返回updates，按错误清单修改指定小问和字段；保留真实未知及异议，不改题设，不删除未授权内容。'}})
            candidate = decode_submission(apply_patch(correction, raw, issues))
        else:
            correction = job.ask('solve', {**payload, 'structured_output_correction': {'error': str(exc)[:12000], 'rejected_output': raw, 'instruction': '外层编号无法安全绑定；修正完整小问对象，保留原题和全部真实未知，不得删问。'}})
            candidate = decode_submission(correction)
        job.audit('submission_correction', {'status': 'local_contract_pass', 'scope': {q: sorted(fields) for q, fields in (allowed or {}).items()}, 'detail': '只纠正已报告范围；仍须真实渲染与独立整题复核。最多一次输出纠正，未增加调用预算。'})
    job.save_ledger(source, candidate)
    return candidate

def repair_math(job, source, previous, feedback):

    def validate(raw):
        if job.settings.compact_tools:
            from math_agent.math_patch import apply
            candidate, patch = apply(raw, previous, source)
            job.audit('math_patch', {'questions': list(patch.updates), 'reasons': {k: v.reason for k, v in patch.updates.items()}, 'detail': '仅应用声明小问的证明；其余证明和全部几何对象原样保留，仍需重新独立复核'})
        else:
            repair = MathRepair.model_validate(raw)
            candidate = Candidate(solutions=repair.solutions, spec=copy.deepcopy(previous.spec))
        canonical_page(candidate, source, job.audit)
        check_candidate(candidate, source)
        job.validate_parallel_candidate(source, candidate)
        return candidate
    candidate = job.validated('math_repair', {'source': source.model_dump(), 'previous': {'solutions': [s.model_dump() for s in previous.solutions]}, 'feedback': feedback}, validate)
    from math_agent.deferred_review import content_binding
    prior = getattr(job, 'last_review_context', None)
    job.repair_review_context = None
    if prior and prior['binding'] == content_binding(source, previous, job.request):
        unchanged = [a.question_id for a, b in zip(previous.solutions, candidate.solutions) if a == b]
        job.repair_review_context = {'current_binding': content_binding(source, candidate, job.request), 'previous_review': prior['review'], 'unchanged_questions': unchanged, 'changed_questions': [q.id for q in source.questions if q.id not in unchanged], 'detail': '服务端逐字数据比较；题设/原图/图形不变。原复核只是模型判断，不是形式化证明，新发现的真实问题仍须报告。'}
    job.pending_math_objections = copy.deepcopy(feedback)
    job.save_ledger(source, candidate)
    job.audit('math_repair', {'detail': '仅更新数学解答及派生讲解文字；原图保留，必须重新观察并独立复核，旧复核不复用'})
    return candidate
