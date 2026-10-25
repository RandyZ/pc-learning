"""Local Studio factory for the same classic nodes, with explicit one-shot permission.

No model client is built when loading topology. This is node-level diagnostics,
not a production task runner; use the original Worker for publication and timing.
"""
from __future__ import annotations
from dataclasses import replace
from pathlib import Path
import os
import re
import time
from langgraph.runtime import Runtime
from langsmith import tracing_context
from math_agent.config import ROOT, Settings
from math_agent.pipeline import Runner
from math_agent.storage import Store
from math_agent.parallel_budget import ParallelBudget
from math_agent.workflows.classic.context import ClassicContext
from math_agent.workflows.classic.graph import build_graph


def debug_root():
    return Path(os.getenv('CLASSIC_DEBUG_ROOT',str(ROOT/'.classic-debug'))).resolve()


def make_graph(config=None):
    options=(config or {}).get('configurable',{})
    runtime_holder={}
    def acquire():
        if runtime_holder.get('closed'):
            raise ValueError('Diagnostic attempt is closed; prepare a new task instead of replaying paid work')
        if 'job' in runtime_holder:
            return runtime_holder['job']
        if options.get('allow_paid_model_calls') is not True:
            raise ValueError('Set allow_paid_model_calls=true explicitly for a fresh diagnostic task')
        job_id=options.get('debug_job_id','')
        if not isinstance(job_id,str) or not re.fullmatch(r'[a-f0-9]{32}',job_id):
            raise ValueError('Use a prepared debug_job_id, not a filesystem path')
        if os.getenv('LANGSMITH_TRACING','false').lower()=='true':
            raise ValueError('Studio node diagnostics require automatic cloud tracing disabled')
        root=debug_root();directory=root/'jobs'/job_id
        if not directory.is_dir() or not (directory/'request.json').is_file() or not directory.resolve().is_relative_to(root):
            raise ValueError('Prepared diagnostic task not found in the isolated debug root')
        cfg=replace(Settings(data_dir=root),redis_url='')
        if cfg.reference_mode or cfg.parallel_questions or cfg.parallel_review:
            raise ValueError('Studio factory exposes the classic sequential graph only')
        if not cfg.api_key:
            raise ValueError('Configure model key locally before an authorized diagnostic run')
        store=Store(root)
        row=store.get(job_id)
        if not row or row['status']!='queued':
            raise ValueError('Only a fresh queued diagnostic task may execute')
        # Atomic persistent lease blocks restarts, replays and forks of a paid run.
        with (directory/'studio-attempt.lock').open('x') as lease:lease.write('one-shot\n')
        with store.connection() as c:
            changed=c.execute("UPDATE jobs SET status='running',stage='studio' WHERE id=? AND status='queued'",(job_id,))
            if changed.rowcount!=1:raise ValueError('Diagnostic task state changed')
        try:
            job=Runner(cfg,job_id)
            budget=ParallelBudget(cfg.max_calls,deadline=job.started+cfg.job_timeout,max_concurrency=1)
            job.shared_budget=budget;job.provider.shared_budget=budget
            runtime_holder['job']=job
            return job
        except BaseException:
            store.finish(job_id,'failed',error='Diagnostic setup failed; no automatic retry')
            runtime_holder['closed']=True
            raise

    def close(job,failed=False):
        runtime_holder['closed']=True
        try:
            if job.pending_ocr is not None:job.pending_ocr.close()
        finally:
            job.provider.close()
        job.store.finish(job.job_id,'failed' if failed else 'interrupted',
            error='Node-level diagnostic only; no production publication. Prepare a new task to run again.')

    def adapt(name,action):
        def node(state,runtime):
            if name=='source' and 'job' in runtime_holder and not state.get('feedback'):
                raise ValueError('Paid source replay is disabled; prepare a fresh diagnostic task')
            job=acquire()
            try:
                job.shared_budget.check()
                with tracing_context(enabled=False):
                    result=action(state,Runtime(context=ClassicContext(job)))
                if name=='review' and result.get('next')=='__end__':close(job)
                return result
            except BaseException:
                if not runtime_holder.get('closed'):close(job,True)
                raise
        return node
    return build_graph(node_adapter=adapt)
