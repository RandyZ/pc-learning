"""Classic topology. Compilation has no filesystem, client or model side effects."""
from langgraph.graph import StateGraph, START, END
from .context import ClassicContext
from .state import State
from . import nodes

NODE_NAMES = ("source", "solve", "math_repair", "diagram", "observe", "review", "repair")


def build_graph(entry="source", *, node_adapter=None):
    if entry not in NODE_NAMES:
        raise ValueError("Unknown classic entry node")
    graph = StateGraph(State, context_schema=ClassicContext)
    for name in NODE_NAMES:
        action = getattr(nodes, name)
        graph.add_node(name, node_adapter(name, action) if node_adapter else action)
    graph.add_edge(START, entry)
    graph.add_edge("source", "solve")
    for name in ("math_repair", "solve", "diagram"):
        graph.add_edge(name, "observe")
    graph.add_conditional_edges("observe", lambda s: s["next"], {"review":"review", "repair":"repair"})
    graph.add_conditional_edges("review", lambda s: s["next"], {"repair":"repair", END:END})
    graph.add_conditional_edges("repair", lambda s: s["next"], {
        "source":"source", "math_repair":"math_repair", "diagram":"diagram"})
    # Never attach an automatic retry/checkpoint policy to paid model actions.
    return graph.compile(name="classic")
