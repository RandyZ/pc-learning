"""Pure presentation/evidence projections shared by classic and future workflows."""
from __future__ import annotations
import copy
import json

from .errors import NeedsInput

def canonical_page(candidate, source, audit=None):
    """A single authoritative solution supplies displayed steps and answers."""
    from math_agent.parent_binding import bind_parent_roles
    candidate.spec, _ = bind_parent_roles(candidate.spec, audit)
    candidate.spec['problem_text'] = source.problem_text
    for solution, tab in zip(candidate.solutions, candidate.spec.get('tabs', [])):
        tab['steps'] = [{'head': step.rule, 'content': step.premise + '\n' + step.conclusion} for step in solution.steps]
        if solution.cases:
            tab['steps'].append({'head': '分类讨论', 'content': '\n'.join(solution.cases)})
        tab['steps'].append({'head': '定义域与边界检查', 'content': solution.domain_and_boundaries})
        tab['conclusion'] = solution.result
    return candidate

def review_payload(candidate, records):
    """Remove duplicated presentation text and historical draft specs, not evidence."""
    data = candidate.model_dump()
    spec = data['spec']
    spec.pop('problem_text', None)
    for solution, tab in zip(data['solutions'], spec.get('tabs', [])):
        tab.pop('steps', None)
        tab.pop('conclusion', None)
    return (data, compact_tool_evidence(records))

def compact_tool_evidence(records):
    evidence = copy.deepcopy(records)
    for record in evidence:
        if record.get('name') != 'preview_scene':
            continue
        try:
            args = json.loads(record.get('arguments', '{}'))
            if 'spec' in args:
                from math_agent.scene_tools import fingerprint
                args['draft_spec_sha256'] = fingerprint(args.pop('spec'))
            record['arguments'] = json.dumps(args, ensure_ascii=False)
        except (ValueError, TypeError):
            pass
    return evidence

def route(review):
    stages = {i.stage for i in review.issues}
    return 'source' if 'source' in stages else 'math_repair' if 'math' in stages else 'diagram'

def failed_inspection_detail(result):
    failures = [{'tab': p['tab'], 'parameter': p.get('parameter'), 'check': c} for p in result.get('probes', []) for c in p.get('checks', []) if c.get('matches_expected') is False or c.get('error')]
    data = {'ok': result.get('ok'), 'error': str(result.get('error', ''))[:400], 'failed_checks': failures[:6], 'failed_count': len(failures)}
    while len(json.dumps(data, ensure_ascii=False)) > 2200 and len(data['failed_checks']) > 1:
        data['failed_checks'].pop()
    return json.dumps(data, ensure_ascii=False)[:2400]
