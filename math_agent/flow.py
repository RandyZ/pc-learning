"""Compatible classic entrypoint backed by explicit LangGraph nodes and Runtime Context."""
from langsmith import tracing_context
from .workflows.classic.graph import build_graph
from .workflows.classic.context import ClassicContext
from .workflows.classic.state import State


def execute_graph(runner, initial=None):
    compiled = build_graph(initial.get("entry", "observe") if initial else "source")
    (runner.directory / "agent-graph.mmd").write_text(compiled.get_graph().draw_mermaid())
    # Automatic tracing could capture source pixels, state or provider reasoning.
    # The optional explicit sink sends only allowlisted span metadata.
    with tracing_context(enabled=False):
        return compiled.invoke(initial or {"repairs":0, "feedback":[]},
            {"recursion_limit":8 + runner.settings.max_repairs * 6, "callbacks":[]},
            context=ClassicContext(runner))
