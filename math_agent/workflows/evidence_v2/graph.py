"""Explicit V2 topology, fixed fan-out/fan-in, no retries of paid nodes."""
from __future__ import annotations

import time
from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime
from langsmith import tracing_context
from .context import Context
from .state import State
from .nodes import preparation, verification, review


def build_graph(profile="serial", *, default_context=None):
    if profile not in {"serial", "parallel", "parallel_reuse"}:
        raise ValueError("unknown V2 execution profile")
    graph = StateGraph(State, context_schema=Context)
    functions = {"extract_source": preparation.extract_source, "propose_solution": preparation.propose_solution,
        "prepare_execution": preparation.prepare_execution, "check_math": verification.check_math,
        "compile_and_observe": verification.compile_and_observe, "join_evidence": verification.join_evidence,
        "independent_review": review.independent_review, "repair": review.repair, "publish": review.publish}
    def wrap(name, fn):
        def node(state: State, runtime: Runtime[Context]):
            ctx = runtime.context or default_context
            if ctx is None:
                raise ValueError("V2 needs a task runtime context; Studio execution requires explicit job authorization")
            nonce = getattr(ctx, "studio_nonce", None)
            if nonce and state.get("studio_nonce") != nonce:
                if name != "extract_source" or state.get("studio_nonce"):
                    raise ValueError("Studio checkpoint replay is not authorized; prepare a new task")
            ctx.check()
            start = time.monotonic()
            ctx.runner.audit("graph_node", {"node": name, "stage": name})
            with ctx.trace.span(name):
                try:
                    result = fn(state, ctx)
                    if nonce and name == "extract_source":
                        result["studio_nonce"] = nonce
                    if name != "publish":
                        ctx.check()
                    return result
                except BaseException:
                    cleanup = getattr(ctx, "studio_failure", None)
                    if cleanup is not None:
                        cleanup()
                    raise
                finally:
                    ctx.runner.audit("v2_node", {"node": name, "stage": name, "seconds": time.monotonic() - start})
        return node
    for name, fn in functions.items():
        graph.add_node(name, wrap(name, fn))
    graph.add_edge(START, "extract_source")
    graph.add_edge("extract_source", "propose_solution")
    graph.add_edge("propose_solution", "prepare_execution")
    def dispatch(state):
        if state["route"] == "repair":
            return "repair"
        return "check_math" if profile == "serial" else ["check_math", "compile_and_observe"]
    graph.add_conditional_edges("prepare_execution", dispatch,
        {"repair": "repair", "check_math": "check_math", "compile_and_observe": "compile_and_observe"})
    if profile == "serial":
        graph.add_edge("check_math", "compile_and_observe")
        graph.add_edge("compile_and_observe", "join_evidence")
    else:
        graph.add_edge(["check_math", "compile_and_observe"], "join_evidence")
    graph.add_conditional_edges("join_evidence", lambda s: s["route"], {"repair": "repair", "review": "independent_review"})
    graph.add_conditional_edges("independent_review", lambda s: s["route"], {"repair": "repair", "publish": "publish"})
    graph.add_conditional_edges("repair", lambda s: s["route"], {"source": "extract_source", "prepare": "prepare_execution"})
    graph.add_edge("publish", END)
    return graph.compile(name="evidence_v2")


def execute(context):
    runner = context.runner
    graph = build_graph(runner.settings.v2_profile)
    (runner.directory / "agent-graph.mmd").write_text(graph.get_graph().draw_mermaid())
    # Metadata-only explicit tracing prevents graph state and exception text leaking.
    with tracing_context(enabled=False):
        return graph.invoke({"job_id": runner.job_id, "repairs": 0, "feedback": [], "prior_evidence": []},
            {"recursion_limit": 16 + 12 * runner.settings.max_repairs, "max_concurrency": 2, "callbacks": []}, context=context)
