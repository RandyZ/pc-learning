from ...evidence_v2.state import State
from ....contracts import Source
from ....domain.v2 import Draft
from ....policies.v2 import prerequisite_ids, operations, validate_draft
from ....storage import write_json


def extract_source(state, ctx):
    source = ctx.runner.extract(state.get("feedback"))
    return {"source": source.model_dump(), "source_revision": ctx.source_revision(source.model_dump()),
            "feedback": [], "prerequisites": {}, "math_evidence": {}, "diagram_evidence": {}}


def propose_solution(state, ctx):
    source = Source.model_validate(state["source"])
    draft = ctx.solver.propose(source, state.get("feedback"))
    return {"draft": draft.model_dump()}


def prepare_execution(state, ctx):
    source = Source.model_validate(state["source"])
    draft = Draft.model_validate(state["draft"])
    validate_draft(source, draft)
    revision = ctx.candidate_revision(state["source_revision"], state["draft"])
    ctx.runner.provider.candidate_revision = revision
    write_json(ctx.runner.directory / "v2-draft.json", state["draft"])
    selected = prerequisite_ids(draft)
    receipts = ctx.executor.run(state["source_revision"], revision, draft, selected)
    bad = []
    by_id = operations(draft)
    for oid in selected:
        r = receipts[oid]
        if r["execution_status"] != "ok" or not r["result"].get("scalar"):
            bad.append({"stage": "math", "target": by_id[oid][0],
                        "detail": f"构图依赖的计算{oid}没有得到有限实数常量；不得猜测坐标。{r['result']}"})
    return {"candidate_revision": revision, "prerequisites": receipts,
            "math_evidence": {}, "diagram_evidence": {}, "feedback": bad,
            "route": "repair" if bad else "execute"}
