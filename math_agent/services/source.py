"""Source: behavior-preserving services; runtime dependencies are explicit arguments."""
from __future__ import annotations
import copy
from math_agent.contracts import Source, check_source
from math_agent.storage import write_json
from math_agent.domain.errors import NeedsInput

def extract(job, feedback=None):
    if 'ocr_evidence' not in job.request:
        from math_agent.ocr import recognize
        if job.settings.overlap_ocr and job.settings.ocr_url and job.request['images']:
            from math_agent.ocr_overlap import PendingOCR
            job.pending_ocr = PendingOCR(job.settings, job.request, job.directory, job.audit, job.started + job.settings.job_timeout, recognize)
            job.request['ocr_evidence'] = []
            job.request['_ocr_overlap'] = True
        else:
            evidence = recognize(job.settings, job.request, job.directory, job.audit)
            job.request['ocr_evidence'] = evidence
            if evidence:
                write_json(job.directory / 'ocr.json', evidence)

    def validate(raw):
        from math_agent.native_constructions import require_output
        require_output(raw.get('source') if job.settings.parallel_questions and isinstance(raw, dict) else raw, job.settings.native_constructions)
        if job.settings.parallel_questions:
            if not isinstance(raw, dict) or set(raw) != {'source', 'parallel_plan'}:
                raise ValueError('小问实验读题输出须为source与parallel_plan的完整封装')
            value = Source.model_validate(raw['source'])
            from math_agent.question_plan import build_plan
            build_plan(value, raw['parallel_plan'])
            job.question_plan_metadata = copy.deepcopy(raw['parallel_plan'])
            write_json(job.directory / 'question-plan-metadata.json', job.question_plan_metadata)
        else:
            value = Source.model_validate(raw)
        check_source(value, job.request['images'])
        return value
    source = job.validated('source', {'feedback': feedback or []}, validate)
    if source.uncertainties:
        job.complete_ocr()
    if source.uncertainties and job.request.get('ocr_evidence') and (not job.source_rechecked):
        job.question_plan_metadata = None
        job.source_rechecked = True
        write_json(job.directory / 'source-before-reread.json', source.model_dump())
        job.audit('source_reread', {'stage': 'source', 'reason': 'OCR辅助读题仍有未决问题；仅用原题重新核对一次，不携带OCR或参考答案', 'uncertainties': source.uncertainties})
        original = {**job.request, 'ocr_evidence': [], 'images': [i for i in job.request['images'] if i['role'] == 'question']}
        from math_agent.source_review import check_reread, apply_reread

        def validate_reread(raw):
            from math_agent.native_constructions import require_output
            require_output(raw.get('source') if isinstance(raw, dict) else None, job.settings.native_constructions)
            return check_reread(raw, issues, original['images'])
        issues = [{'id': f'U{i + 1}', 'text': text} for i, text in enumerate(source.uncertainties)]
        decision = job.validated('source_review', {'previous_source': source.model_dump(), 'issues': issues, 'feedback': feedback or []}, validate_reread, request_override=original)
        write_json(job.directory / 'source-question-review.json', decision.model_dump())
        job.audit('source_question_review', {'stage': 'source_review', 'decisions': [d.model_dump() for d in decision.decisions]})
        source = apply_reread(decision, issues)
    write_json(job.directory / 'source.json', source.model_dump())
    if source.uncertainties:
        raise NeedsInput('；'.join(source.uncertainties))
    job.save_ledger(source)
    return source

def complete_ocr(job):
    if job.pending_ocr is not None:
        evidence = job.pending_ocr.result()
        job.request['ocr_evidence'] = evidence
        if evidence:
            write_json(job.directory / 'ocr.json', evidence)
