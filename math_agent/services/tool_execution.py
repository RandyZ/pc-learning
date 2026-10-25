"""Tool execution: behavior-preserving services; runtime dependencies are explicit arguments."""
from __future__ import annotations
from math_agent.storage import write_json
from math_agent.tools import dispatch
from math_agent.evidence import read_ledger, validate_basis, record_hypothesis

def dispatch_stage_tool(job, stage, active_request, name, args):
    if name == 'view_image_region':
        if args.get('image_id') not in {i['id'] for i in active_request['images']}:
            raise ValueError('本阶段不能查看这张图片')
        return job.image_tools.view(**args)
    if name == 'probe_rotation_rule':
        if stage != 'review' or job.review_candidate is None:
            raise ValueError('旋转前提取证仅供当前独立数学复核使用')
        if job.scenes.count >= 12:
            raise ValueError('浏览器观察工具次数达到上限')
        job.scenes.count += 1
        from math_agent.rotation_probe import evaluate
        return evaluate(job.review_candidate, args, job.directory / 'rotation-probes' / f'probe-{job.scenes.count:02d}')
    if name == 'read_evidence':
        return read_ledger(job.ledger, args['ids'])
    if name == 'record_hypothesis':
        result = record_hypothesis(job.ledger, **args)
        write_json(job.directory / 'evidence-ledger.json', job.ledger)
        return result
    if name in {'preview_scene', 'inspect_scene'}:
        if (stage in {'review', 'review_plan', 'adjudicate', 'graph_review', 'diagram_review', 'deferred_review'}) != (name == 'inspect_scene'):
            raise ValueError('当前阶段不能调用此图形工具')
        if name == 'inspect_scene':
            from math_agent.inspection_recovery import schema_failure, reference_failure
            failure = schema_failure(args, job.scenes.current)
            if failure:
                return failure
            failure = reference_failure(args, job.scenes.current)
            if failure:
                return failure
            if stage == 'diagram_review' and job.review_change_tabs is not None:
                if any((p['tab'] not in job.review_change_tabs for p in args['probes'])):
                    raise ValueError('局部复核只能查询实际变化画板：' + str(job.review_change_tabs))
            invalid_checks = []
            errors = []
            from math_agent.inspection_recovery import target
            from math_agent.scene_tools import fingerprint
            from math_agent.rendering import strict_spec
            if stage in {'adjudicate', 'review_plan', 'diagram_review', 'deferred_review'}:
                forbidden = [target(p, {k: v for k, v in c.items() if k != 'expected'}, job.scenes.current) for p in args.get('probes', []) for c in p.get('checks', []) if 'expected' in c]
                if forbidden:
                    return {'ok': False, 'error': '本阶段只能查询：删除expected后，按原tab、参数、关系和点序重做以下查询；不得跳过。', 'execution_failure': {'kind': 'query_only', 'spec_sha256': fingerprint(strict_spec(job.scenes.current)), 'targets': forbidden}}
            for probe in args.get('probes', []):
                for check in probe.get('checks', []):
                    if 'expected' not in check:
                        continue
                    if stage in {'adjudicate', 'review_plan', 'diagram_review', 'deferred_review'}:
                        raise ValueError('本阶段只做查询，省略expected；根据真实观察和题设判断原断言是否合理')
                    tab = probe.get('tab')
                    if type(tab) is not int or not job.review_candidate or (not 0 <= tab < len(job.review_candidate.solutions)):
                        raise ValueError('观察小问不存在')
                    try:
                        validate_basis(job.ledger, check.get('basis_ids', []), job.review_candidate.solutions[tab].question_id)
                        if not check.get('rationale'):
                            raise ValueError('请解释依据为什么要求这个expected，或省略expected作查询')
                    except ValueError as exc:
                        invalid_checks.append(target(probe, check, job.scenes.current))
                        errors.append(str(exc))
            if invalid_checks:
                from math_agent.evidence import assertion_basis_index
                index = assertion_basis_index(job.ledger, job.review_candidate.solutions)
                return {'ok': False, 'error': '；'.join(errors), 'execution_failure': {'kind': 'basis_validation', 'spec_sha256': fingerprint(strict_spec(job.scenes.current)), 'targets': invalid_checks}, 'available_assertion_basis': {str(t['tab']): index[str(t['tab'])] for t in invalid_checks}, 'recovery_hint': '所列编号仅满足本画板作用域，不证明断言成立。须核对内容与rationale；依据不足时省略expected作查询，原失败仍按既有规则逐条处理，不得删掉或声称已通过。'}
        result = job.scenes.dispatch(name, args)
        if name == 'inspect_scene' and result.get('ok') is False and (not result.get('execution_failure')):
            failed = [target(p, c) for p in result.get('probes', []) for c in p.get('checks', []) if 'error' in c]
            result['execution_failure'] = {'kind': 'browser_execution', 'spec_sha256': fingerprint(strict_spec(job.scenes.current)), 'targets': failed or [target(p, c, job.scenes.current) for p in args.get('probes', []) for c in p.get('checks', [])]}
            from math_agent.inspection_recovery import partition_pending
            _, malformed = partition_pending([{'target': t} for t in result['execution_failure']['targets']], job.scenes.current)
            if malformed:
                result['invalid_queries'] = malformed
                result['recovery_hint'] = '这些关系的点数不合法，不能原样重做。需要观察时改用合法点数；最终review保留真实数学异议。原无效请求会交独立裁决，当前并未解除或视作通过。'
        return result
    return dispatch(name, args)
