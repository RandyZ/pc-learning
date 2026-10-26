"""Display the V2 graph, or explicitly execute one prepared isolated task.

Schema inspection makes no provider. Resuming/forking a paid run is deliberately
unsupported: each execution requires a fresh --prepare-only directory.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import uuid
from langchain_core.runnables import RunnableConfig
from math_agent.config import Settings,ROOT
from math_agent.pipeline import Runner
from math_agent.application.job_service import make_context
from math_agent.workflows.evidence_v2.graph import build_graph
from benchmarks.profiles import overrides


def graph(config: RunnableConfig):
    options=config.get('configurable',{})
    task=options.get('studio_task')
    if not task:
        return build_graph('serial')  # Pure topology/schema access; no credentials/client.
    if os.getenv('LANGSMITH_TRACING', 'false').lower() == 'true':
        raise ValueError('Studio runs require LANGSMITH_TRACING=false; use Worker for metadata-only cloud tracing')
    if options.get('allow_paid_model_calls') is not True:
        raise ValueError('Studio model execution requires allow_paid_model_calls=true')
    root=Path(os.getenv('AGENT_DEBUG_DATA_DIR',str(ROOT/'.studio-runs'))).resolve()
    directory=(root/task).resolve()
    if not directory.is_relative_to(root) or directory==root:
        raise ValueError('Studio task must be inside the isolated debug root')
    manifest=json.loads((directory/'manifest.json').read_text())
    if manifest['profile'] not in {'B0','B1','B2'}:
        raise ValueError('Studio supports only explicitly prepared V2 tasks')
    # Graph factories may run in shared threads; never mutate process environment.
    switches=overrides(manifest['profile'])
    cfg=replace(Settings(),data_dir=directory/'runtime',redis_url='',workflow='evidence_v2',
        v2_profile=switches['AGENT_V2_PROFILE'],compact_tools=True,reference_mode=False,review_plan=False,
        solve_draft=False,parallel_questions=False,parallel_review=False,overlap_ocr=False,
        incremental_review=False,incremental_math_review=False)
    if not cfg.api_key:raise ValueError('DEEPSEEK_API_KEY is not configured')
    from math_agent.storage import Store
    store=Store(cfg.data_dir);job=manifest['job_id'];job_dir=store.path(job)
    # Atomic one-shot marker: two Studio runs cannot replay this prepared task.
    marker=job_dir/'studio-attempt.lock'
    marker.open('x').close()
    try:
        if any((job_dir/name).exists() for name in ('audit.json','report.json','run-manifest.json')):
            raise ValueError('Studio cannot resume or repeat a previously executed task')
        if store.get(job)['status']!='queued' or store.claim()!=job:
            raise ValueError('Studio requires a fresh queued task')
        runner=Runner(cfg,job)
        ctx=make_context(runner)
        ctx.studio_nonce=uuid.uuid4().hex
        original=ctx.publish
        def publish(state):
            try:original(state)
            finally:ctx.trace.close(True);runner.provider.close()
        ctx.publish=publish
        def failure():
            store.finish(job,'failed',error='Studio run stopped; not automatically retried')
            ctx.trace.close(False);runner.provider.close()
        ctx.studio_failure=failure
        return build_graph(cfg.v2_profile,default_context=ctx)
    except BaseException:
        store.finish(job,'failed',error='Studio preparation failed; prepare a new isolated task')
        raise
