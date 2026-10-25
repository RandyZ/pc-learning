"""Adjudication: behavior-preserving services; runtime dependencies are explicit arguments."""
from __future__ import annotations
import copy
from math_agent.contracts import check_review
from math_agent.storage import write_json
from math_agent.contracts import Adjudication, exact_ids
from math_agent.evidence import validate_basis
from math_agent.domain.presentation import compact_tool_evidence
from math_agent.domain.errors import NeedsInput

def settle_deferred(job, source, candidate, observation, review):
    """Fresh current review precedes an independent, explicitly version-bound resolution."""
    payload = job.deferred_reviews.payload(source, candidate, job.request)
    job.scenes.current = copy.deepcopy(candidate.spec)
    job.review_candidate = candidate
    job.save_ledger(source, candidate)
    job.review_change_tabs = None
    job.active_review_delta = None
    before = len(job.records)

    def validate(raw):
        result = job.deferred_reviews.validate(raw, payload, source, candidate, job.ledger)
        from math_agent.inspection_recovery import reconcile
        from math_agent.scene_tools import fingerprint
        from math_agent.rendering import strict_spec
        checks = [r for r in job.records[before:] if r['kind'] == 'tool' and r.get('name') == 'inspect_scene']
        if reconcile(checks, fingerprint(strict_spec(candidate.spec)))['unresolved']:
            raise ValueError('暂存裁决中的工具失败尚未有效重做，不能解除待审检查')
        return result
    result = job.validated('deferred_review', {**payload, 'source': source.model_dump(), 'candidate': candidate.model_dump(), 'browser_observation': observation, 'current_review': review.model_dump(), 'evidence_ledger': job.ledger, 'tool_evidence': compact_tool_evidence([r for r in job.records if r['kind'] == 'tool'][-24:])}, validate)
    if any((r.verdict == 'unresolved' for r in result.resolutions)):
        write_json(job.directory / 'deferred-last-unresolved.json', result.model_dump())
        raise NeedsInput('修订后仍有暂存检查未决；原记录保留，不能发布')
    job.deferred_reviews.commit(result, payload)
    write_json(job.directory / 'deferred-reviews.json', job.deferred_reviews.dump())
    for decision in result.resolutions:
        if decision.verdict == 'real_issue':
            review.issues.append(decision.issue)
            review.verdict = 'revise'
            from math_agent.contracts import Check
            affected = {s.question_id for s in decision.scopes}
            review.questions = [Check(id=q.id, verdict='issue', evidence=decision.issue.detail) if q.id in affected and q.verdict != 'unknown' else q for q in review.questions]
    check_review(review, source)
    job.audit('deferred_review_settled', {'origins': [b['sha256'] for b in payload['deferred_bundles']], 'current_hash': payload['current_hash'], 'decisions': result.model_dump()})
    return review

def adjudicate(job, source, candidate, observation, failures, original_review=None):
    """Independent evidence-based decision; never edit mathematics or the diagram."""
    job.audit('graph_node', {'node': 'adjudicate', 'failed_assertions': len(failures)})
    compact_decisions = bool(job.active_review_delta and original_review is not None)

    def validate(raw):
        from math_agent.compact_adjudication import unpack as unpack_decisions
        result = unpack_decisions(raw, original_review) if compact_decisions else Adjudication.model_validate(raw)
        exact_ids([d.assertion_id for d in result.decisions], [f['id'] for f in failures], '断言裁决')
        by_id = {f['id']: f for f in failures}
        for d in result.decisions:
            item = by_id[d.assertion_id]
            if item.get('kind') == 'review_issue':
                exact_ids([scope.question_id for scope in d.scopes], [scope.question_id for scope in d.scopes], '异议小问作用域')
                for scope in d.scopes:
                    if scope.question_id not in {s.question_id for s in candidate.solutions}:
                        raise ValueError(f'{d.assertion_id}须绑定实际小问；可选{[s.question_id for s in candidate.solutions]}')
                    tab = next((i for i, s in enumerate(candidate.solutions) if s.question_id == scope.question_id))
                    objects = {o['id'] for o in candidate.spec['tabs'][tab]['board']['objects']}
                    if not set(scope.object_ids) <= objects:
                        raise ValueError(f'{d.assertion_id}/{scope.question_id}必须绑定当前小问真实object_ids')
                    try:
                        validate_basis(job.ledger, scope.basis_ids, scope.question_id)
                    except ValueError as exc:
                        raise ValueError(f'{d.assertion_id}/{scope.question_id}: {exc}') from exc
            else:
                question_id = candidate.solutions[item['tab']].question_id
                validate_basis(job.ledger, d.basis_ids, question_id)
            if d.verdict == 'real_issue' and d.issue is None:
                raise ValueError('真实问题必须明确来源/数学/构图的修复目标')
            if d.verdict != 'real_issue' and d.issue is not None:
                raise ValueError('非真实问题不能附带修图要求')
        if original_review is not None:
            from math_agent.objection_review import validate_resolution
            validate_resolution(result, original_review, source)
        elif result.reviewed is not None:
            raise ValueError('仅断言裁决不得改写原复核')
        return result
    before = len(job.records)
    result = job.validated('adjudicate', {'source': source.model_dump(), 'candidate': candidate.model_dump(), 'browser_observation': observation, 'failed_assertions': failures, 'original_review': original_review.model_dump() if original_review else None, 'evidence_ledger': job.ledger, 'compact_decisions': compact_decisions, 'reuse_certificate': job.active_review_delta if compact_decisions else None}, validate)
    from math_agent.inspection_recovery import reconcile
    from math_agent.scene_tools import fingerprint
    from math_agent.rendering import strict_spec
    inspections = [r for r in job.records[before:] if r['kind'] == 'tool' and r.get('name') == 'inspect_scene']
    recovery = reconcile(inspections, fingerprint(strict_spec(candidate.spec)))
    job.audit('adjudication_recovery', recovery)
    if recovery['unresolved']:
        raise ValueError('断言裁决中的真实图形工具失败且尚未完成对应查询，不能发布')
    n = len(list(job.directory.glob('adjudication-*.json'))) + 1
    write_json(job.directory / f'adjudication-{n:02d}.json', {'failures': failures, **result.model_dump()})
    job.audit('adjudication', result.model_dump())
    return result
