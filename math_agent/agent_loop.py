"""Compatibility entrypoint for the explicit model/tool LangGraph subgraph."""
from langsmith import tracing_context
from .workflows.tool_loop.state import LoopState
from .workflows.tool_loop.context import LoopContext
from .workflows.tool_loop.graph import build_graph


def run_loop(request, messages, tools, dispatch, audit, stage, error_type,
             remaining_calls=None, verify_initial_output=False, remaining_seconds=None):
    context = LoopContext(request, tools, dispatch, audit, stage, error_type,
                          remaining_calls, verify_initial_output, remaining_seconds)
    with tracing_context(enabled=False):
        return build_graph().invoke({"history":list(messages), "turns":0},
            {"recursion_limit":10, "callbacks":[]}, context=context)["value"]
