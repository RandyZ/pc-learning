"""One explicit reusable tool subgraph; no hidden retries."""
from langgraph.graph import StateGraph, START, END
from .state import LoopState
from .context import LoopContext
from . import nodes


def build_graph():
    graph = StateGraph(LoopState, context_schema=LoopContext)
    graph.add_node("model", nodes.model)
    graph.add_node("tools", nodes.execute_tools)
    graph.add_node("draft", nodes.verify_draft)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", lambda s: "tools" if s["finish"] == "tool_calls"
        else "draft" if s["finish"] == "draft" else END,
        {"tools":"tools", "draft":"draft", END:END})
    graph.add_edge("tools", "model")
    graph.add_edge("draft", "model")
    return graph.compile(name="model_tools")
