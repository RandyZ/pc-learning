from ....contracts import Source, Candidate, Review
from ....domain.v2 import Draft
from ....services.reviewing import review, publication_gate
from ....storage import write_json


def independent_review(state, ctx):
    value, resolution = review(ctx.runner, Source.model_validate(state["source"]),
        Candidate.model_validate(state["candidate"]), Draft.model_validate(state["draft"]),
        state["diagram_evidence"]["observation"], state["evidence_bundle"])
    write_json(ctx.runner.directory / "review.json", value.model_dump())
    write_json(ctx.runner.directory / "v2-operation-resolution.json", resolution)
    if value.verdict == "needs_input":
        from ....pipeline import NeedsInput
        raise NeedsInput("；".join(i.detail for i in value.issues) or "复核仍有未确定信息")
    return {"review": value.model_dump(), "operation_resolution": resolution,
            "feedback": [i.model_dump() for i in value.issues], "route": "publish" if value.verdict == "pass" else "repair"}


def repair(state, ctx):
    count = state.get("repairs", 0)
    if count >= ctx.runner.settings.max_repairs:
        raise ValueError("V2修复次数已用完；保留异议，不发布半成品")
    issues = state["feedback"]
    if not issues:
        raise ValueError("repair requires explicit issues")
    ctx.runner.audit("repair", {"attempt": count + 1, "feedback": issues})
    if any(i["stage"] == "source" for i in issues):
        return {"repairs": count + 1, "route": "source"}
    current, change = ctx.solver.repair(Source.model_validate(state["source"]),
        Draft.model_validate(state["draft"]), state["candidate_revision"], issues)
    prior = state.get("prior_evidence", []) + [{"candidate_revision": state["candidate_revision"],
        "issues": issues, "receipts": state.get("math_evidence", {}).get("receipts", state.get("prerequisites", {}))}]
    write_json(ctx.runner.directory / f"v2-change-{count+1:02d}.json", change)
    ctx.runner.audit("v2_change", {"stage": "repair", "attempt": count + 1,
        "changed_questions": change["changed_questions"], "affected_questions": change["affected_questions"]})
    return {"draft": current.model_dump(), "repairs": count + 1, "prior_evidence": prior, "route": "prepare"}


def publish(state, ctx):
    publication_gate(Source.model_validate(state["source"]), Candidate.model_validate(state["candidate"]),
        Draft.model_validate(state["draft"]), Review.model_validate(state["review"]),
        state["evidence_bundle"], state["operation_resolution"], state["source_revision"],
        state["candidate_revision"], ctx.runner.deferred_reviews.pending)
    ctx.check()
    ctx.publish(state)
    return {"completed": True}
