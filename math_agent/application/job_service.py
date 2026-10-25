"""Opt-in V2 job assembly, compatible with the existing Worker process boundary."""
from __future__ import annotations

import copy
import json
import time
from ..contracts import Source, Candidate
from ..domain.v2 import Draft, digest
from ..policies.v2 import validate_draft
from ..parallel_budget import ParallelBudget
from ..models.v2_gateway import ModelGateway
from ..services.solving import PlanSolver
from ..services.verification import ReceiptExecutor
from ..services.publishing import Publisher
from ..observability.effective_config import effective, code_identity
from ..observability.langsmith import SafeTrace
from ..observability.timing import summarize
from ..storage import write_json
from ..workflows.evidence_v2.context import Context, input_identity


def make_context(runner):
    if runner.resume_state or (runner.directory / "revision.json").exists():
        raise ValueError("V2 requires a fresh task; no implicit replay of a classic paid attempt")
    if runner.settings.workflow != "evidence_v2":
        raise ValueError("V2 must be explicitly selected")
    diagnostic = getattr(runner.settings, "diagnostic_mode", False)
    input_hash = input_identity(runner)
    config = effective(runner.settings)
    code_hash = code_identity()
    write_json(runner.directory / "effective-config.json", config)
    write_json(runner.directory / "run-manifest.json", {"workflow_version": "evidence-v2.1",
        "input_hash": input_hash, "code_hash": code_hash, "config_hash": digest(config),
        "task_deadline_seconds": None if diagnostic else runner.settings.job_timeout, "automatic_paid_retry": False})
    # Model concurrency stays one; parallel V2 branches are local math/browser work.
    runner.shared_budget = ParallelBudget(runner.settings.max_calls,
        deadline=None if diagnostic else runner.started + runner.settings.job_timeout, max_concurrency=1)
    runner.provider.shared_budget = runner.shared_budget
    runner.provider = ModelGateway(runner.provider, runner.job_id, runner.audit)
    trace = SafeTrace(runner.job_id, runner.settings.v2_profile)
    runner.trace_sink = trace
    cache_key = "math-agent:v2:" + digest({"input": input_hash, "config": config, "code": code_hash})
    publisher = Publisher(runner, input_hash, cache_key)
    ctx = Context(runner, None, None, trace, input_hash, publisher)
    ctx.solver = PlanSolver(runner, ctx.check)
    def math_allowance():
        remaining = ctx.remaining()
        # No job deadline does not remove the mathematical subprocess watchdog.
        return 12.0 if remaining is None else remaining
    ctx.executor = ReceiptExecutor(runner.audit, ctx.check, math_allowance,
                                  reuse=runner.settings.v2_profile == "parallel_reuse")
    return ctx


def use_cache(ctx, cached):
    """Invalid cache fails closed; never replace it with a paid regeneration."""
    from ..compilers.scene import compile_candidate
    r = ctx.runner
    if r.deferred_reviews.pending or cached.get("schema_version") != "v2-cache.1" or cached.get("input_hash") != ctx.input_hash:
        raise ValueError("incompatible V2 cache or pending obligations")
    state = copy.deepcopy(cached["state"])
    if cached.get("state_hash") != digest(state):
        raise ValueError("corrupt V2 cached state")
    source, draft = Source.model_validate(state["source"]), Draft.model_validate(state["draft"])
    from ..contracts import check_source
    check_source(source, r.request["images"])
    if source.uncertainties:
        raise ValueError("cached source is unresolved")
    validate_draft(source, draft)
    if state["source_revision"] != ctx.source_revision(state["source"]) or state["candidate_revision"] != ctx.candidate_revision(state["source_revision"], state["draft"]):
        raise ValueError("stale V2 cached revision")
    for receipt in state["evidence_bundle"]["receipts"].values():
        if receipt["producer_version"] != ctx.executor.version:
            raise ValueError("cached computational producer has changed")
    candidate, source_map = compile_candidate(source, draft, state["evidence_bundle"]["receipts"])
    if digest(candidate) != digest(state["candidate"]):
        raise ValueError("cached compiled candidate does not match its plan")
    observation = r.observe(candidate)
    state["evidence_bundle"]["browser_status"] = observation["status"]
    ctx.publish.cache_hit = True
    ctx.publish.cached_tool_evidence = copy.deepcopy(cached.get("tool_evidence", []))
    write_json(r.directory / "v2-draft.json", state["draft"])
    write_json(r.directory / "v2-source-map.json", source_map)
    r.audit("cache", {"status": "hit", "workflow": "evidence_v2"})
    ctx.check()
    ctx.publish(state)


def execute(runner):
    ctx = make_context(runner)
    success = False
    try:
        ctx.check()
        cached = None if runner.request["force"] else runner.cache.get(ctx.publish.cache_key)
        if cached:
            use_cache(ctx, cached)
        else:
            runner.audit("cache", {"status": "forced" if runner.request["force"] else "miss", "workflow": "evidence_v2"})
            if not runner.settings.api_key:
                raise RuntimeError("DEEPSEEK_API_KEY is not configured")
            from ..workflows.evidence_v2.graph import execute as execute_graph
            state = execute_graph(ctx)
            if not state.get("completed"):
                raise ValueError("V2 graph did not reach publication")
        success = True
    finally:
        ctx.trace.close(success)
        write_json(runner.directory / "perf.json", summarize({"status": "completed" if success else "failed",
            "seconds": round(time.monotonic() - runner.started, 3), "audit": runner.records}))
