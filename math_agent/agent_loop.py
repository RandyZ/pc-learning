"""Compatibility entrypoint for the explicit model/tool LangGraph subgraph."""
import sys
from langsmith import tracing_context
from .workflows.tool_loop.state import LoopState
from .workflows.tool_loop.context import LoopContext
from .workflows.tool_loop.graph import build_graph


def run_loop(request, messages, tools, dispatch, audit, stage, error_type,
             remaining_calls=None, verify_initial_output=False, remaining_seconds=None, max_turns=4):
    context = LoopContext(request, tools, dispatch, audit, stage, error_type,
                          remaining_calls, verify_initial_output, remaining_seconds, max_turns)
    with tracing_context(enabled=False):
        return build_graph().invoke({"history":list(messages), "turns":0},
            {"recursion_limit":sys.maxsize if max_turns is None else max(10, max_turns * 2 + 2), "callbacks":[]}, context=context)["value"]
