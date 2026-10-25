"""Reviewing: behavior-preserving services; runtime dependencies are explicit arguments."""
from __future__ import annotations
import copy
import json
from math_agent.contracts import Review, ReviewPreparation, check_review
from math_agent.storage import write_json
from math_agent.tools import DEFINITIONS
from math_agent.contracts import exact_ids
from math_agent.domain.presentation import review_payload
from math_agent.domain.errors import NeedsInput

def review(job, source, candidate, observation, stage='review'):
    job.complete_ocr()
    from math_agent.review_reuse import Snapshot
    delta = None
    if job.settings.incremental_review and stage == 'review' and job.review_snapshot:
        delta = job.review_snapshot.delta(source, candidate, job.request, job.directory)
    if job.settings.incremental_review and stage == 'review' and (delta is None):
        if job.review_snapshot:
            job.audit('review_reuse_invalidated', {'detail': '内容或作用域不匹配；重新完整复核，不沿用旧证书链'})
        job.review_snapshot = None
        job.review_certificates = []
    actual_stage = 'diagram_review' if delta else stage
    if delta:
        job.audit('review_reuse', {'certificate': delta['certificate'], 'changed_tabs': delta['changed_tabs'], 'detail': '原图/题设/数学一致且上次全部数学项pass；独立复核全部变化画板及原异议'})
    math_scope = None
    if job.settings.incremental_math_review and stage == 'review' and (not delta):
        from math_agent.math_review_scope import plan
        math_scope = plan(getattr(job, 'math_review_baseline', None), source, candidate, job.request, job.directory)
        if math_scope:
            job.audit('math_review_scope', {'sha256': math_scope['sha256'], 'question_ids': math_scope['question_ids'], 'reused_questions': math_scope['reused_questions'], 'detail': '串行复核修改问及依赖；全部条件、原图、画板、真实工具与暂存裁决保持'})
    value = job._review(source, candidate, observation, actual_stage, delta, math_scope)
    substantive = any((i.stage in {'source', 'math'} for i in value.issues))
    if job.deferred_reviews.pending and value.verdict != 'needs_input' and (not substantive):
        value = job.settle_deferred(source, candidate, observation, value)
    if job.settings.incremental_review and stage == 'review':
        try:
            job.review_snapshot = Snapshot(source, candidate, value, observation, job.request, job.directory)
            record = {'sha256': job.review_snapshot.certificate, 'binding': job.review_snapshot.binding, 'review': value.model_dump()}
            job.review_certificates.append(record)
            write_json(job.directory / 'review-certificates.json', job.review_certificates)
        except ValueError:
            job.review_snapshot = None
    from math_agent.deferred_review import content_binding
    if job.settings.incremental_math_review and stage == 'review':
        from math_agent.math_review_scope import capture
        job.math_review_baseline = capture(source, candidate, value, job.request, job.directory)
    job.last_review_context = {'binding': content_binding(source, candidate, job.request), 'review': value.model_dump()}
    return value

def _review(job, source, candidate, observation, stage='review', delta=None, math_scope=None):
    job.review_change_tabs = delta['changed_tabs'] if delta else None
    job.active_review_delta = delta
    job.scenes.current = copy.deepcopy(candidate.spec)
    job.review_candidate = candidate
    job.save_ledger(source, candidate)
    evidence = [r for r in job.records if r['kind'] == 'tool'][-24:]
    before = len(job.records)
    preparation = None
    from math_agent.proof_guards import collect as collect_proof_guards
    guards = collect_proof_guards(source, candidate, job.directory / 'proof-guards')
    write_json(job.directory / 'proof-guards.json', guards)
    if guards['observations'] or guards['unobserved']:
        job.audit('proof_case_observation', guards)
    if job.settings.review_plan and stage == 'review':

        def validate_preparation(raw):
            value = ReviewPreparation.model_validate(raw)
            exact_ids(value.covered_questions, [q.id for q in source.questions], '复核取证小问')
            from jsonschema import validate as schema_validate, ValidationError

            def validate_schema(instance, schema):
                try:
                    schema_validate(instance, schema)
                except ValidationError as exc:
                    raise ValueError(exc.message) from exc
            from math_agent.scene_tools import PROBES
            if value.probes:
                validate_schema(value.probes, PROBES)
            if any(('expected' in c for p in value.probes for c in p['checks'])):
                raise ValueError('取证计划只查询关系，不能填写expected')
            if value.algebra_operations:
                algebra = next((d for d in DEFINITIONS if d['function']['name'] == 'algebra_batch'))
                validate_schema({'operations': value.algebra_operations}, algebra['function']['parameters'])
            return value
        compact, _ = review_payload(candidate, [])
        preparation = job.validated('review_plan', {'source': source.model_dump(), 'candidate': compact, 'browser_observation': observation, 'repair_objections': getattr(job, 'pending_math_objections', [])}, validate_preparation).model_dump()
        write_json(job.directory / f'review-preparation-{job.observation_count:02d}.json', preparation)
        for name, args in ([('inspect_scene', {'probes': preparation['probes']})] if preparation['probes'] else []) + ([('algebra_batch', {'operations': preparation['algebra_operations']})] if preparation['algebra_operations'] else []):
            try:
                result = job.dispatch_stage_tool('review_plan', job.request, name, args)
            except (ValueError, TypeError, KeyError) as exc:
                result = {'ok': False, 'error': str(exc)[:800]}
            job.audit('tool', {'stage': 'review_plan', 'name': name, 'arguments': json.dumps(args, ensure_ascii=False), 'result': result})
        evidence = [r for r in job.records if r['kind'] == 'tool'][-24:]
    scope_raw = {}

    def validate(raw):
        from math_agent.compact_review import unpack as unpack_review
        from math_agent.math_review_scope import merge as merge_math_scope
        review = merge_math_scope(raw, math_scope, source) if math_scope else job.review_snapshot.merge(raw, delta, source) if delta else unpack_review(raw, source) if stage == 'review' and job.settings.compact_tools else Review.model_validate(raw)
        if review.verdict == 'pass' and review.issues:
            review.verdict = 'revise'
            job.audit('review_consistency', {'stage': 'review', 'detail': '保留全部异议并降级为修复，不放行'})
        check_review(review, source)
        from math_agent.inspection_recovery import reconcile, partition_pending
        from math_agent.scene_tools import fingerprint
        from math_agent.rendering import strict_spec
        inspections = [r for r in job.records[before:] if r['kind'] == 'tool' and r.get('name') == 'inspect_scene']
        pending = reconcile(inspections, fingerprint(strict_spec(candidate.spec)))['unresolved']
        pending, malformed = partition_pending(pending, candidate.spec)
        if pending:
            raise ValueError('还有真实图形工具查询未有效重做。保持同一图、tab、parameter、relation、points顺序和expected，纠正basis_ids后调用inspect_scene，不得删除检查或仅声称通过。待办：' + json.dumps(pending, ensure_ascii=False))
        scope_raw['value'] = copy.deepcopy(raw)
        return review
    compact_candidate, evidence = review_payload(candidate, evidence)
    from math_agent.proof_dependencies import review_dependencies
    repair_context = getattr(job, 'repair_review_context', None)
    if repair_context:
        from math_agent.deferred_review import content_binding
        if repair_context['current_binding'] != content_binding(source, candidate, job.request):
            repair_context = None
    if math_scope:
        compact_candidate['solutions'] = [s for s in compact_candidate['solutions'] if s['question_id'] in math_scope['context_questions']]
    context = {'source': source.model_dump(), 'candidate': compact_candidate, 'review_output_source': math_scope['output_source'] if math_scope else source.model_dump(), 'math_review_scope': {k: math_scope[k] for k in ('sha256', 'question_ids', 'reused_questions', 'context_questions')} if math_scope else None, 'repair_context': repair_context, 'proof_guard_observations': guards, 'proof_dependencies': review_dependencies(source, candidate.solutions), 'preparation': preparation, 'repair_objections': delta['objections'] if delta else getattr(job, 'pending_math_objections', []), 'reuse_certificate': delta, 'math_certificate': getattr(job, 'math_certificate', None) if stage == 'graph_review' else None, 'tool_evidence': evidence, 'browser_observation': observation}
    if job.settings.parallel_review and stage == 'review':
        from math_agent.parallel_review import generate
        review = validate(generate(job, source, candidate, context))
    else:
        review = job.validated(stage, context, validate)
    if math_scope:
        from math_agent.math_review_scope import receipt
        job.math_review_receipts.append(receipt(math_scope, scope_raw['value'], review))
        write_json(job.directory / 'math-review-receipts.json', job.math_review_receipts)
    inspections = [r for r in job.records[before:] if r['kind'] == 'tool' and r.get('name') == 'inspect_scene']
    observations = [r['result'] for r in inspections]
    from math_agent.inspection_recovery import reconcile, partition_pending
    from math_agent.scene_tools import fingerprint
    from math_agent.rendering import strict_spec
    recovery = reconcile(inspections, fingerprint(strict_spec(candidate.spec)))
    job.audit('inspection_recovery', recovery)
    blocked, malformed = partition_pending(recovery['unresolved'], candidate.spec)
    if blocked:
        raise ValueError('真实图形工具执行失败，仍有未完成有效重做的检查，不能发布')
    if malformed:
        job.audit('inspection_referral', {'stage': stage, 'pending': malformed, 'detail': '无效查询参数或断言依据交独立裁决；此记录不是解除或通过'})
    failures = []
    for item in malformed:
        t = item['target']
        failures.append({'id': f'A{len(failures) + 1}', 'kind': 'invalid_query', 'tab': t['tab'], 'parameter': t['parameter'], 'check': {k: t[k] for k in ('relation', 'points', 'expected')}, 'required_point_count': item['required_point_count'], 'referral_reason': item['referral_reason'], 'original_error': item.get('error'), **{k: item[k] for k in ('invalid_point_ids', 'available_point_ids', 'permitted_parameter_range') if k in item}, 'detail': '未得到有合法依据的有效检查；必须独立裁决，不能原样重做或自动撤销。'})
    for result in observations:
        for probe in result.get('probes', []):
            for check in probe.get('checks', []):
                if check.get('matches_expected') is False and 'error' not in check:
                    failures.append({'id': f'A{len(failures) + 1}', 'tab': probe['tab'], 'parameter': probe.get('parameter'), 'check': check})
    if any((r.get('ok') is True and r.get('assertions_pass') is False for r in observations)) and (not failures):
        raise ValueError('真实图形工具返回断言失败但没有明细，不能发布')
    rotation_results = [r.get('result', {}) for r in job.records[before:] if r['kind'] == 'tool' and r.get('name') == 'probe_rotation_rule']
    for result in rotation_results:
        for failure in result.get('counterexamples', []):
            failures.append({'id': f'A{len(failures) + 1}', **failure})
        for missed in result.get('unobserved', []):
            args = result['request']
            for points in args['triangles']:
                failures.append({'id': f'A{len(failures) + 1}', 'kind': 'proof_guard', 'tab': args['tab'], 'parameter': None, 'check': {'relation': 'collinear', 'points': points, 'expected': False, 'holds': None, 'matches_expected': None, 'observed': {}}, 'basis_ids': [result['step_id']], 'rule': result['rule'], 'case_evidence': {'question_id': result['question_id'], 'angle_degrees': missed['angle'], 'rotation_ids': args['rotation_ids'], 'original_spec_sha256': result['original_spec_sha256'], 'unobserved': missed}, 'detail': 'Agent请求的旋转前提取证未成功，没有共线或非共线结论。须独立判断原假设是否适用、证明前提是否已另证；不能以未返回反例冒充通过，证据不足保持unresolved。'})
    for failure in guards['failures']:
        failures.append({'id': f'A{len(failures) + 1}', **failure})
    diagram_objections = [{'id': f'R{i + 1}', 'kind': 'review_issue', 'issue': issue.model_dump()} for i, issue in enumerate(review.issues) if issue.stage == 'diagram']
    if (failures or diagram_objections) and review.verdict == 'revise' and any((i.stage in {'source', 'math'} for i in review.issues)):
        job.deferred_reviews.add(source, candidate, observation, job.request, failures + diagram_objections, review)
        write_json(job.directory / 'deferred-reviews.json', job.deferred_reviews.dump())
        job.review_snapshot = None
        job.review_certificates = []
        job.audit('review_deferred', {'stage': stage, 'pending': len(job.deferred_reviews.pending), 'detail': '实质读题/数学异议先回对应Agent修正；其余检查连同原版本完整暂存，发布前必须逐条裁决'})
        return review
    if failures or diagram_objections:
        decisions = job.adjudicate(source, candidate, observation, failures + diagram_objections, original_review=review if diagram_objections else None)
        if diagram_objections:
            review = decisions.reviewed
            if any((d.verdict == 'unresolved' for d in decisions.decisions)):
                raise NeedsInput('构图异议裁决仍有未决项')
            return review
        by_assertion = {f['id']: f for f in failures}
        for decision in decisions.decisions:
            if decision.verdict == 'unresolved':
                raise NeedsInput('断言依据仍无法确定：' + decision.reason)
            if decision.verdict == 'real_issue':
                review.verdict = 'revise'
                review.issues.append(decision.issue)
                failure = by_assertion[decision.assertion_id]
                if 'tab' in failure:
                    from math_agent.contracts import Check
                    qid = candidate.solutions[failure['tab']].question_id
                    review.questions = [Check(id=q.id, verdict='issue', evidence=decision.issue.detail) if q.id == qid and q.verdict != 'unknown' else q for q in review.questions]
        check_review(review, source)
    return review
