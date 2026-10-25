"""Structural planning and conservative invalidation; no paid calls here."""
from __future__ import annotations

import copy
from ..contracts import Source, Solution, Step
from ..domain.v2 import Draft, ChangeSet, digest

SCALAR_FIELDS = {"coords", "vector", "degrees", "angle", "radius", "expression"}


def scene_refs(scene) -> set[str]:
    refs = set()
    def visit(value, enabled=False):
        if isinstance(value, str) and enabled and value.startswith("@"):
            refs.add(value[1:])
        elif isinstance(value, list):
            for v in value:
                visit(v, enabled)
        elif isinstance(value, dict):
            for key, v in value.items():
                visit(v, key in SCALAR_FIELDS)
    visit(scene.model_dump())
    return refs


def operations(draft):
    return {op.id: (qid, op) for qid, q in draft.questions.items() for op in q.computations}


def closure(ids, dependencies):
    result = set(ids)
    pending = list(ids)
    while pending:
        for parent in dependencies.get(pending.pop(), []):
            if parent not in result:
                result.add(parent)
                pending.append(parent)
    return result



def normalize_step_references(source: Source, draft: Draft):
    """Qualify only exact, globally unique step IDs; never guess a dependency.

    Source condition IDs take precedence. Unknown/ambiguous refs remain intact
    so the existing source-order/self/future-reference validator rejects them.
    The raw model output stays on disk; the caller audits the exact rewrites.
    """
    conditions = {c.id for c in source.conditions}
    owners = {}
    for qid, question in draft.questions.items():
        for step in question.proof.steps:
            owners.setdefault(step.id, []).append(f"{qid}:{step.id}")
    normalized = draft.model_copy(deep=True)
    changes = []
    def qualify(record, path):
        for index, basis in enumerate(record.basis_ids):
            candidates = owners.get(basis, [])
            if basis not in conditions and ":" not in basis and len(candidates) == 1:
                qualified = candidates[0]
                record.basis_ids[index] = qualified
                changes.append({"path": f"{path}.basis_ids.{index}", "from": basis, "to": qualified})
    for qid, question in normalized.questions.items():
        for index, step in enumerate(question.proof.steps):
            qualify(step, f"questions.{qid}.proof.steps.{index}")
        for index, operation in enumerate(question.computations):
            qualify(operation, f"questions.{qid}.computations.{index}")
        for index, macro in enumerate(question.scene.constructions):
            qualify(macro, f"questions.{qid}.scene.constructions.{index}")
    return normalized, changes


def validate_draft(source: Source, draft: Draft) -> None:
    ids = [q.id for q in source.questions]
    if set(draft.questions) != set(ids):
        raise ValueError("draft questions must exactly match source: " + ",".join(ids))
    conditions = {c.id: c for c in source.conditions}
    available = set(conditions)
    op_ids = [op.id for q in draft.questions.values() for op in q.computations]
    if len(op_ids) > 64 or len(op_ids) != len(set(op_ids)):
        raise ValueError("computation IDs must be globally unique (maximum 64)")
    by_op = operations(draft)
    for qid in ids:
        q = draft.questions[qid]
        if not set(q.proof.condition_ids) <= set(conditions):
            raise ValueError(qid + ": unknown condition")
        step_ids = [s.id for s in q.proof.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError(qid + ": duplicate stable step ID")
        before_step = {}
        for step in q.proof.steps:
            before_step[step.id] = available.copy()
            if not set(step.basis_ids) <= available:
                raise ValueError(qid + ": future, self or unknown proof basis")
            available.add(f"{qid}:{step.id}")
        for op in q.computations:
            if op.target_step_id not in before_step or not set(op.basis_ids) <= before_step[op.target_step_id]:
                raise ValueError(op.id + ": a check cannot assume its own target or a future step")
            if any(d not in by_op for d in op.depends_on):
                raise ValueError(op.id + ": unknown computation dependency")
        if not set(q.scene.step_links) <= set(step_ids):
            raise ValueError(qid + ": unknown step in scene map")
        for macro in q.scene.constructions:
            if not set(macro.basis_ids) <= available:
                raise ValueError(macro.id + ": unknown construction basis")
        if not scene_refs(q.scene) <= set(by_op):
            raise ValueError(qid + ": unknown scene computation reference")
    # The executor must not wait on a cycle, including dependencies without scalar refs.
    done, active = set(), set()
    def visit(oid):
        if oid in active:
            raise ValueError("cyclic computation dependency: " + oid)
        if oid in done:
            return
        active.add(oid)
        for parent in by_op[oid][1].depends_on:
            visit(parent)
        active.remove(oid)
        done.add(oid)
    for oid in by_op:
        visit(oid)


def solutions(source: Source, draft: Draft) -> list[Solution]:
    """Translate stable step IDs to the existing public positional contract."""
    mapping = {f"{q.id}:{s.id}": f"{q.id}:S{i}"
               for q in source.questions
               for i, s in enumerate(draft.questions[q.id].proof.steps, 1)}
    result = []
    for q in source.questions:
        proof = draft.questions[q.id].proof
        result.append(Solution(question_id=q.id, result=proof.result, cases=proof.cases,
            domain_and_boundaries=proof.domain_and_boundaries, condition_ids=proof.condition_ids,
            steps=[Step(premise=s.premise, rule=s.rule, conclusion=s.conclusion,
                        basis_ids=[mapping.get(b, b) for b in s.basis_ids]) for s in proof.steps]))
    return result


def prerequisite_ids(draft: Draft) -> set[str]:
    deps = {oid: op.depends_on for oid, (_, op) in operations(draft).items()}
    return closure({r for q in draft.questions.values() for r in scene_refs(q.scene)}, deps)


def question_dependencies(draft):
    by_op = operations(draft)
    deps = {q: set() for q in draft.questions}
    for qid, q in draft.questions.items():
        references = [b for s in q.proof.steps for b in s.basis_ids]
        references += [b for o in q.computations for b in o.basis_ids]
        deps[qid].update(b.split(":", 1)[0] for b in references if ":" in b)
        for oid in scene_refs(q.scene) | {d for o in q.computations for d in o.depends_on}:
            deps[qid].add(by_op[oid][0])
        deps[qid].discard(qid)
    return deps


def dependency_fingerprints(draft, qid):
    # Proof text is included, so reusing a stable ID with new content never reuses proof evidence.
    relevant = closure({qid}, question_dependencies(draft))
    return {q: digest(draft.questions[q].proof) for q in sorted(relevant)}


def apply_changes(source, previous, change: ChangeSet, revision, issues):
    if change.expected_candidate_revision != revision:
        raise ValueError("stale patch: candidate revision changed")
    allowed = {}
    for issue in issues:
        if issue["stage"] == "source":
            raise ValueError("source issues must go back to source extraction")
        target = issue.get("target") or issue.get("question_id")
        # Unscoped browser errors cannot authorize arbitrary mathematical edits.
        targets = [target] if target in previous.questions else list(previous.questions)
        for qid in targets:
            allowed.setdefault(qid, set()).update(
                {"proof", "computations"} if issue["stage"] == "math" else {"scene"})
    raw = previous.model_dump()
    changed = set()
    for qid, update in change.updates.items():
        fields = {k: v for k, v in update.model_dump().items() if v is not None}
        if qid not in allowed or not set(fields) <= allowed[qid]:
            raise ValueError("patch exceeds authorized question/field scope: " + qid)
        if "computations" in fields:
            old_ids = {o.id for o in previous.questions[qid].computations}
            new_ids = {o["id"] for o in fields["computations"]}
            if not old_ids <= new_ids:
                raise ValueError("a patch cannot drop a computational obligation; correct it with the same ID")
            updated_ops = {o["id"]: o for o in fields["computations"]}
            for old in previous.questions[qid].computations:
                if old.expected is not None and updated_ops[old.id].get("expected") is None:
                    raise ValueError("a check cannot be downgraded to an unchecked calculation")
        raw["questions"][qid].update(copy.deepcopy(fields))
        if raw["questions"][qid] != previous.questions[qid].model_dump():
            changed.add(qid)
    if not changed:
        raise ValueError("patch makes no change")
    current, normalization = normalize_step_references(source, Draft.model_validate(raw))
    validate_draft(source, current)
    old_deps, new_deps = question_dependencies(previous), question_dependencies(current)
    affected = set(changed)
    while True:
        more = {q for q in previous.questions if (old_deps[q] | new_deps[q]) & affected}
        if more <= affected:
            break
        affected |= more
    return current, {"changed_questions": sorted(changed), "affected_questions": sorted(affected),
        "reference_normalization": normalization,
        "review_policy": "full independent review; only exact computational receipts may be reused"}
