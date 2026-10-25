"""Propose all three plans in one stage; one bounded format-only correction."""
from __future__ import annotations

from ..domain.v2 import Draft, ChangeSet, digest
from ..policies.v2 import validate_draft, apply_changes, normalize_step_references
from ..models.v2_prompts import messages
from ..tools import DEFINITIONS
from ..image_tools import VIEW_REGION
from ..storage import write_json


class PlanSolver:
    def __init__(self, runner, check):
        self.runner, self.check = runner, check

    def _request(self, stage, payload, schema, *, patch=False, correction=False):
        self.check()
        r = self.runner
        definitions = [d for d in DEFINITIONS if d["function"]["name"] in {"algebra_batch", "symbolic_geometry"}]
        if r.request["images"]:
            definitions.append(VIEW_REGION)
        if correction:
            definitions = []
        args = {}
        if stage in {"solve", "diagram"} and r.settings.solve_responses or stage == "math_repair" and r.settings.review_responses:
            args["output_schema"] = schema
        r.audit("graph_node", {"node": "patch_plan" if patch else "propose_solution", "stage": stage})
        from ..submission import receive
        raw = receive(r.provider, stage, messages(r.request, r.directory, payload, schema, patch=patch), r.audit,
            tools=definitions, dispatch=lambda name, params: r.dispatch_stage_tool(stage, r.request, name, params), **args)
        self.check()
        write_json(r.directory / f"model-{r.model_calls:02d}-v2-{stage}.json", raw)
        return raw

    def propose(self, source, feedback=None):
        schema = Draft.model_json_schema()
        item = schema["properties"]["questions"].pop("additionalProperties")
        ids = [q.id for q in source.questions]
        schema["properties"]["questions"].update(additionalProperties=False, required=ids,
            properties={qid: item for qid in ids})
        payload = {"source": source.model_dump(), "feedback": feedback or []}
        for attempt in range(2):
            raw = self._request("solve", payload, schema, correction=bool(attempt))
            try:
                parsed = Draft.model_validate(raw)
                draft, rewrites = normalize_step_references(source, parsed)
                validate_draft(source, draft)
                if rewrites:
                    self.runner.audit("reference_normalization", {"stage": "solve",
                        "before_sha256": digest(parsed), "after_sha256": digest(draft),
                        "rewrites": rewrites,
                        "detail": "Only exact unique step IDs were qualified; raw model output is retained."})
                return draft
            except ValueError as exc:
                self.runner.audit("contract_error", {"stage": "solve", "attempt": attempt + 1, "detail": str(exc)[:4000]})
                if attempt:
                    raise
                payload = {**payload, "format_correction": {"error": str(exc)[:6000], "rejected_output": raw,
                    "instruction": "仅纠正结构、引用和覆盖，不重新探索；保留全部未知和证据。"}}
        raise AssertionError("unreachable")

    def repair(self, source, draft, revision, issues):
        allowed = {}
        for issue in issues:
            target = issue.get("target")
            for qid in ([target] if target in draft.questions else draft.questions):
                allowed.setdefault(qid, set()).update({"proof", "computations"} if issue["stage"] == "math" else {"scene"})
        stage = "math_repair" if any(i["stage"] == "math" for i in issues) else "diagram"
        payload = {"source": source.model_dump(), "previous": draft.model_dump(),
            "candidate_revision": revision, "issues": issues,
            "allowed_fields": {q: sorted(v) for q, v in allowed.items()}}
        for attempt in range(2):
            raw = self._request(stage, payload, ChangeSet.model_json_schema(), patch=True, correction=bool(attempt))
            try:
                change = ChangeSet.model_validate(raw)
                updated, impact = apply_changes(source, draft, change, revision, issues)
                return updated, {"patch": change.model_dump(), **impact}
            except ValueError as exc:
                self.runner.audit("contract_error", {"stage": stage, "attempt": attempt + 1, "detail": str(exc)[:4000]})
                if attempt:
                    raise
                payload = {**payload, "format_correction": {"error": str(exc)[:6000], "rejected_output": raw}}
        raise AssertionError("unreachable")
