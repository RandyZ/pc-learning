"""Job service: behavior-preserving services; runtime dependencies are explicit arguments."""
from __future__ import annotations
import copy
import json
import time
from math_agent.cache import key_for
from math_agent.contracts import Source, Candidate, Review, check_source, check_candidate, check_review
from math_agent.storage import write_json
from math_agent.domain.presentation import canonical_page
from math_agent.domain.errors import NeedsInput

def execute(job):
    revision = job.directory / 'revision.json'
    if revision.is_file():
        from math_agent.revisions import validate_revision
        data = json.loads(revision.read_text())
        source, candidate, feedback = validate_revision(data, job.request)
        job.resume_state = {'entry': 'diagram', 'source': source, 'candidate': candidate, 'feedback': feedback, 'repairs': 0}
        write_json(job.directory / 'source.json', source.model_dump())
        job.save_ledger(source, candidate)
        job.audit('explicit_revision', {'original_id': data['original_id'], 'feedback': feedback, 'detail': '显式修订图形，沿用原题与数学解答；重新观察和复核，不能改写数学解答'})
    cache_key = key_for(job.settings, job.request)
    cached = None if job.request['force'] or job.resume_state else job.cache.get(cache_key)
    output = job.directory / 'output'
    if cached and job.deferred_reviews.pending:
        raise ValueError('仍有暂存复核义务，不得用缓存绕过，也未自动转为付费调用')
    if cached:
        job.audit('cache', {'status': 'hit', 'detail': '只重跑本地核验和网页引擎，不重新调用模型'})
        from math_agent.native_constructions import require_output
        require_output(cached['source'], job.settings.native_constructions)
        source = Source.model_validate(cached['source'])
        candidate = Candidate.model_validate(cached['candidate'])
        review = Review.model_validate(cached['review'])
        check_source(source, job.request['images'])
        check_candidate(candidate, source)
        if job.settings.parallel_questions:
            if 'question_plan_metadata' not in cached:
                raise ValueError('并发缓存缺少原依赖计划，不能放宽范围或自动付费重算')
            job.question_plan_metadata = copy.deepcopy(cached['question_plan_metadata'])
            job.validate_parallel_candidate(source, candidate)
        check_review(review, source)
        if review.verdict != 'pass':
            raise ValueError('缓存中没有通过复核的数据')
        from math_agent.review_reuse import check_certificate_chain
        job.review_certificates = copy.deepcopy(cached.get('review_certificates', []))
        check_certificate_chain(source, review, job.review_certificates)
        from math_agent.math_review_scope import check_receipts
        job.math_review_receipts = copy.deepcopy(cached.get('math_review_receipts', []))
        check_receipts(job.math_review_receipts, source)
        if job.settings.parallel_review:
            from math_agent.parallel_review import check_rounds
            job.parallel_review_rounds = copy.deepcopy(cached.get('parallel_review_rounds', []))
            check_rounds(job.parallel_review_rounds, source, candidate, job.request, job.math_review_receipts)
            write_json(job.directory / 'parallel-review-rounds.json', job.parallel_review_rounds)
        if job.math_review_receipts:
            write_json(job.directory / 'math-review-receipts.json', job.math_review_receipts)
        if job.review_certificates:
            write_json(job.directory / 'review-certificates.json', job.review_certificates)
        if canonical_page(copy.deepcopy(candidate), source) != candidate:
            raise ValueError('缓存展示内容与解答不一致')
        from math_agent.explanation_coverage import require as require_explanation
        require_explanation(candidate)
        from math_agent.native_constructions import require as require_constructions
        require_constructions(source, candidate)
        job.save_ledger(source, candidate)
        job.renderer(candidate.spec, output)
    else:
        if not job.settings.api_key:
            raise RuntimeError('尚未配置 DEEPSEEK_API_KEY')
        job.audit('cache', {'status': 'forced' if job.request['force'] else 'miss'})
        from math_agent.flow import execute_graph
        if job.settings.reference_mode and (not job.resume_state):
            from math_agent.reference_flow import execute_reference_graph
            state = execute_reference_graph(job)
        else:
            state = execute_graph(job, job.resume_state)
        source, candidate, review = (state['source'], state['candidate'], state['review'])
        job.complete_ocr()
        if job.shared_budget is not None:
            job.shared_budget.check()
        if job.deferred_reviews.pending:
            raise ValueError('仍有暂存复核义务，不能写缓存或发布')
        from math_agent.native_constructions import require as require_constructions
        require_constructions(source, candidate)
        job.validate_parallel_candidate(source, candidate)
        if job.settings.parallel_review:
            from math_agent.parallel_review import check_rounds
            check_rounds(job.parallel_review_rounds, source, candidate, job.request, job.math_review_receipts)
        job.cache.put(cache_key, {'source': source.model_dump(), 'candidate': candidate.model_dump(), 'review': review.model_dump(), **({'question_plan_metadata': copy.deepcopy(job.question_plan_metadata)} if job.settings.parallel_questions else {}), 'parallel_review_rounds': job.parallel_review_rounds, 'tool_evidence': [r for r in job.records if r['kind'] == 'tool'], 'deferred_reviews': job.deferred_reviews.dump(), 'review_certificates': job.review_certificates, 'math_review_receipts': job.math_review_receipts, 'model_calls': job.model_calls})
    if job.deferred_reviews.pending:
        raise ValueError('仍有暂存检查，不得发布网页')
    if job.shared_budget is not None:
        job.shared_budget.check()
        write_json(job.directory / 'shared-budget.json', job.shared_budget.snapshot())
    for name, data in [('source', source), ('candidate', candidate), ('review', review)]:
        write_json(job.directory / (name + '.json'), data.model_dump())
    report = {'status': 'completed', 'runtime': 'langgraph', 'workflow': 'reference' if job.settings.reference_mode and (not job.resume_state) else 'classic', 'model': job.settings.model, 'cache_hit': bool(cached), 'model_calls_this_job': job.model_calls, 'seconds': round(time.monotonic() - job.started, 3), 'source': source.model_dump(), 'review': review.model_dump(), 'review_certificates': job.review_certificates, 'math_review_receipts': job.math_review_receipts, 'deferred_reviews': job.deferred_reviews.dump(), 'audit': job.records, 'cached_tool_evidence': cached.get('tool_evidence', []) if cached else [], 'render_check': json.loads((output / 'export-check.json').read_text()) if (output / 'export-check.json').is_file() else None, 'render_check_scope': '真实 JSXGraph 对象和有限参数采样，不代表整题数学正确或连续区间证明', 'limitations': ['框架和浏览器观察不保证数学证明正确；模型仍需选对待验关系', '复核来自同一模型的新上下文，不能保证消除同源错误', '坐标工具校验给定实例，不是一般几何证明', '网页自动检查不等于真实数学正确率验收']}
    write_json(job.directory / 'report.json', report)
    job.store.finish(job.job_id, 'completed', summary={k: report[k] for k in ['model', 'cache_hit', 'model_calls_this_job', 'seconds']})

def resume_review(job):
    """Explicit operator action only: budget-exhausted, fully rendered candidate.

        No HTTP request is replayed. Re-render and independent review are mandatory;
        previous model usage and repairs remain part of the same job's budget.
        """
    if job.settings.parallel_questions or job.settings.parallel_review:
        raise ValueError('并发实验不支持重置原任务预算续跑；须显式新建隔离任务，不重放旧请求')
    prior = json.loads((job.directory / 'report.json').read_text())
    row = job.store.get(job.job_id)
    local_observation_collision = prior.get('error', '').startswith('[Errno 17] File exists:') and str(job.directory / 'observations' / 'scene-') in prior['error'] and any((r['kind'] == 'explicit_resume' for r in prior.get('audit', [])))
    if row['status'] != 'failed' or not (prior.get('error', '').startswith('达到模型调用预算上限') or local_observation_collision):
        raise ValueError('只能显式接续调用预算耗尽的失败任务')
    records = prior['audit']
    nodes = [r.get('node') for r in records if r['kind'] == 'graph_node']
    if not nodes or nodes[-1] != 'review':
        raise ValueError('没有完整渲染后等待复核的候选')
    source = Source.model_validate(json.loads((job.directory / 'source.json').read_text()))
    candidate = Candidate.model_validate(json.loads((job.directory / 'candidate.json').read_text()))
    check_source(source, job.request['images'])
    check_candidate(candidate, source)
    if source.uncertainties:
        raise NeedsInput('原题仍有未决问题')
    calls = prior['model_calls_this_job']
    if job.settings.max_calls <= calls:
        raise ValueError('接续必须显式增加调用上限')
    with job.store.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT count(*) FROM jobs WHERE status IN ('running','queued')").fetchone()[0]:
            raise ValueError('接续前需等待其他任务完成')
        changed = c.execute("UPDATE jobs SET status='running',stage='resume_review',error=NULL WHERE id=? AND status='failed'", (job.job_id,))
        if changed.rowcount != 1:
            raise ValueError('任务状态已变化')
    write_json(job.directory / f'report-before-resume-{calls}.json', prior)
    job.records = records
    job.started -= prior['seconds']
    job.provider.calls = calls
    job.observation_count = len(list(job.directory.glob('observation-*.json')))
    job.resume_state = {'source': source, 'candidate': candidate, 'repairs': sum((r['kind'] == 'repair' for r in records)), 'feedback': []}
    job.audit('explicit_resume', {'stage': 'review', 'previous_calls': calls, 'max_calls': job.settings.max_calls, 'detail': '显式补充预算；复用已完成候选，重新渲染和独立复核，不重发旧请求'})
    job.run()

def run(job):
    try:
        job.execute()
    except Exception as exc:
        detail = str(exc)[:4000]
        for secret in [job.settings.api_key, job.settings.access_token]:
            if secret:
                detail = detail.replace(secret, '[REDACTED]')
        status = 'needs_input' if isinstance(exc, NeedsInput) else 'failed'
        write_json(job.directory / 'report.json', {'status': status, 'error': detail, 'audit': job.records, 'model_calls_this_job': job.model_calls, 'seconds': round(time.monotonic() - job.started, 3)})
        job.store.finish(job.job_id, status, error=detail)
    finally:
        if job.pending_ocr is not None:
            job.pending_ocr.close()
        if job.shared_budget is not None:
            write_json(job.directory / 'shared-budget.json', job.shared_budget.snapshot())
        job.provider.close()
