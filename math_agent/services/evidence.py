"""Evidence: behavior-preserving services; runtime dependencies are explicit arguments."""
from __future__ import annotations
from math_agent.storage import write_json
from math_agent.evidence import build_ledger, validate_basis

def save_ledger(job, source, candidate=None):
    job.ledger = build_ledger(source, candidate)
    write_json(job.directory / 'evidence-ledger.json', job.ledger)
    n = len(list(job.directory.glob('evidence-version-*.json'))) + 1
    write_json(job.directory / f'evidence-version-{n:02d}.json', job.ledger)
    job.audit('evidence', {'entries': len(job.ledger['entries']), 'version': n, 'detail': '原题条件、目标和待核验推导分开记录；引用不等于已经证明'})

def validate_parallel_candidate(job, source, candidate):
    if job.settings.parallel_questions:
        from math_agent.question_plan import build_plan, validate_basis
        validate_basis(source, build_plan(source, job.question_plan_metadata), candidate.solutions)
