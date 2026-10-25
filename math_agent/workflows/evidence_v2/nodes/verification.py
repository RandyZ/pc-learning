from ....contracts import Source
from ....domain.v2 import Draft, digest
from ....compilers.scene import compile_candidate
from ....rendering import RenderError
from ....storage import write_json


def check_math(state, ctx):
    receipts = ctx.executor.run(state["source_revision"], state["candidate_revision"],
        Draft.model_validate(state["draft"]), initial=state["prerequisites"])
    return {"math_evidence": {"candidate_revision": state["candidate_revision"], "receipts": receipts}}


def compile_and_observe(state, ctx):
    revision = state["candidate_revision"]
    try:
        candidate, source_map = compile_candidate(Source.model_validate(state["source"]),
            Draft.model_validate(state["draft"]), state["prerequisites"])
        write_json(ctx.runner.directory / "v2-source-map.json", source_map)
        write_json(ctx.runner.directory / "candidate.json", candidate.model_dump())
        observation = ctx.runner.observe(candidate)
        ctx.check()
        return {"diagram_evidence": {"candidate_revision": revision, "candidate": candidate.model_dump(),
            "observation": observation, "issues": []}}
    except (ValueError, RenderError) as exc:
        # Runtime/host failures are not mathematical mistakes. Never spend another
        # model call asking it to repair a blocked browser or a missing executable.
        if isinstance(exc, RenderError) and any(token in str(exc) for token in (
                "ERR_BLOCKED_BY_ADMINISTRATOR", "Executable doesn't exist", "BrowserType.launch",
                "TargetClosedError", "渲染超时", "渲染进程异常退出")):
            raise RuntimeError("browser infrastructure failed; no paid repair attempted") from exc
        supplied = getattr(exc, "issues", None)
        issues = [{"stage": "diagram", "target": i.get("target", i.get("question_id", "browser")),
                   "detail": i["detail"]} for i in supplied] if supplied else [
                       {"stage": "diagram", "target": "browser", "detail": str(exc)[:3000]}]
        return {"diagram_evidence": {"candidate_revision": revision, "issues": issues}}


def join_evidence(state, ctx):
    math, diagram = state["math_evidence"], state["diagram_evidence"]
    if any(p.get("candidate_revision") != state["candidate_revision"] for p in (math, diagram)):
        raise ValueError("cannot join missing or different candidate revisions")
    if diagram["issues"]:
        return {"feedback": diagram["issues"], "route": "repair"}
    candidate = diagram["candidate"]
    bundle = {"schema_version": "evidence-bundle-v2.1", "source_revision": state["source_revision"],
        "candidate_revision": state["candidate_revision"], "candidate_hash": digest(candidate),
        "browser_status": diagram["observation"].get("status"), "receipts": math["receipts"],
        "mandatory_conditions": [c["id"] for c in state["source"]["conditions"]],
        "mandatory_questions": [q["id"] for q in state["source"]["questions"]],
        "prior_evidence": state.get("prior_evidence", []),
        "scope": "Calculations and finite browser samples are evidence, not proof of correct source formalization."}
    write_json(ctx.runner.directory / "v2-evidence.json", bundle)
    return {"candidate": candidate, "evidence_bundle": bundle, "route": "review"}
