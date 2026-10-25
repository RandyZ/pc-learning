"""Keep the existing independent review, recovery and adjudication gates."""
from __future__ import annotations

import copy
from ..contracts import Check, Issue, check_review
from ..domain.v2 import OperationReview, RevisionReview, prior_issues, digest
from ..policies.v2 import operations


def operation_issues(source, candidate, draft, bundle, decisions):
    expected = operations(draft)
    if not isinstance(decisions, dict) or set(decisions) != set(expected):
        raise ValueError("operation_reviews must cover exactly the current computation IDs")
    valid_basis = {c.id for c in source.conditions} | {
        f"{s.question_id}:S{i}" for s in candidate.solutions for i in range(1, len(s.steps) + 1)}
    issues = []
    parsed = {}
    for oid, (qid, operation) in expected.items():
        decision = OperationReview.model_validate(decisions[oid])
        if not set(decision.basis_ids) <= valid_basis:
            raise ValueError("unknown operation-review basis")
        # A purported independent proof cannot cite the target itself or a later step.
        ordered = [f"{q.id}:{step.id}" for q in source.questions for step in draft.questions[q.id].proof.steps]
        public = [f"{s.question_id}:S{i}" for s in candidate.solutions for i in range(1, len(s.steps) + 1)]
        prior = {c.id for c in source.conditions} | set(public[:ordered.index(f"{qid}:{operation.target_step_id}")])
        if not set(decision.basis_ids) <= prior:
            raise ValueError("operation review cannot assume its own target or a later step")
        receipt = bundle["receipts"][oid]
        if decision.verdict == "pass" and decision.method == "unresolved":
            raise ValueError("unresolved operation cannot pass")
        if decision.verdict == "pass" and decision.method == "receipt" and (
                receipt["execution_status"] != "ok" or receipt["verdict"] != "pass"):
            raise ValueError("unknown/failed execution cannot be claimed as verified receipt")
        # Negative evidence remains blocking, even when the reviewer says pass.
        # Correct a bad formalized check through an audited mathematical patch.
        if receipt["verdict"] == "fail" or decision.verdict != "pass":
            issues.append(Issue(stage="math", target=qid,
                detail=f"V2计算{oid}未解除：{decision.evidence}"))
        parsed[oid] = decision.model_dump()
    return issues, parsed



def revision_issues(source, candidate, bundle, decisions):
    expected = prior_issues(bundle)
    if not isinstance(decisions, dict) or set(decisions) != set(expected):
        raise ValueError("revision_reviews must settle every prior issue explicitly")
    basis = {c.id for c in source.conditions} | {
        f"{s.question_id}:S{i}" for s in candidate.solutions for i in range(1, len(s.steps)+1)}
    issues, parsed = [], {}
    for key, issue in expected.items():
        value = RevisionReview.model_validate(decisions[key])
        if not set(value.basis_ids) <= basis:
            raise ValueError("unknown revision-review basis")
        parsed[key] = value.model_dump()
        if value.verdict != "resolved":
            issues.append(Issue(stage=issue["stage"], target=issue["target"], detail=key+"："+value.evidence))
    return issues, parsed

def review(runner, source, candidate, draft, observation, bundle):
    if digest(candidate) != bundle["candidate_hash"]:
        raise ValueError("candidate changed before independent review")
    runner.provider.review_bundle = bundle
    runner.provider.operation_reviews = None
    runner.provider.revision_reviews = None
    try:
        # Reuses original: original pixels, inspection recovery, proof guards,
        # mathematical-first deferral, independent objection adjudication.
        value = runner.review(source, candidate, {**observation, "v2_evidence": bundle})
        additions, decisions = operation_issues(source, candidate, draft, bundle, runner.provider.operation_reviews)
        pending, revisions = revision_issues(source, candidate, bundle, runner.provider.revision_reviews)
        additions.extend(pending)
    finally:
        runner.provider.review_bundle = None
    if additions:
        value = value.model_copy(deep=True)
        value.issues.extend(additions)
        value.verdict = "revise" if value.verdict != "needs_input" else value.verdict
        affected = {i.target for i in additions}
        value.questions = [Check(id=q.id, verdict="issue", evidence="；".join(i.detail for i in additions if i.target == q.id))
                           if q.id in affected else q for q in value.questions]
        check_review(value, source)
    return value, {"candidate_revision": bundle["candidate_revision"], "candidate_hash": digest(candidate),
                   "decisions": decisions, "revision_decisions": revisions, "review_hash": digest(value)}


def publication_gate(source, candidate, draft, review_value, bundle, resolution, source_revision, revision, pending):
    from ..contracts import check_candidate
    if source.uncertainties:
        raise ValueError("unresolved source blocks publication")
    if pending:
        raise ValueError("unresolved deferred obligations block publication")
    check_candidate(candidate, source)
    check_review(review_value, source)
    if review_value.verdict != "pass":
        raise ValueError("independent review has not passed")
    if bundle["source_revision"] != source_revision or bundle["candidate_revision"] != revision:
        raise ValueError("stale evidence bundle")
    if bundle["candidate_hash"] != digest(candidate) or bundle.get("browser_status") != "pass":
        raise ValueError("missing current browser evidence")
    if resolution["candidate_revision"] != revision or resolution["candidate_hash"] != digest(candidate) or resolution["review_hash"] != digest(review_value):
        raise ValueError("review is not bound to the current version")
    if set(bundle["receipts"]) != set(operations(draft)):
        raise ValueError("missing mandatory computation evidence")
    for receipt in bundle["receipts"].values():
        if receipt["candidate_revision"] != revision or receipt["source_revision"] != source_revision:
            raise ValueError("mixed evidence revisions")
    issues, _ = operation_issues(source, candidate, draft, bundle, resolution["decisions"])
    pending, _ = revision_issues(source, candidate, bundle, resolution.get("revision_decisions", {}))
    if issues or pending:
        raise ValueError("unresolved computational or prior-issue evidence")
